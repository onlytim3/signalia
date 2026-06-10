"""
Signalia — model scorecard (the model grades its own calls).

Every stored reading is an implicit prediction: high structural says a bounce
is coming, high overheat says a drawdown risks, whale accumulation says a
bottom is forming. This module checks those claims against what price ACTUALLY
did next — forward returns at several horizons, bucketed by signal state.

Zero new data: the readings table already holds price + every score. Honesty
rules: every bucket reports its sample size, n < MIN_N is labeled
low-confidence instead of hidden, and bucket medians are compared against the
SAME-period baseline median (so a drifting market can't fake an edge).
Pure math on rows -> selftest covers it offline. Useless on day one,
informative by month two — it improves itself every run.
"""
import time

import store

HORIZONS = {"4h": 4 * 3600, "1d": 86400, "3d": 3 * 86400}
MIN_N = 10          # below this a bucket is shown but flagged low-confidence

# (key, label, predicate on a reading row, thesis direction)
BUCKETS = [
    ("struct_hi",   "structural ≥ 70 — washed out",
     lambda r: r.get("structural") is not None and r["structural"] >= 70, "up"),
    ("struct_lo",   "structural ≤ 35 — not exhausted",
     lambda r: r.get("structural") is not None and r["structural"] <= 35, "down"),
    ("deep_fear",   "Fear & Greed ≤ 20 — deep fear",
     lambda r: r.get("fng") is not None and r["fng"] <= 20, "up"),
    ("greed",       "Fear & Greed ≥ 65 — greed",
     lambda r: r.get("fng") is not None and r["fng"] >= 65, "down"),
    ("overheat_hi", "overheat ≥ 60 — crowded longs",
     lambda r: r.get("overheat") is not None and r["overheat"] >= 60, "down"),
    ("whale_acc",   "whale bias ≥ +0.15 — accumulating",
     lambda r: r.get("whale_bias") is not None and r["whale_bias"] >= 0.15, "up"),
    ("whale_dist",  "whale bias ≤ −0.15 — distributing",
     lambda r: r.get("whale_bias") is not None and r["whale_bias"] <= -0.15, "down"),
    ("liq_spent",   "liq flush ≥ 60 — cascade spent",
     lambda r: r.get("liq_flush") is not None and r["liq_flush"] >= 60, "up"),
]


# ------------------------------ pure math -------------------------------------
def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def forward_returns(rows, horizon_sec):
    """
    For each reading, the % move to the first reading >= horizon_sec later.
    rows: oldest -> newest dicts with ts + btc_price. Readings too young to
    have a matured future point are excluded (no peeking, no padding).
    Returns [(row, fwd_pct), ...].
    """
    out = []
    n = len(rows)
    j = 0
    for i, r in enumerate(rows):
        target_ts = r["ts"] + horizon_sec
        if j <= i:
            j = i + 1
        while j < n and rows[j]["ts"] < target_ts:
            j += 1
        if j >= n:
            break                                    # this and all later rows immature
        p0, p1 = r.get("btc_price"), rows[j].get("btc_price")
        if p0 and p1:
            out.append((r, (p1 / p0 - 1) * 100))
    return out


def grade(rows, horizons=None, min_n=MIN_N):
    """Bucketed forward-return report: {horizons: {name: {baseline, buckets}}}."""
    horizons = horizons or HORIZONS
    out = {"n_readings": len(rows), "min_n": min_n, "horizons": {}}
    for hname, hsec in horizons.items():
        pairs = forward_returns(rows, hsec)
        if not pairs:
            out["horizons"][hname] = {"matured": 0, "baseline_median": None, "buckets": []}
            continue
        base = _median([f for _, f in pairs])
        buckets = []
        for key, label, pred, direction in BUCKETS:
            fs = [f for r, f in pairs if pred(r)]
            if not fs:
                continue
            med = _median(fs)
            hits = sum(1 for f in fs if (f > 0) == (direction == "up"))
            buckets.append({
                "key": key, "label": label, "direction": direction,
                "n": len(fs),
                "median_pct": round(med, 2),
                "hit_rate": round(100.0 * hits / len(fs)),
                "edge_vs_base": round(med - base, 2),
                "confident": len(fs) >= min_n,
            })
        out["horizons"][hname] = {"matured": len(pairs),
                                  "baseline_median": round(base, 2),
                                  "buckets": buckets}
    return out


# ------------------------------ wiring ----------------------------------------
def build(limit=2000):
    """Scorecard over the most recent stored readings (~21 days at 15-min cadence)."""
    sc = grade(store.recent_rows(limit))
    sc["as_of"] = time.time()
    return sc


def report_line(horizon="1d"):
    """One compact evidence line for reports — only once something is confident."""
    try:
        sc = build()
    except Exception:
        return None
    h = sc["horizons"].get(horizon) or {}
    confident = [b for b in h.get("buckets", []) if b["confident"]]
    if not confident:
        return None
    best = max(confident, key=lambda b: abs(b["edge_vs_base"]))
    sign = "+" if best["median_pct"] > 0 else ""
    return (f"Track record ({horizon}): {best['label']} → median {sign}{best['median_pct']}% "
            f"(n={best['n']}, {best['hit_rate']}% hit; baseline {h['baseline_median']:+}%)")
