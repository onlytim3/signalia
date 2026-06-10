"""
Signalia — adaptive thresholds (rolling-percentile scoring).

The fix for hardcoded magic numbers: score each signal by its PERCENTILE against
its own recent history, so "extreme" is defined relative to the current regime
rather than an absolute cut that's only right for one volatility environment.

Falls back to the static _lin mappings in signals.py until enough history exists.
"""
import config as C
import signals as S
import store


def _pctile_low_is_bullish(value, history):
    """
    Score 0..100 where LOWER values rank HIGHER (bullish).
    Used for funding (deeply negative = crowded shorts = fuel) and OI change
    (sharp drop = forced selling spent). Percentile = share of history >= value.
    """
    if not history:
        return None
    n = len(history)
    ge = sum(1 for h in history if h >= value)
    return round(100.0 * ge / n, 1)


def funding_score(funding):
    hist = store.recent_values("funding", C.BASELINE_DAYS)
    if len(hist) >= C.BASELINE_MIN_SAMPLES:
        s = _pctile_low_is_bullish(funding, hist)
        if s is not None:
            return s
    return S.funding_score(funding)          # static fallback


def oi_flush_score(oi_change):
    hist = store.recent_values("oi_change", C.BASELINE_DAYS)
    if len(hist) >= C.BASELINE_MIN_SAMPLES:
        s = _pctile_low_is_bullish(oi_change, hist)
        if s is not None:
            return s
    return S.oi_flush_score(oi_change)        # static fallback


def structural_score(funding, oi_change, liq_flush=None):
    """Same blend as signals.structural_score but with adaptive sub-scores."""
    fs = funding_score(funding)
    ois = oi_flush_score(oi_change)
    if liq_flush is None:
        tot = C.W_FUNDING + C.W_OI_FLUSH
        return round((C.W_FUNDING / tot) * fs + (C.W_OI_FLUSH / tot) * ois, 1)
    return round(C.W_FUNDING * fs + C.W_OI_FLUSH * ois + C.W_LIQ_FLUSH * liq_flush, 1)


def _pctile_high_is_hot(value, history):
    """Score 0..100 where HIGHER values rank HIGHER (danger). Share of history <= value."""
    if not history:
        return None
    n = len(history)
    le = sum(1 for h in history if h <= value)
    return round(100.0 * le / n, 1)


def overheat_adaptive(raw):
    """
    Percentile the raw overheat composite against its own rolling history —
    'crowded' is relative to the current regime, same logic as the structural
    side. Returns (score, is_adaptive); static passthrough until enough
    samples exist. The DB stores the RAW composite (see store.insert_reading)
    so the baseline never feeds on its own percentiles.
    """
    if raw is None:
        return None, False
    hist = store.recent_values("overheat", C.BASELINE_DAYS)
    if len(hist) >= C.BASELINE_MIN_SAMPLES:
        s = _pctile_high_is_hot(raw, hist)
        if s is not None:
            return s, True
    return raw, False


def using_adaptive():
    """True if we have enough history to be scoring adaptively (for display)."""
    return len(store.recent_values("funding", C.BASELINE_DAYS)) >= C.BASELINE_MIN_SAMPLES
