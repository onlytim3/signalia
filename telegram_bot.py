"""
Signalia — two-way Telegram interface.

notifier.py pushes alerts OUT; this module answers commands coming IN, so the
ladder lives in your pocket: update your held %, ask why the target is what it
is, or pull the scan — all from the chat where the alerts already arrive.

Plumbing: app.py exposes POST /telegram-webhook (an open path — Telegram's
servers can't carry the dashboard token; authentication is the secret-token
header, derived from the bot token so no extra env var is needed) and forwards
updates here. Only the configured TELEGRAM_CHAT_ID is ever answered; anyone
else who finds the bot gets silence.

Every command is a thin adapter over logic that already exists in the engine,
store, and scorecard — this module owns parsing + formatting only, so the
pure parts stay selftest-able offline.
"""
import hashlib
import time

import requests

import config as C
import scorecard
import store

HELP = """Signalia — the ladder in your pocket

/status — the read + target vs held
/why — layer-by-layer: what sets today's target
/deployed 25 — record that you now hold 25%
/scan — market breadth, movers, squeeze
/candidates — names earning a watchlist spot
/watch SOL · /unwatch SOL — edit the watchlist
/scorecard — the model grading its own calls
/help — this list"""


# ------------------------------ plumbing ---------------------------------------
def webhook_secret():
    """Shared secret for Telegram's X-Telegram-Bot-Api-Secret-Token header.
    Derived from the bot token (itself a secret) — stable across restarts,
    no extra env var to manage."""
    return hashlib.sha256(("signalia-webhook:" + C.TELEGRAM_TOKEN)
                          .encode()).hexdigest()[:48]


def register_webhook():
    """Point the bot at this deployment. Called at boot; safe to re-run."""
    if not (C.TELEGRAM_TOKEN and C.TELEGRAM_WEBHOOK_BASE):
        return False
    url = C.TELEGRAM_WEBHOOK_BASE.rstrip("/") + "/telegram-webhook"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/setWebhook",
            json={"url": url, "secret_token": webhook_secret(),
                  "allowed_updates": ["message"]},
            timeout=15)
        ok = bool(r.ok and r.json().get("ok"))
        print("telegram webhook:", f"registered {url}" if ok
              else f"failed: {r.text[:200]}")
        return ok
    except Exception as e:
        print("telegram webhook error:", e)
        return False


def send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as e:
        print("telegram send error:", e)


# ------------------------------ formatting (pure) ------------------------------
def _pct(x, digits=0):
    return f"{x:.{digits}f}%" if x is not None else "—"


def fmt_status(snap):
    if not snap or not snap.get("ladder"):
        return "engine is still warming up — try again in a minute"
    s = snap.get("sizing") or {}
    lines = [snap.get("read") or "(no read)"]
    bits = [f"target {_pct(snap['ladder'].get('target'))}",
            f"held {_pct(s.get('current_pct'))}"]
    if s.get("actionable") and s.get("delta_usd") is not None:
        bits.append(f"{s['action']} ${s['delta_usd']:,.0f}")
    lines.append(" · ".join(bits))
    if snap.get("degraded"):
        lines.append("degraded feeds: " + ", ".join(snap["degraded"]))
    return "\n\n".join(lines)


def binding_gate(ladder):
    """Which of the three gates is setting the target (the one that = min)."""
    if not ladder:
        return None
    t = ladder.get("target")
    if t is None:
        return None
    if t == ladder.get("confirm_cap") and t < ladder.get("raw_desire", 101):
        return "confirmation cap — price hasn't proven the turn"
    if t == ladder.get("macro_ceiling") and t < ladder.get("raw_desire", 101):
        return "macro ceiling — your own regime setting is the brake"
    return "raw desire — structure + sentiment are the limit"

def fmt_why(snap):
    if not snap or not snap.get("ladder"):
        return "engine is still warming up — try again in a minute"
    l = snap["ladder"]
    funding = snap.get("funding")
    conf = snap.get("confirms") or {}
    marks = {True: "✓", False: "✗"}
    lines = [
        f"TARGET {_pct(l.get('target'))} — binding gate: {binding_gate(l)}",
        "",
        f"structural {snap.get('structural')}/100 "
        f"(funding {funding * 100:+.4f}%/8h, OI {snap.get('oi_change', 0) * 100:+.1f}%"
        + (f", liq-flush {snap['liq_flush']:.0f}" if snap.get("liq_flush") is not None
           else ", liq warming up") + ")",
        f"sentiment {snap.get('sentiment')}/100 (F&G {snap.get('fng')} — "
        f"{snap.get('fng_class', '?')})",
        f"→ raw desire {l.get('raw_desire')} "
        f"({C.W_STRUCTURAL:.2f}·struct + {C.W_SENTIMENT:.2f}·sent)",
        f"macro {snap.get('regime')} → ceiling {l.get('macro_ceiling')}",
        f"confirmations {l.get('confirm_count')}/3 → cap {l.get('confirm_cap')}",
        f"  {marks.get(conf.get('btc_reclaim'), '?')} BTC ≥ "
        f"${snap.get('reclaim_level', 0):,.0f} (now ${snap.get('btc_price', 0):,.0f})",
        f"  {marks.get(conf.get('funding_positive'), '?')} funding positive",
        f"  {marks.get(conf.get('fng_above_30'), '?')} F&G ≥ {C.FNG_CONFIRM} "
        f"(now {snap.get('fng')})",
    ]
    if snap.get("overheat") is not None:
        lines.append(f"overheat {snap['overheat']:.0f}/100"
                     + (" ⚠" if snap["overheat"] >= C.OVERHEAT_ALERT else ""))
    return "\n".join(lines)


def _row_line(r):
    return (f"{r['symbol'].replace('USDT', '')} {r['chg24h'] * 100:+.1f}% "
            f"(funding {r['funding'] * 100:+.4f}%)")


def fmt_scan(scan_data):
    sc = (scan_data or {}).get("scan")
    if not sc:
        return "no scan yet — it runs every 30 min"
    screens = sc.get("screens") or {}
    out = [f"breadth: {sc.get('breadth')}% of {sc.get('universe')} liquid names green"]
    for key, label in (("gainers", "gainers"), ("losers", "losers"),
                       ("squeeze", "squeeze setups")):
        rows = (screens.get(key) or [])[:3]
        if rows:
            out.append(label + ": " + " · ".join(_row_line(r) for r in rows))
    flags = (scan_data or {}).get("watchlist_flags") or {}
    hot = [f"{s.replace('USDT', '')} ({', '.join(v)})" for s, v in flags.items() if v]
    if hot:
        out.append("watchlist flagged: " + " · ".join(hot))
    sugg = [c for c in (scan_data or {}).get("candidates") or [] if c.get("suggest")]
    if sugg:
        out.append("earning a watchlist spot: "
                   + " · ".join(c["symbol"].replace("USDT", "") for c in sugg[:3])
                   + " — /candidates for why")
    return "\n".join(out)


def fmt_candidates(scan_data):
    board = (scan_data or {}).get("candidates")
    if not board:
        return ("no candidates yet — the board fills as routine scans "
                "accumulate (persistence is the signal)")
    out = ["watchlist candidates (recurrence-scored):"]
    for c in board:
        mark = "→" if c.get("suggest") else "·"
        out.append(f"{mark} {c['symbol'].replace('USDT', '')} "
                   f"[{c['score']}] {c['reason']}")
    out.append("→ = clears the suggestion bar · /watch NAME to track one")
    return "\n".join(out)


def fmt_scorecard(sc, horizon="1d"):
    h = (sc.get("horizons") or {}).get(horizon) or {}
    buckets = h.get("buckets") or []
    if not buckets:
        return "scorecard is still maturing — needs stored readings + forward returns"
    out = [f"scorecard @ {horizon} (matured n={h.get('matured')}, "
           f"baseline median {h.get('baseline_median'):+}%):"]
    for b in sorted(buckets, key=lambda x: abs(x.get("edge_vs_base") or 0),
                    reverse=True)[:5]:
        out.append(f"{'★' if b.get('confident') else '·'} {b['label']}: "
                   f"median {b['median_pct']:+}% · {b['hit_rate']}% hit · n={b['n']} "
                   f"· edge {b['edge_vs_base']:+}%")
    out.append("★ = confident sample · grades, not promises")
    return "\n".join(out)


# ------------------------------ commands ---------------------------------------
def _cmd_deployed(args, snap):
    try:
        pct = float(args[0])
    except (IndexError, ValueError):
        return "usage: /deployed 25 — the % of your capital base now deployed"
    if not 0 <= pct <= 100:
        return "pct must be 0–100"
    store.set_meta("deployed_pct", pct)
    store.set_meta("deployed_set_ts", time.time())
    tgt = ((snap or {}).get("ladder") or {}).get("target")
    if tgt is None:
        return f"recorded: you hold {pct:.0f}%"
    gap = tgt - pct
    if abs(gap) < C.MIN_TRANCHE_PCT:
        verdict = f"that's on target ({_pct(tgt)}) — hold"
    else:
        verdict = (f"target is {_pct(tgt)} — "
                   f"{'add' if gap > 0 else 'trim'} about {abs(gap):.0f} points")
    return f"recorded: you hold {pct:.0f}%. {verdict}"


def route(text, ctx):
    """Map one command line to its reply. ctx: snap/scan dicts + watchlist fns."""
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return fmt_status(ctx.get("snap"))
    if cmd == "/why":
        return fmt_why(ctx.get("snap"))
    if cmd == "/deployed":
        return _cmd_deployed(args, ctx.get("snap"))
    if cmd == "/scan":
        return fmt_scan(ctx.get("scan"))
    if cmd == "/candidates":
        return fmt_candidates(ctx.get("scan"))
    if cmd == "/scorecard":
        try:
            return fmt_scorecard(scorecard.build())
        except Exception as e:
            return f"scorecard error: {e}"
    if cmd == "/watch":
        return (ctx["watch"](args[0]) if args else "usage: /watch SOL")
    if cmd == "/unwatch":
        return (ctx["unwatch"](args[0]) if args else "usage: /unwatch SOL")
    return "unknown command — /help lists what I speak"


def handle_update(update, ctx):
    """Telegram update -> (reply_text|None, chat_id|None).
    Silence for any chat that isn't the configured owner."""
    msg = (update or {}).get("message") or {}
    chat_id = str(((msg.get("chat") or {}).get("id")) or "")
    if not chat_id or chat_id != str(C.TELEGRAM_CHAT_ID):
        return None, None
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return "I speak commands — /help lists them", chat_id
    return route(text, ctx), chat_id
