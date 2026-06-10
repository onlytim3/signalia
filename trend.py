"""
Signalia — trend & cycle context (daily klines; free public endpoint).

Gives the ladder a price-structure anchor the positioning signals lack:
  - 50d / 200d SMA position + golden/death state (cycle posture)
  - 20d range position (is this dip at range lows or mid-air?)
  - 30d realized vol + percentile (how violent is the tape -> stage entries?)
  - auto reclaim level (50d SMA) for the confirmation trigger when no manual
    BTC_RECLAIM_LEVEL is pinned via env — a static level goes stale.

Math functions are PURE (lists in, numbers out) so selftest covers them offline.
One kline fetch per engine run.
"""
import math

import requests

import config as C

HEADERS = {"User-Agent": "signalia/2.0"}
TIMEOUT = 20


def fetch_daily(symbol, days=None):
    """Daily candles, oldest -> newest. Returns (closes, highs, lows)."""
    days = days or C.KLINE_DAYS
    r = requests.get(f"{C.BYBIT_BASE}/v5/market/kline",
                     params={"category": "linear", "symbol": symbol,
                             "interval": "D", "limit": min(days, 1000)},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()["result"]["list"]          # Bybit returns newest first
    rows.reverse()
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    return closes, highs, lows


# ------------------------------ pure math -------------------------------------
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def realized_vol(closes, window=None):
    """Annualized close-to-close volatility (%) over the last `window` days."""
    window = window or C.RV_WINDOW
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365) * 100


def rv_percentile(closes, window=None):
    """Today's realized vol vs every prior window in the data (0..100)."""
    window = window or C.RV_WINDOW
    series = [realized_vol(closes[:i], window)
              for i in range(window + 1, len(closes) + 1)]
    series = [s for s in series if s is not None]
    if len(series) < 20:
        return None
    cur = series[-1]
    return round(100.0 * sum(1 for s in series if s <= cur) / len(series), 0)


def range_position(highs, lows, price, days=None):
    """Where price sits in the N-day high/low range: 0 = at lows, 100 = at highs."""
    days = days or C.RANGE_DAYS
    if len(highs) < days or len(lows) < days:
        return None
    hi, lo = max(highs[-days:]), min(lows[-days:])
    if hi == lo:
        return 50.0
    return round(100.0 * (price - lo) / (hi - lo), 1)


def build_context(closes, highs, lows, price):
    """Assemble the trend dict. 0.0 is a legitimate value — guard with `is not None`."""
    s_fast = sma(closes, C.SMA_FAST)
    s_slow = sma(closes, C.SMA_SLOW)
    rv = realized_vol(closes)

    def pct_vs(s):
        return round((price / s - 1) * 100, 1) if s is not None and s > 0 else None

    return {
        "sma_fast": round(s_fast) if s_fast is not None else None,
        "sma_slow": round(s_slow) if s_slow is not None else None,
        "px_vs_fast": pct_vs(s_fast),
        "px_vs_slow": pct_vs(s_slow),
        "golden": (s_fast > s_slow) if (s_fast is not None and s_slow is not None) else None,
        "range_pos": range_position(highs, lows, price),
        "rv30": round(rv, 1) if rv is not None else None,
        "rv_pctile": rv_percentile(closes),
        "reclaim_auto": round(s_fast) if s_fast is not None else None,
    }


def context(symbol, price):
    """Fetch + build; None on any failure (engine marks the source degraded)."""
    try:
        closes, highs, lows = fetch_daily(symbol)
        if len(closes) < C.RANGE_DAYS:
            return None
        return build_context(closes, highs, lows, price)
    except Exception:
        return None
