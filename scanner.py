"""
Signalia — market-wide scanner.

Pulls every Bybit linear (USDT) perp in one call and produces ranked screens
plus a breadth read. The watchlist gets deep per-asset treatment elsewhere;
this is the wide-angle lens that surfaces *where to look*.

Screens:
  - funding_neg : most negative funding (crowded shorts -> squeeze/long fuel)
  - funding_pos : most positive funding (crowded longs -> fragility)
  - losers      : biggest 24h drops among liquid names (cyclical-discount hunting)
  - gainers     : biggest 24h gains among liquid names (momentum/rotation)
  - turnover    : highest 24h turnover (where the volume actually is)
Breadth: share of liquid names green on 24h = risk-on/off temperature.
"""
import requests
import config as C

HEADERS = {"User-Agent": "signalia/2.0"}
TIMEOUT = 20


def _fetch_all_linear():
    r = requests.get(f"{C.BYBIT_BASE}/v5/market/tickers",
                     params={"category": "linear"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["result"]["list"]


def _clean(rows):
    out = []
    for x in rows:
        sym = x.get("symbol", "")
        if not sym.endswith(C.SCAN_QUOTE):
            continue
        try:
            turnover = float(x.get("turnover24h") or 0)
            if turnover < C.SCAN_MIN_TURNOVER:
                continue
            out.append({
                "symbol": sym,
                "price": float(x.get("lastPrice") or 0),
                "chg24h": float(x.get("price24hPcnt") or 0),   # fraction, e.g. -0.07
                "funding": float(x.get("fundingRate") or 0),
                "turnover": turnover,
                "oi": float(x.get("openInterest") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


def _squeeze_rank(rows):
    """
    Squeeze candidates: crowded shorts (negative funding) INTO weakness (down
    hard). Rank by Borda sum of the two ranks — scale-free, no magic weights.
    These are the catalyst-leg names where covering can move price violently.
    """
    cands = [r for r in rows
             if r["funding"] <= C.SQUEEZE_MIN_NEG_FUNDING
             and r["chg24h"] <= C.SQUEEZE_MIN_DROP]
    by_f = sorted(cands, key=lambda r: r["funding"])
    by_c = sorted(cands, key=lambda r: r["chg24h"])
    rank = {r["symbol"]: by_f.index(r) + by_c.index(r) for r in cands}
    return sorted(cands, key=lambda r: rank[r["symbol"]])[:C.SCAN_TOP_N]


def _screens(rows):
    by_funding = sorted(rows, key=lambda r: r["funding"])
    by_chg = sorted(rows, key=lambda r: r["chg24h"])
    by_turn = sorted(rows, key=lambda r: r["turnover"], reverse=True)
    n = C.SCAN_TOP_N
    return {
        "funding_neg": by_funding[:n],
        "funding_pos": by_funding[-n:][::-1],
        "losers": by_chg[:n],
        "gainers": by_chg[-n:][::-1],
        "turnover": by_turn[:n],
        "squeeze": _squeeze_rank(rows),
    }


def breadth(rows):
    if not rows:
        return None
    green = sum(1 for r in rows if r["chg24h"] > 0)
    return round(100.0 * green / len(rows), 1)


def scan_market():
    rows = _clean(_fetch_all_linear())
    return {
        "universe": len(rows),
        "breadth": breadth(rows),
        "screens": _screens(rows),
    }


def watchlist_flags(scan, watchlist):
    """Which watchlist names show up in any extreme screen (so we can highlight)."""
    flags = {}
    for name, lst in scan["screens"].items():
        for r in lst:
            if r["symbol"] in watchlist:
                flags.setdefault(r["symbol"], []).append(name)
    return flags
