"""
Signalia — scaffolded external layers.

These need API keys or paid/scraped feeds, so they ship as clean interfaces with
honest fallbacks. Each returns None (or the manual value) until wired up. None of
these could be live-tested from the build sandbox (domains not reachable there),
so verify each after deploy.
"""
import requests

import config as C

HEADERS = {"User-Agent": "signalia/2.0"}
TIMEOUT = 15


# --------------------------------------------------------------------------
# MACRO auto-suggest (LAYER 1 helper). Suggests a regime; you still confirm.
# Needs a free FRED API key (api.stlouisfed.org). FedWatch has no clean free
# API, so cut-probabilities stay manual.
# --------------------------------------------------------------------------
def suggest_regime():
    if not C.FRED_API_KEY:
        return {"suggested": C.MACRO_REGIME, "source": "manual (no FRED key)"}
    try:
        # DFII10 = 10y real yield. Falling real yields -> easing -> supportive.
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": "DFII10", "api_key": C.FRED_API_KEY,
                                 "file_type": "json", "sort_order": "desc", "limit": 20},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        obs = [float(o["value"]) for o in r.json()["observations"]
               if o["value"] not in (".", "")]
        if len(obs) < 5:
            return {"suggested": C.MACRO_REGIME, "source": "manual (insufficient data)"}
        latest, prior = obs[0], obs[len(obs) // 2]
        # crude rule: real yields clearly falling -> SUPPORTIVE, rising -> HOSTILE
        if latest < prior - 0.10:
            sug = "SUPPORTIVE"
        elif latest > prior + 0.10:
            sug = "HOSTILE"
        else:
            sug = "NEUTRAL"
        return {"suggested": sug, "real_yield_10y": latest, "source": "FRED DFII10"}
    except Exception as e:
        return {"suggested": C.MACRO_REGIME, "source": f"manual (FRED error: {e})"}


# --------------------------------------------------------------------------
# IMPLIED VOL (Deribit DVOL). Tells you when long-dated calls are cheap vs rich.
# --------------------------------------------------------------------------
def dvol(currency="BTC"):
    if not C.DERIBIT_ENABLED:
        return None
    try:
        r = requests.get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                         params={"currency": currency, "resolution": "3600",
                                 "start_timestamp": 0, "end_timestamp": 9999999999999},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()["result"]["data"]
        latest = data[-1][4] if data else None        # close of last candle
        return {"dvol": latest}
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------
# ETF FLOWS. No clean free API — manual override now, scraper later.
# Set ETF_FLOW_OVERRIDE to today's net flow in $millions (e.g. "-350").
# --------------------------------------------------------------------------
def etf_net_flow():
    if C.ETF_FLOW_OVERRIDE.strip():
        try:
            return {"net_flow_musd": float(C.ETF_FLOW_OVERRIDE), "source": "manual override"}
        except ValueError:
            pass
    return {"net_flow_musd": None, "source": "not wired (add Farside/SoSoValue scraper)"}
