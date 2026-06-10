"""
Signalia — watchlist deep insights.

For each watchlist symbol: adaptive structural score, confirmation triggers,
live liquidation read (if streamed), and distance-from-ATH (CoinGecko). Produces
a focused one-line insight per name — the "where to look" turned into "what's
actually happening here."

The watchlist itself lives in the DB (editable from the dashboard, no restart);
the env var only seeds it. CoinGecko ids for new names are auto-resolved.
"""
import time

import requests

import baseline
import config as C
import signals as S
import store

HEADERS = {"User-Agent": "signalia/2.0"}
TIMEOUT = 15

_ATH_CACHE = {}        # symbol -> (fetched_at, value); ATH moves slowly
_ATH_TTL = 1800        # 30 min — lets insights refresh every engine run without
                       # hammering CoinGecko's free-tier rate limits


def resolve_cg_id(symbol):
    """Best-effort CoinGecko id for a Bybit symbol (DOGEUSDT -> dogecoin)."""
    base = symbol.replace("USDT", "").lower()
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search",
                         params={"query": base}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        coins = r.json().get("coins") or []
        for c in coins:
            if str(c.get("symbol", "")).lower() == base:
                return c.get("id")
        return coins[0].get("id") if coins else None
    except Exception:
        return None


def _ath_distance(symbol):
    """% below all-time high via CoinGecko (cached; watchlist only)."""
    hit = _ATH_CACHE.get(symbol)
    if hit and time.time() - hit[0] < _ATH_TTL:
        return hit[1]
    cid = store.get_cg_map().get(symbol)
    if not cid:
        return None
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": cid},
            headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()[0]
        v = d.get("ath_change_percentage")
        v = round(v, 1) if v is not None else None
        _ATH_CACHE[symbol] = (time.time(), v)
        return v
    except Exception:
        return None


def asset_insight(symbol, liq_monitor=None):
    t = S.bybit_ticker(symbol)
    oi_chg = S.bybit_oi_change(symbol)

    liq_flush = None
    liq_15m = None
    if liq_monitor is not None and symbol in C.SYMBOLS and liq_monitor.ready(C.LIQ_WARMUP_SEC):
        # per-asset attribution: this name's cascades only, not the pooled stream
        liq_flush = S.liq_flush_score(liq_monitor.long_liq_bins(C.LIQ_BINS, symbol))
        liq_15m = liq_monitor.stats(C.LIQ_BIN_SEC, symbol)

    struct = baseline.structural_score(t["funding"], oi_chg, liq_flush)
    confirms = S.confirmation_triggers(t["price"], t["funding"], 50)  # fng n/a per-asset
    ath = _ath_distance(symbol)

    bits = [f"{symbol}: ${t['price']:,.4g}",
            f"funding {t['funding']*100:.4f}%",
            f"OI {oi_chg*100:+.1f}%",
            f"struct {struct:.0f}"]
    if liq_flush is not None:
        bits.append(f"liq-flush {liq_flush:.0f}")
    if liq_15m:
        bits.append(f"longs liq ${liq_15m['long']/1e6:.1f}M/15m")
    if ath is not None:
        mult = (100.0 / (100.0 + ath)) if ath > -100 else None   # x to reclaim ATH
        bits.append(f"{ath:.0f}% vs ATH" + (f" (~{mult:.1f}x)" if mult else ""))
    out = {
        "symbol": symbol, "price": t["price"], "funding": t["funding"],
        "oi_change": round(oi_chg, 4), "structural": struct,
        "liq_flush": liq_flush, "ath_change": ath,
        "line": "  ".join(bits),
    }
    # honesty label: adaptive percentiles grade against the LADDER symbol's
    # stored history, not this asset's own (per-symbol baselines = future work)
    if symbol != C.LADDER_SYMBOL and baseline.using_adaptive():
        out["struct_note"] = f"vs {C.LADDER_SYMBOL.replace('USDT', '')} baseline"
    return out


def build(watchlist, liq_monitor=None):
    out = []
    for sym in watchlist:
        try:
            out.append(asset_insight(sym, liq_monitor))
        except Exception as e:
            out.append({"symbol": sym, "error": str(e), "line": f"{sym}: error {e}"})
    return out
