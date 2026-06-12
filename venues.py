"""
Signalia — venue adapters for the public market-data websockets.

Bybit is the home venue, but its WS edges (CloudFront) go dark from some
hosts' networks — accepting handshakes while never delivering a frame, or
refusing outright — while other venues stream fine from the same box. So the
monitors take a list of venue SPECS and rotate until one delivers; the winner
is remembered (store meta) so restarts reconnect straight to it.

Each spec is a plain dict:
    name        venue label for /health and logs
    url         wss endpoint
    subscribe   JSON string sent on open
    ping        JSON string for the 20s keepalive (None = rely on protocol
                pings; the monitors refresh freshness via pong frames too)
    rejected    fn(msg_dict) -> True when the venue refused the subscription
    alive       fn(msg_dict) -> True for frames proving real delivery (data or
                app-level pongs — a mere connect ack doesn't count, because
                geo-blocked edges often ack and then go silent forever)
    parse       fn(msg_dict) -> list of normalized events

Normalized event shapes (the repo's internal convention):
    liq:   (ts_ms, symbol, side, notional)   side "Buy" = a LONG was liquidated
    trade: (ts_ms, side, notional)           side = taker direction Buy/Sell

Liquidation side mapping is the part worth double-checking per venue: a long
liquidation is a forced SELL order, so venues reporting the order side need
inverting into the repo's "Buy = long died" convention (Bybit's stream already
arrives that way).
"""
import json

import config as C


# ------------------------------ Bybit ------------------------------------------
def _bybit_rejected(m):
    return m.get("op") == "subscribe" and not m.get("success", True)


def _bybit_liq_parse(m):
    if not str(m.get("topic", "")).startswith("allLiquidation."):
        return []
    out = []
    for it in (m.get("data") or []):
        try:
            out.append((int(it["T"]), it.get("s", ""), it["S"],
                        float(it["v"]) * float(it["p"])))
        except Exception:
            continue
    return out


def _bybit_trade_parse(m):
    if not str(m.get("topic", "")).startswith("publicTrade."):
        return []
    out = []
    for it in (m.get("data") or []):
        try:
            out.append((int(it["T"]), it["S"], float(it["v"]) * float(it["p"])))
        except Exception:
            continue
    return out


def bybit(kind, symbols, url=None):
    topic = "allLiquidation" if kind == "liq" else "publicTrade"
    return {
        "name": "bybit", "url": url or C.BYBIT_WS_CANDIDATES[0],
        "subscribe": json.dumps({"op": "subscribe",
                                 "args": [f"{topic}.{s}" for s in symbols]}),
        "ping": json.dumps({"op": "ping"}),
        "rejected": _bybit_rejected,
        "alive": lambda m: (m.get("op") == "pong" or m.get("ret_msg") == "pong"
                            or bool(m.get("data"))),
        "parse": _bybit_liq_parse if kind == "liq" else _bybit_trade_parse,
    }


# ------------------------------ Kraken Futures ---------------------------------
# One public feed carries the whole tape; liquidations arrive as ordinary
# trades with type == "liquidation". PF_* are the linear USD perps (qty in
# base units, so notional = qty * price). trade_snapshot on subscribe gives
# ~100 recent prints — an instant warm start for the rolling windows.
def _kraken_product(sym):
    base = sym[:-4] if sym.endswith("USDT") else sym
    return "PF_XBTUSD" if base == "BTC" else f"PF_{base}USD"


def _kraken_events(m, prod_map):
    if m.get("feed") == "trade":
        items = [m]
    elif m.get("feed") == "trade_snapshot":
        items = m.get("trades") or []
    else:
        return []
    out = []
    for t in items:
        sym = prod_map.get(t.get("product_id"))
        if sym is None:
            continue
        try:
            out.append((int(t["time"]), sym, t["side"], t["type"],
                        float(t["qty"]) * float(t["price"])))
        except Exception:
            continue
    return out


def _kraken_rejected(m):
    return m.get("event") == "error" or (m.get("event") == "alert"
                                         and "error" in str(m).lower())


def kraken(kind, symbols):
    prod_map = {_kraken_product(s): s for s in symbols}

    def parse_liq(m):
        # forced SELL closes a long -> repo convention "Buy" = long liquidated
        return [(ts, sym, "Buy" if side == "sell" else "Sell", notional)
                for ts, sym, side, typ, notional in _kraken_events(m, prod_map)
                if typ == "liquidation"]

    def parse_trade(m):
        return [(ts, "Buy" if side == "buy" else "Sell", notional)
                for ts, _sym, side, _typ, notional in _kraken_events(m, prod_map)]

    return {
        "name": "kraken", "url": "wss://futures.kraken.com/ws/v1",
        "subscribe": json.dumps({"event": "subscribe", "feed": "trade",
                                 "product_ids": sorted(prod_map)}),
        "ping": None,
        "rejected": _kraken_rejected,
        "alive": lambda m: m.get("feed") in ("trade", "trade_snapshot"),
        "parse": parse_liq if kind == "liq" else parse_trade,
    }


# ------------------------------ Binance USD-M futures --------------------------
# Geo-blocked from US hosts (connects, acks, never streams) but the best
# fallback everywhere else, so it rides at the back of the hunt order.
def _binance_liq_parse(m):
    if m.get("e") != "forceOrder":
        return []
    o = m.get("o") or {}
    try:
        side = "Buy" if o["S"] == "SELL" else "Sell"   # forced SELL = long died
        return [(int(o["T"]), o.get("s", ""), side,
                 float(o["q"]) * float(o.get("ap") or o["p"]))]
    except Exception:
        return []


def _binance_trade_parse(m):
    if m.get("e") != "aggTrade":
        return []
    try:   # m["m"]: buyer is maker -> the taker sold
        return [(int(m["T"]), "Sell" if m["m"] else "Buy",
                 float(m["p"]) * float(m["q"]))]
    except Exception:
        return []


def binance(kind, symbols):
    suffix = "forceOrder" if kind == "liq" else "aggTrade"
    return {
        "name": "binance", "url": "wss://fstream.binance.com/ws",
        "subscribe": json.dumps({"method": "SUBSCRIBE", "id": 1,
                                 "params": [f"{s.lower()}@{suffix}" for s in symbols]}),
        "ping": None,
        "rejected": lambda m: bool(m.get("error")),
        "alive": lambda m: bool(m.get("e")),   # the connect ack doesn't count
        "parse": _binance_liq_parse if kind == "liq" else _binance_trade_parse,
    }


# ------------------------------ hunt orders ------------------------------------
def liq_specs(symbols):
    return ([bybit("liq", symbols, u) for u in C.BYBIT_WS_CANDIDATES]
            + [kraken("liq", symbols), binance("liq", symbols)])


def tape_specs(symbols):
    return ([bybit("trade", symbols, u) for u in C.BYBIT_WS_CANDIDATES]
            + [kraken("trade", symbols), binance("trade", symbols)])
