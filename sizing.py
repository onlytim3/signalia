"""
Signalia — position-aware sizing.

Turns the ladder's target deployment % into a concrete dollar tranche to add or
trim, split across the assets per ASSET_SPLIT. Pure function; no execution.
You still place every order by hand on Bybit.
"""
import config as C


def tranche(target_pct, current_deployed_pct, capital=None, split=None):
    capital = C.CAPITAL_BASE if capital is None else capital
    split = C.ASSET_SPLIT if split is None else split

    gap = target_pct - current_deployed_pct          # +ve = add, -ve = trim
    action = "ADD" if gap > 0 else ("TRIM" if gap < 0 else "HOLD")
    delta_pct = abs(gap)

    out = {
        "action": action,
        "target_pct": round(target_pct, 1),
        "current_pct": round(current_deployed_pct, 1),
        "delta_pct": round(delta_pct, 1),
        "actionable": delta_pct >= C.MIN_TRANCHE_PCT,
    }
    if capital and capital > 0:
        delta_usd = capital * delta_pct / 100.0
        out["delta_usd"] = round(delta_usd, 2)
        out["per_asset"] = {k: round(delta_usd * w, 2) for k, w in split.items()}
    return out
