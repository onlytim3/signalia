"""
Signalia — data fetchers and scoring.

Scoring functions are PURE (numbers in, numbers out) so they can be unit-tested
and self-tested with no network. Fetchers hit only public endpoints.
"""
import requests
import config as C

HEADERS = {"User-Agent": "signalia/1.0"}
TIMEOUT = 15


def _get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# --------------------------- Data fetchers -----------------------------------
def bybit_ticker(symbol):
    """Current price, funding rate, and open interest for a linear perp."""
    j = _get(f"{C.BYBIT_BASE}/v5/market/tickers",
             {"category": "linear", "symbol": symbol})
    row = j["result"]["list"][0]
    return {
        "price": float(row["lastPrice"]),
        "funding": float(row["fundingRate"]),
        "oi": float(row["openInterest"]),
        "chg24h": float(row.get("price24hPcnt") or 0),
    }


def bybit_oi_change(symbol):
    """% change in open interest over the lookback window (newest vs oldest)."""
    j = _get(f"{C.BYBIT_BASE}/v5/market/open-interest",
             {"category": "linear", "symbol": symbol,
              "intervalTime": C.OI_INTERVAL, "limit": C.OI_LOOKBACK + 1})
    rows = j["result"]["list"]            # newest first
    if len(rows) < 2:
        return 0.0
    newest, oldest = float(rows[0]["openInterest"]), float(rows[-1]["openInterest"])
    return 0.0 if oldest == 0 else (newest - oldest) / oldest


def bybit_funding_history(symbol, n=3):
    """Mean of the last n SETTLED funding rates (stability check for confirms)."""
    j = _get(f"{C.BYBIT_BASE}/v5/market/funding/history",
             {"category": "linear", "symbol": symbol, "limit": n})
    vals = [float(r["fundingRate"]) for r in j["result"]["list"]]
    return sum(vals) / len(vals) if vals else None


def fear_greed():
    j = _get("https://api.alternative.me/fng/", {"limit": 1})
    d = j["data"][0]
    return int(d["value"]), d["value_classification"]


def btc_dominance():
    j = _get("https://api.coingecko.com/api/v3/global")
    return float(j["data"]["market_cap_percentage"]["btc"])


# ------------------------------ Scoring --------------------------------------
def _lin(x, x0, y0, x1, y1):
    """Linear map x in [x0,x1] -> [y0,y1], clamped to the endpoints."""
    if x1 == x0:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def funding_score(funding):
    return _lin(funding, C.FUNDING_MAX_NEG, 100.0, C.FUNDING_MAX_POS, 0.0)


def oi_flush_score(oi_change):
    return _lin(oi_change, C.OI_DROP_FULL, 100.0, C.OI_RISE_ZERO, 0.0)


def liq_flush_score(bins):
    """
    Long-liquidation notional per bin (oldest -> newest) -> 0..100.
    High = a cascade happened and is now DECAYING (forced selling spent).
    0    = no cascade, or cascade still escalating (peak is the latest bin).
    """
    peak = max(bins) if bins else 0.0
    if peak <= 0:
        return 0.0
    if bins.index(peak) == len(bins) - 1:
        return 0.0                       # peak is right now -> still escalating
    latest = bins[-1]
    return round(max(0.0, 1.0 - latest / peak) * 100, 1)


def structural_score(funding, oi_change, liq_flush=None):
    """
    Blend funding + OI-flush + live-liquidation-flush. If the WS stream isn't
    ready (liq_flush is None), redistribute its weight to funding + OI so the
    score stays well-formed instead of being dragged down by a missing feed.
    """
    fs = funding_score(funding)
    ois = oi_flush_score(oi_change)
    if liq_flush is None:
        tot = C.W_FUNDING + C.W_OI_FLUSH
        return (C.W_FUNDING / tot) * fs + (C.W_OI_FLUSH / tot) * ois
    return C.W_FUNDING * fs + C.W_OI_FLUSH * ois + C.W_LIQ_FLUSH * liq_flush


def sentiment_score(fng):
    """Extreme fear -> high accumulation score (contrarian)."""
    return float(max(0, min(100, 100 - fng)))


def overheat_score(funding, fng, px_vs_slow=None, oi_change=0.0):
    """
    Mirror of the bottom detector: how crowded/euphoric is the LONG side?
    0 = calm, 100 = textbook top conditions. Returns (score, parts) where parts
    are the 0..100 components. px_vs_slow is % above the slow SMA; when trend
    data is unavailable (None) its weight redistributes — same pattern as the
    liq-flush fallback in structural_score.
    """
    parts = {
        "funding": _lin(funding, 0.0, 0.0, C.OH_FUNDING_FULL, 100.0),
        "fng": _lin(fng, C.OH_FNG_FLOOR, 0.0, C.OH_FNG_FULL, 100.0),
        "oi": _lin(oi_change, 0.0, 0.0, C.OH_OI_FULL, 100.0),
    }
    weights = {"funding": C.W_OH_FUNDING, "fng": C.W_OH_FNG, "oi": C.W_OH_OI}
    if px_vs_slow is not None:
        parts["extension"] = _lin(px_vs_slow,
                                  C.OH_EXT_FLOOR * 100, 0.0,
                                  C.OH_EXT_FULL * 100, 100.0)
        weights["extension"] = C.W_OH_EXT
    total = sum(weights.values())
    score = sum(parts[k] * w for k, w in weights.items()) / total
    return round(score, 1), {k: round(v) for k, v in parts.items()}


def macro_ceiling(regime):
    return C.MACRO_CEILING.get(regime, 40)


def confirmation_triggers(btc_price, funding, fng, reclaim_level=None, funding_avg=None):
    """
    reclaim_level: dynamic level (e.g. 50d SMA) — falls back to the config constant.
    funding_avg: mean of recent settled rates; when given, the funding confirm
    needs BOTH current and average positive, so one flicker can't flip the cap.
    """
    level = reclaim_level if reclaim_level else C.BTC_RECLAIM_LEVEL
    return {
        "btc_reclaim": btc_price >= level,
        "funding_positive": funding > 0 and (funding_avg is None or funding_avg > 0),
        "fng_above_30": fng >= C.FNG_CONFIRM,
    }


def compute_ladder(structural, sentiment, regime, confirms):
    """
    Deployment target = min of three independent gates:
      raw desire (structure + sentiment), macro ceiling, confirmation cap.
    All three must permit before you scale up. That's the whole discipline.
    """
    raw = C.W_STRUCTURAL * structural + C.W_SENTIMENT * sentiment
    ceiling = macro_ceiling(regime)
    n = sum(1 for v in confirms.values() if v)
    cap = C.CONFIRM_CAPS.get(n, 100)
    target = min(raw, ceiling, cap)
    return {
        "raw_desire": round(raw, 1),
        "macro_ceiling": ceiling,
        "confirm_count": n,
        "confirm_cap": cap,
        "target": round(target, 1),
    }
