"""
Signalia — orchestration.

run_once() gathers the four layers, computes the ladder, compares to the last
stored snapshot, and fires alerts only on meaningful changes.
"""
import datetime
import json
import os
import time

import config as C
import signals as S
import baseline
import candidates
import store
import scanner
import scorecard
import trend
import watchlist as wl
import whales
import sizing
from notifier import notify

# Set by app.py at boot (the running monitors). None when unavailable.
LIQ_MONITOR = None
WHALE_TAPE = None


# ------------------------------ Gather ---------------------------------------
def _last_reading():
    """Most recent stored reading, for graceful degradation fallbacks."""
    try:
        rows = store.recent_rows(1)
        return rows[0] if rows else {}
    except Exception:
        return {}


def gather():
    """
    Resilient gather: the ticker is the spine (no price -> the run fails, as
    before), but every other source degrades gracefully to the last stored
    value instead of killing the whole run — APIs get flaky exactly when the
    market is violent, which is when a reading matters most. Degraded sources
    are listed in snap["degraded"] and surfaced in reports + dashboard.
    """
    degraded = []
    last = _last_reading()

    led = S.bybit_ticker(C.LADDER_SYMBOL)          # critical — no fallback

    try:
        oi_chg = S.bybit_oi_change(C.LADDER_SYMBOL)
    except Exception:
        oi_chg = float(last.get("oi_change") or 0.0)
        degraded.append("oi")

    try:
        fng, fng_class = S.fear_greed()
    except Exception:
        if last.get("fng") is not None:
            fng, fng_class = int(last["fng"]), "unavailable (last known)"
        else:
            fng, fng_class = 50, "unavailable (neutral default)"
        degraded.append("fng")

    try:
        dom = S.btc_dominance()
    except Exception:
        dom = None
        degraded.append("dominance")

    # Trend & cycle context (50d/200d SMA, 20d range, realized vol)
    tctx = trend.context(C.LADDER_SYMBOL, led["price"])
    if tctx is None:
        degraded.append("klines")

    # Funding stability for the confirm (mean of last N settled rates)
    try:
        funding_avg = S.bybit_funding_history(C.LADDER_SYMBOL, C.FUNDING_CONFIRM_N)
    except Exception:
        funding_avg = None                          # confirm degrades to instantaneous
        degraded.append("funding_history")

    # Live liquidation layer (only trusted after warmup AND while the feed is
    # actually delivering — a silent zombie socket must not read as "quiet")
    liq_flush, liq_stats, ws_ready = None, None, False
    if (LIQ_MONITOR is not None and LIQ_MONITOR.ready(C.LIQ_WARMUP_SEC)
            and LIQ_MONITOR.healthy()):
        ws_ready = True
        liq_flush = S.liq_flush_score(LIQ_MONITOR.long_liq_bins(C.LIQ_BINS))
        liq_stats = LIQ_MONITOR.stats(C.LIQ_BIN_SEC)

    # Whale flow: tape (WS, warmup-gated) + book depth + crowd ratio + absorption
    try:
        depth = whales.depth_imbalance(C.LADDER_SYMBOL)
    except Exception:
        depth = None
        degraded.append("depth")
    try:
        crowd_long = whales.account_ratio(C.LADDER_SYMBOL)
    except Exception:
        crowd_long = None
        degraded.append("crowd")
    tape_ready, tape_4h, tape_1h = False, None, None
    summary, summary_1h, dir_bins = None, None, None
    if (WHALE_TAPE is not None and WHALE_TAPE.ready(C.WHALE_WARMUP_SEC)
            and WHALE_TAPE.healthy()):
        tape_ready = True
        tape_4h = WHALE_TAPE.delta(C.WHALE_WINDOW_SEC)
        tape_1h = WHALE_TAPE.delta(3600)
        summary = WHALE_TAPE.summary(C.WHALE_WINDOW_SEC)
        summary_1h = WHALE_TAPE.summary(3600)
        dir_bins = WHALE_TAPE.whale_net_bins(C.WHALE_DIR_BINS, C.WHALE_BIN_SEC)
    whale_ctx = {
        "tape_ready": tape_ready,
        "tape_connected": WHALE_TAPE.healthy() if WHALE_TAPE is not None else False,
        "clip_min": C.WHALE_MIN_CLIP_USD,
        "retail_max": C.WHALE_RETAIL_MAX_USD,
        "tape_4h": tape_4h,
        "tape_1h": tape_1h,
        "summary": summary,          # tiered: whale/mid/retail vol+bias, share, divergence
        "summary_1h": summary_1h,
        "dir_bins": dir_bins,        # whale net per 15-min bin, oldest -> newest
        "depth": depth,
        "crowd_long": crowd_long,
        "crowd": whales.crowd_read(crowd_long),
        "absorption": whales.absorption_note(oi_chg, led.get("chg24h")),
    }

    # Reclaim level: auto-track the 50d SMA unless pinned via env
    reclaim_level, reclaim_source = C.BTC_RECLAIM_LEVEL, "manual"
    if C.BTC_RECLAIM_AUTO and tctx and tctx.get("reclaim_auto"):
        reclaim_level, reclaim_source = tctx["reclaim_auto"], "auto (50d SMA)"

    # Adaptive structural score (percentile vs rolling baseline; static fallback)
    struct = baseline.structural_score(led["funding"], oi_chg, liq_flush)
    sent = S.sentiment_score(fng)
    confirms = S.confirmation_triggers(led["price"], led["funding"], fng,
                                       reclaim_level, funding_avg)
    ladder = S.compute_ladder(struct, sent, C.MACRO_REGIME, confirms)

    # Overheat / top-risk mirror — raw composite, then adaptive percentile
    # vs its own rolling history once enough samples exist
    overheat_raw, oh_parts = S.overheat_score(
        led["funding"], fng,
        tctx.get("px_vs_slow") if tctx else None,
        oi_chg)
    overheat, oh_adaptive = baseline.overheat_adaptive(overheat_raw)

    # Position-aware sizing (uses last known deployed % from meta, default 0)
    try:
        current_dep = float(store.get_meta("deployed_pct", 0) or 0)
    except Exception:
        current_dep = 0.0
    size = sizing.tranche(ladder["target"], current_dep)
    try:   # stale held% silently poisons sizing — surface its age
        set_ts = store.get_meta("deployed_set_ts")
        size["set_age_days"] = round((time.time() - float(set_ts)) / 86400, 1) if set_ts else None
    except Exception:
        size["set_age_days"] = None

    snap = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "regime": C.MACRO_REGIME,
        "btc_price": led["price"],
        "funding": led["funding"],
        "funding_avg": funding_avg,
        "oi_change": round(oi_chg, 4),
        "fng": fng,
        "fng_class": fng_class,
        "dominance": dom,
        "ws_ready": ws_ready,
        "liq_flush": liq_flush,
        "liq_stats": liq_stats,
        "adaptive": baseline.using_adaptive(),
        "structural": round(struct, 1),
        "sentiment": round(sent, 1),
        "trend": tctx,
        "whales": whale_ctx,
        "whale_bias": (summary or {}).get("whale", {}).get("bias"),
        "retail_bias": (summary or {}).get("retail", {}).get("bias"),
        "overheat": overheat,
        "overheat_raw": overheat_raw,
        "overheat_adaptive": oh_adaptive,
        "overheat_parts": oh_parts,
        "reclaim_level": reclaim_level,
        "reclaim_source": reclaim_source,
        "confirms": confirms,
        "ladder": ladder,
        "sizing": size,
        "degraded": degraded,
    }
    snap["read"] = plain_read(snap)
    return snap


# ------------------------------ State ----------------------------------------
def load_state():
    if os.path.exists(C.STATE_FILE):
        try:
            with open(C.STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(snap, prev=None):
    prev = prev or {}
    t = snap.get("trend") or {}
    # divergence alert memory: remember what we alerted on; re-arm only after
    # the verdict has decayed to neutral for two consecutive runs
    div_key = (((snap.get("whales") or {}).get("summary") or {}).get("divergence") or {}).get("key")
    alerted = snap.pop("_div_alerted", None) or prev.get("whale_div_alerted")
    if div_key == "neutral" and prev.get("whale_div_key") == "neutral":
        alerted = None
    slim = {
        "target": snap["ladder"]["target"],
        "regime": snap["regime"],
        "confirms": snap["confirms"],
        "fng": snap["fng"],
        "btc_price": snap["btc_price"],
        "liq_flush": snap.get("liq_flush"),
        "overheat": snap.get("overheat"),
        "golden": t.get("golden"),
        "px_above_slow": (t["px_vs_slow"] > 0) if t.get("px_vs_slow") is not None else None,
        "whale_net_1h": ((snap.get("whales") or {}).get("tape_1h") or {}).get("net"),
        "whale_div_key": div_key,
        "whale_div_alerted": alerted,
    }
    with open(C.STATE_FILE, "w") as f:
        json.dump(slim, f)


# ------------------------- Plain-English synthesis ----------------------------
def plain_read(snap):
    """
    The whole snapshot in one breath — the same sentences appear on the
    dashboard, in Telegram, and in email, so a layman and a quant are reading
    the identical situation. Each sentence carries interpretation, not labels:
      1. mood, and whether POSITIONING agrees with it (the head-vs-books tension)
      2. where price is offering this (range floor or mid-air, with vs against trend)
      3. the target, and WHO holds the key to more (you, price, or the setup)
      4. the action, sized to how violent the tape is
      5. risks under the hood (overheat, whale divergence, degraded feeds)
    """
    l = snap["ladder"]
    parts = []

    # 1 — mood vs positioning
    fng = snap.get("fng", 50)
    st = snap.get("structural") or 0
    funding = snap.get("funding") or 0.0
    mood = ("deep fear" if fng <= 20 else "fear" if fng <= 35
            else "greed" if fng >= 65 else "a neutral mood")
    if fng <= 35 and st >= 65:
        parts.append(f"The crowd is in {mood} and positioning is genuinely washed out "
                     f"(structure {st:.0f}/100) — the classic accumulation zone.")
    elif fng <= 35 and funding > 0:
        parts.append(f"The crowd is in {mood}, yet longs are still paying to hold — "
                     f"scared in the head, not flushed in the books (structure {st:.0f}/100).")
    elif fng <= 35:
        parts.append(f"The crowd is in {mood} but the forced selling isn't finished "
                     f"(structure {st:.0f}/100) — panic without a full flush yet.")
    elif fng >= 65:
        parts.append(f"The crowd is in {mood} — appetite falls when everyone is brave "
                     f"(structure {st:.0f}/100).")
    else:
        parts.append(f"The crowd is in {mood} and structure reads {st:.0f}/100 — "
                     f"nothing extreme either way.")

    # 2 — where price is offering this
    t = snap.get("trend") or {}
    rp, slow = t.get("range_pos"), t.get("px_vs_slow")
    if rp is not None and slow is not None:
        spot = ("at the floor of its 20-day range" if rp <= 25 else
                "at the top of its 20-day range" if rp >= 75 else "mid-range")
        side = (f"{abs(slow):.0f}% below the 200-day trend" if slow < 0
                else f"{slow:.0f}% above the 200-day trend")
        tail = " — counter-trend buying, stay staged." if slow < 0 and l["target"] > 0 else "."
        parts.append(f"Price is {spot}, {side}{tail}")

    # 3 — the target, and who holds the key to more
    gates = {"desire": l["raw_desire"], "macro": l["macro_ceiling"], "confirm": l["confirm_cap"]}
    binding = min(gates, key=gates.get)
    runner_up = sorted(gates.values())[1]
    if binding == "macro":
        parts.append(f"Target {l['target']:.0f}% — the brake is your own "
                     f"{snap.get('regime', '?')} macro setting; the signals alone would "
                     f"allow {runner_up:.0f}%.")
    elif binding == "confirm":
        nxt = ""
        if not (snap.get("confirms") or {}).get("btc_reclaim") and snap.get("reclaim_level"):
            nxt = f"; next unlock is BTC back above ${snap['reclaim_level']:,.0f}"
        parts.append(f"Target {l['target']:.0f}% — price hasn't proven the turn yet "
                     f"({l['confirm_count']}/3 confirmations{nxt}).")
    else:
        parts.append(f"Target {l['target']:.0f}% — the setup itself is the limit; "
                     f"no external gate is holding you back.")

    # 4 — the action, sized to the tape
    z = snap.get("sizing") or {}
    held = z.get("current_pct", 0) or 0
    stage = (" in 2–3 clips (the tape is violent)"
             if t.get("rv_pctile") is not None and t["rv_pctile"] >= C.RV_STAGE_PCTILE else "")
    if z.get("actionable") and z.get("action") == "ADD":
        parts.append(f"You hold {held:.0f}% — add about {z['delta_pct']:.0f} points{stage}.")
    elif z.get("actionable") and z.get("action") == "TRIM":
        parts.append(f"You hold {held:.0f}% — that's ahead of the model; "
                     f"trim about {z['delta_pct']:.0f} points.")
    else:
        parts.append(f"You hold {held:.0f}% — close enough to target; no action.")

    # 5 — risks under the hood
    oh = snap.get("overheat")
    if oh is not None and oh >= C.OVERHEAT_ALERT:
        parts.append("Caution: the long side is crowded — overheat is flashing.")
    div = (((snap.get("whales") or {}).get("summary") or {}).get("divergence") or {})
    if div.get("key") in ("smart_accumulation", "smart_distribution"):
        parts.append("Under the hood, " + div["text"] + ".")
    if snap.get("degraded"):
        parts.append("(Some data feeds are degraded; reading from last known values.)")
    return " ".join(parts)


# ------------------------------ Alerts ---------------------------------------
def diff_alerts(prev, snap):
    msgs = []
    if not prev:
        msgs.append("Signalia online — baseline established.")
        return msgs

    dt = snap["ladder"]["target"] - prev.get("target", 0)
    if abs(dt) >= C.ALERT_DELTA:
        action = "↑ ADD a tranche" if dt > 0 else "↓ PAUSE buys / consider trim"
        msgs.append(f"Deployment target {dt:+.0f}pts -> {snap['ladder']['target']:.0f}%  ({action})")

    if snap["regime"] != prev.get("regime"):
        msgs.append(f"Macro regime: {prev.get('regime')} -> {snap['regime']}")

    for k, v in snap["confirms"].items():
        if prev.get("confirms", {}).get(k) != v:
            msgs.append(f"Confirmation '{k}' -> {'ON' if v else 'OFF'}")

    for thr in (C.DEEP_FEAR, 25, C.FNG_CONFIRM):
        p, c = prev.get("fng"), snap["fng"]
        if p is not None and ((p < thr <= c) or (p >= thr > c)):
            msgs.append(f"Fear & Greed crossed {thr} ({p}->{c}, {snap['fng_class']})")

    pl, cl = prev.get("liq_flush"), snap.get("liq_flush")
    if pl is not None and cl is not None and pl < C.LIQ_DECAY_ALERT <= cl:
        msgs.append(f"Liquidation cascade decaying (flush {pl:.0f}->{cl:.0f}) — forced selling looks spent")

    # v3: overheat threshold crossings (the exit-side mirror)
    po, co = prev.get("overheat"), snap.get("overheat")
    if po is not None and co is not None:
        if po < C.OVERHEAT_ALERT <= co:
            msgs.append(f"⚠ OVERHEAT {po:.0f}->{co:.0f} — longs crowded/euphoric; review trims and hedges")
        elif po >= C.OVERHEAT_ALERT > co:
            msgs.append(f"Overheat cooled ({po:.0f}->{co:.0f})")

    # v3: trend-structure events
    t = snap.get("trend") or {}
    if prev.get("golden") is not None and t.get("golden") is not None \
            and prev["golden"] != t["golden"]:
        msgs.append("GOLDEN CROSS — 50d SMA back above 200d" if t["golden"]
                    else "DEATH CROSS — 50d SMA below 200d")
    ca = (t["px_vs_slow"] > 0) if t.get("px_vs_slow") is not None else None
    pa = prev.get("px_above_slow")
    if pa is not None and ca is not None and pa != ca:
        msgs.append("BTC reclaimed the 200d SMA" if ca else "BTC lost the 200d SMA")

    # v3: whale bid into fear — net large-clip buying crosses the line while fearful
    pw = prev.get("whale_net_1h")
    cw = ((snap.get("whales") or {}).get("tape_1h") or {}).get("net")
    if pw is not None and cw is not None and snap.get("fng", 50) <= C.FNG_CONFIRM \
            and pw < C.WHALE_NET_ALERT_USD <= cw:
        msgs.append(f"WHALE BID — net +${cw/1e6:.1f}M in large clips (1h) into fear; size is absorbing")

    # v4: smart-money divergence with PERSISTENCE — the verdict must hold for
    # two consecutive runs before it alerts (a choppy tape flipping
    # neutral<->accumulation would otherwise ping you all day), and the same
    # verdict never re-alerts until it has fully decayed back to neutral.
    div = (((snap.get("whales") or {}).get("summary") or {}).get("divergence") or {})
    ck = div.get("key")
    pk = prev.get("whale_div_key")               # last run's verdict
    already = prev.get("whale_div_alerted")      # last verdict we alerted on
    if (ck in ("smart_accumulation", "smart_distribution")
            and pk == ck and ck != already):
        prefix = "⚠ " if ck == "smart_distribution" else ""
        msgs.append(f"{prefix}SMART MONEY DIVERGENCE (held 2 runs) — {div['text']}")
        snap["_div_alerted"] = ck                # save_state persists this
    return msgs


def next_trigger(snap):
    c, tips = snap["confirms"], []
    if not c["btc_reclaim"]:
        level = snap.get("reclaim_level", C.BTC_RECLAIM_LEVEL)
        gap = (level / snap["btc_price"] - 1) * 100
        tips.append(f"BTC reclaim ${level:,.0f} [{snap.get('reclaim_source', 'manual')}] "
                    f"(needs {gap:+.1f}%)")
    if not c["funding_positive"]:
        avg = snap.get("funding_avg")
        stab = f", 3-period avg {avg*100:.4f}%" if avg is not None else ""
        tips.append(f"funding flip + (now {snap['funding']*100:.4f}%{stab})")
    if not c["fng_above_30"]:
        tips.append(f"F&G above {C.FNG_CONFIRM} (now {snap['fng']})")
    return tips


def format_report(snap):
    l = snap["ladder"]
    liq = ""
    if snap.get("ws_ready") and snap.get("liq_stats") is not None:
        ls = snap["liq_stats"]
        liq = (f", liq-flush {snap['liq_flush']:.0f} "
               f"[15m longs ${ls['long']/1e6:.1f}M / shorts ${ls['short']/1e6:.1f}M]")
    else:
        liq = ", liq-flush warming up"
    mode = "adaptive" if snap.get("adaptive") else "static (building baseline)"
    lines = [
        snap.get("read", ""),
        "",
        f"DEPLOYMENT LADDER -> target {l['target']:.0f}%   [{mode}]",
        f"BTC ${snap['btc_price']:,.0f} | F&G {snap['fng']} ({snap['fng_class']}) | regime {snap['regime']}",
        f"raw desire {l['raw_desire']:.0f}% / macro ceiling {l['macro_ceiling']}% / confirms {l['confirm_count']}/3 (cap {l['confirm_cap']}%)",
        f"structural {snap['structural']:.0f} (funding {snap['funding']*100:.4f}%, OI {snap['oi_change']*100:+.1f}%{liq}) / sentiment {snap['sentiment']:.0f}",
    ]
    # v3: trend anchor + top-risk mirror
    t = snap.get("trend")
    if t:
        bits = []
        if t.get("px_vs_fast") is not None:
            bits.append(f"50d {t['px_vs_fast']:+.1f}%")
        if t.get("px_vs_slow") is not None:
            bits.append(f"200d {t['px_vs_slow']:+.1f}%")
        if t.get("golden") is not None:
            bits.append("golden" if t["golden"] else "death-cross")
        if t.get("range_pos") is not None:
            bits.append(f"20d range {t['range_pos']:.0f}%")
        if t.get("rv30") is not None:
            pct = f" (p{t['rv_pctile']:.0f})" if t.get("rv_pctile") is not None else ""
            bits.append(f"RV30 {t['rv30']:.0f}%{pct}")
        if bits:
            lines.append("Trend: " + " | ".join(bits))
    oh = snap.get("overheat")
    if oh is not None:
        mode = " (adaptive)" if snap.get("overheat_adaptive") else ""
        flag = " ⚠ crowded longs — review trims/hedges" if oh >= C.OVERHEAT_ALERT else ""
        lines.append(f"Risk: overheat {oh:.0f}/100{mode}{flag}")
    w = snap.get("whales")
    if w:
        bits = []
        sm = w.get("summary")
        if w.get("tape_ready") and sm:
            wh_ = sm["whale"]
            if wh_.get("bias") is not None:
                bits.append(f"whales {wh_['direction']} (bias {wh_['bias']:+.2f}, "
                            f"vol ${wh_['vol']/1e6:.1f}M, net {wh_['net']/1e6:+.1f}M /4h)")
            else:
                bits.append("no whale clips in window")
            if sm.get("whale_share") is not None:
                bits.append(f"{sm['whale_share']*100:.0f}% of tape")
            dv = sm.get("divergence")
            if dv and dv["key"] != "neutral":
                bits.append(dv["text"])
        else:
            bits.append("tape warming up")
        if w.get("depth"):
            bits.append(f"book {w['depth']['bid_share']*100:.0f}% bid (±2%)")
        if w.get("crowd_long") is not None:
            bits.append(f"crowd {w['crowd_long']*100:.0f}% long ({w['crowd']})")
        if w.get("absorption"):
            bits.append(w["absorption"])
        lines.append("Whales: " + " | ".join(bits))

    sz = snap.get("sizing")
    if sz:
        line = f"Sizing: {sz['action']} (target {sz['target_pct']:.0f}% vs held {sz['current_pct']:.0f}%, gap {sz['delta_pct']:.0f}pts)"
        if sz.get("delta_usd"):
            pa = ", ".join(f"{k} ${v:,.0f}" for k, v in sz["per_asset"].items())
            line += f" -> ${sz['delta_usd']:,.0f}  [{pa}]"
        if (sz["action"] == "ADD" and t and t.get("rv_pctile") is not None
                and t["rv_pctile"] >= C.RV_STAGE_PCTILE):
            line += f" — vol p{t['rv_pctile']:.0f}: stage the add in 2-3 clips"
        if sz.get("set_age_days") is not None and sz["set_age_days"] >= 3:
            line += f" — held% last set {sz['set_age_days']:.0f}d ago; update if you've traded"
        lines.append(line)
    nt = next_trigger(snap)
    lines.append("Next: " + "; ".join(nt) if nt else "All confirmations ON — full ladder unlocked.")
    track = scorecard.report_line()
    if track:
        lines.append(track)
    if snap.get("degraded"):
        lines.append("⚠ degraded sources (using last known): " + ", ".join(snap["degraded"]))
    lines.append("(leverage BTC only; SOL takes size; catalyst leg small)")
    return "\n".join(lines)


def run_once(force_report=False):
    snap = gather()
    prev = load_state()
    alerts = diff_alerts(prev, snap)
    if alerts or force_report:
        body = format_report(snap)
        if alerts:
            body = "ALERTS:\n- " + "\n- ".join(alerts) + "\n\n" + body
        notify("Signalia", body)
    save_state(snap, prev)
    try:
        store.insert_reading(snap)
        store.set_meta("last_run_ts", time.time())
    except Exception as e:
        print("store error:", e)
    return snap


def run_scan():
    """Market-wide scan + watchlist deep insights. Heavier; run less often."""
    syms = store.get_watchlist()
    scan = scanner.scan_market()
    flags = scanner.watchlist_flags(scan, syms)
    insights = wl.build(syms, LIQ_MONITOR)
    try:    # scan-driven watchlist discovery (recurrence across scans)
        cands = candidates.update(scan, syms)
    except Exception as e:
        print("candidates error:", e)
        cands = None
    now = time.time()
    return {"scan": scan, "watchlist_flags": flags, "insights": insights,
            "candidates": cands,
            "watchlist": syms, "scan_ts": now, "insights_ts": now}


if __name__ == "__main__":
    import sys
    store.init_db()   # app.py inits the DB at boot; standalone runs must do it too
    run_once(force_report="--report" in sys.argv)
