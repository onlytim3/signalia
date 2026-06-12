"""
Signalia — scan-driven watchlist discovery.

The scanner already ranks the whole market every 30 minutes; this module makes
that intelligence cumulative. Every extreme-screen appearance is logged, and
names are scored on RECURRENCE — per-screen weights × a recency half-life —
because persistence is the signal: one appearance is a pump, a name living in
the squeeze screen across days is a setup.

Names clearing the bar are suggested (alert + /candidates in Telegram) with a
one-tap /watch. Auto-add is opt-in (WATCHLIST_AUTO=1) and only fires while the
watchlist has room — the human confirm is the discipline, same probation
philosophy as every other layer.

Pure math lives in score()/rank() so selftest covers it offline; store/alert
plumbing stays in observe()/update().
"""
import json
import time

import config as C
import store
from notifier import notify


# ------------------------------ pure scoring -----------------------------------
def score(hits, now=None):
    """One symbol's hits -> {score, hits, buckets, screens} or None.
    Each hit is worth its screen weight, halved every CAND_HALF_LIFE_H hours;
    `buckets` counts distinct 6h windows hit (the anti-one-off-pump gate)."""
    if not hits:
        return None
    now = now or time.time()
    total, screens, buckets = 0.0, {}, set()
    for h in hits:
        age_h = max(0.0, (now - h["ts"]) / 3600)
        w = C.CAND_SCREEN_W.get(h["screen"], 0.0) * 0.5 ** (age_h / C.CAND_HALF_LIFE_H)
        total += w
        screens[h["screen"]] = screens.get(h["screen"], 0) + 1
        buckets.add(int(h["ts"] // 21600))
    return {"score": round(total, 2), "hits": len(hits),
            "buckets": len(buckets), "screens": screens}


def rank(all_hits, watchlist, now=None, top_n=None):
    """Score every non-watchlist symbol seen in the window, best first.
    Suggestion gate: enough hits, spread over enough distinct windows,
    score over the line."""
    exclude = set(watchlist or []) | C.CAND_EXCLUDE
    by_sym = {}
    for h in all_hits:
        if h["symbol"] not in exclude:
            by_sym.setdefault(h["symbol"], []).append(h)
    out = []
    for sym, hits in by_sym.items():
        s = score(hits, now)
        latest = max(hits, key=lambda h: h["ts"])
        s.update({
            "symbol": sym,
            "chg24h": latest["chg24h"], "funding": latest["funding"],
            "turnover": latest["turnover"],
            "suggest": (s["hits"] >= C.CAND_MIN_HITS
                        and s["buckets"] >= C.CAND_MIN_BUCKETS
                        and s["score"] >= C.CAND_SUGGEST_SCORE),
        })
        s["reason"] = reason(s)
        out.append(s)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_n or C.CAND_TOP_N]


def reason(c):
    """One human line: why this name is on the radar."""
    screens = " ".join(f"{k}×{v}" for k, v in
                       sorted(c["screens"].items(), key=lambda kv: -kv[1]))
    days = max(1, c["buckets"]) * 0.25
    return (f"{c['hits']} screen hit{'s' if c['hits'] != 1 else ''} "
            f"over ~{days:.1f}d ({screens}) · "
            f"{c['chg24h'] * 100:+.1f}% 24h · funding {c['funding'] * 100:+.4f}% · "
            f"${c['turnover'] / 1e6:.0f}M turnover")


# ------------------------------ plumbing ---------------------------------------
def observe(scan, watchlist):
    """Log this scan's extreme-screen appearances for non-watchlist names."""
    ts = time.time()
    skip = set(watchlist or []) | C.CAND_EXCLUDE
    hits = []
    for screen, rows in (scan.get("screens") or {}).items():
        if screen not in C.CAND_SCREEN_W:
            continue
        for r in rows:
            if r["symbol"] not in skip:
                hits.append((ts, r["symbol"], screen,
                             r.get("chg24h"), r.get("funding"), r.get("turnover")))
    store.add_candidate_hits(hits)


def _alert_new(suggestions, watchlist):
    """Suggest (or auto-add) fresh qualifiers; per-symbol cooldown so the same
    name can't nag every scan."""
    try:
        seen = json.loads(store.get_meta("cand_alerted") or "{}")
    except Exception:
        seen = {}
    now, cooldown = time.time(), C.CAND_ALERT_COOLDOWN_D * 86400
    for s in suggestions:
        sym = s["symbol"]
        if now - float(seen.get(sym, 0)) < cooldown:
            continue
        seen[sym] = now
        if C.WATCHLIST_AUTO and len(watchlist) < 12:
            watchlist.append(sym)
            store.set_watchlist(watchlist)
            notify(f"Signalia — auto-watching {sym.replace('USDT', '')}",
                   s["reason"] + "\n\n(added by WATCHLIST_AUTO; /unwatch to drop)")
        else:
            notify(f"Signalia — watchlist candidate: {sym.replace('USDT', '')}",
                   s["reason"] + f"\n\nreply /watch {sym.replace('USDT', '')} to track it")
    # keep the memory bounded to names still inside the cooldown window
    store.set_meta("cand_alerted",
                   json.dumps({k: v for k, v in seen.items() if now - v < cooldown}))


def update(scan, watchlist):
    """Called from every routine scan: log appearances, re-rank, surface new
    qualifiers. Returns the ranked board for /scan payloads and the bot."""
    observe(scan, watchlist)
    board = rank(store.candidate_hits(C.CAND_WINDOW_DAYS), watchlist)
    _alert_new([c for c in board if c["suggest"]], list(watchlist or []))
    return board
