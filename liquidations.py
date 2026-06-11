"""
Signalia — live liquidation monitor (Bybit All Liquidation WS).

Stream: wss://stream.bybit.com/v5/public/linear
Topic:  allLiquidation.{symbol}   (push every 500ms)
Fields: T=ts(ms), s=symbol, S=side (Buy => a LONG was liquidated), v=size, p=price

Runs in a background thread, keeps a rolling in-memory buffer of liquidation
events, and exposes aggregates the engine reads on its scheduled run.

The signal we want: a SPIKE in long-liquidations that then DECAYS = forced
selling spent = bottom precursor. Escalating liquidations = cascade still live.
"""
import json
import threading
import time
from collections import deque

import websocket  # websocket-client


class LiquidationMonitor:
    def __init__(self, symbols, ws_url, max_age_sec, bin_sec):
        self.symbols = symbols
        self.ws_url = ws_url
        self.max_age = max_age_sec
        self.bin_sec = bin_sec
        self.events = deque()          # (ts_ms, side, notional_usd)
        self.lock = threading.Lock()
        self.connected = False
        self.started_at = None
        self.last_msg_ts = None        # any server traffic (incl. pongs/acks)
        self._ws = None

    # a healthy socket answers our 20s pings, so >90s of total silence means
    # the connection is dead-but-open (or the subscribe never took) — reconnect
    SILENT_SEC = 90

    # -------------------------- WS callbacks --------------------------------
    def _on_open(self, ws):
        self.connected = True
        self.last_msg_ts = time.time()
        args = [f"allLiquidation.{s}" for s in self.symbols]
        ws.send(json.dumps({"op": "subscribe", "args": args}))
        print("liq ws: subscribe sent", args)

    def _on_message(self, ws, msg):
        self.last_msg_ts = time.time()
        try:
            m = json.loads(msg)
        except Exception:
            return
        if m.get("op") == "subscribe" and not m.get("success", True):
            # bad endpoint/topic would otherwise look "connected" with 0 events
            print("liq ws: subscribe REJECTED:", m.get("ret_msg"), "— reconnecting")
            ws.close()
            return
        if m.get("op") in ("pong", "ping") or m.get("ret_msg") == "pong":
            return
        data = m.get("data")
        if not data:
            return
        items = data if isinstance(data, list) else [data]
        with self.lock:
            for it in items:
                try:
                    ts = int(it["T"])
                    sym = it.get("s", "")               # keep the symbol — per-asset reads
                    side = it["S"]                      # Buy => long liquidated
                    notional = float(it["v"]) * float(it["p"])
                    self.events.append((ts, sym, side, notional))
                except Exception:
                    continue
            self._prune()

    def _on_close(self, ws, *a):
        self.connected = False

    def _on_error(self, ws, err):
        self.connected = False
        print("liq ws error:", err)

    # ------------------------------ plumbing --------------------------------
    def _prune(self):
        cutoff = time.time() * 1000 - self.max_age * 1000
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def _ping_loop(self):
        while True:
            time.sleep(20)
            try:
                if self._ws and self.connected:
                    self._ws.send(json.dumps({"op": "ping"}))
                    silent = time.time() - (self.last_msg_ts or 0)
                    if self.last_msg_ts and silent > self.SILENT_SEC:
                        print(f"liq ws: no traffic for {silent:.0f}s — forcing reconnect")
                        self._ws.close()
            except Exception:
                pass

    def _run(self):
        while True:
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print("liq ws run error:", e)
            self.connected = False
            time.sleep(5)               # reconnect backoff

    def start(self):
        self.started_at = time.time()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()

    # ------------------------------ readers ---------------------------------
    def ready(self, warmup_sec):
        """True once the stream has run long enough to trust the flush score."""
        return self.started_at is not None and (time.time() - self.started_at) >= warmup_sec

    def long_liq_bins(self, n_bins, symbol=None):
        """Long-liq notional per time bin, oldest -> newest.
        symbol=None pools every streamed name (the systemic-cascade read the
        ladder wants); pass a symbol for an honest per-asset view."""
        now = time.time() * 1000
        bins = [0.0] * n_bins
        with self.lock:
            for ts, sym, side, notional in self.events:
                if side != "Buy":       # only long liquidations = the selloff cascade
                    continue
                if symbol and sym != symbol:
                    continue
                idx = int((now - ts) / 1000 // self.bin_sec)
                if 0 <= idx < n_bins:
                    bins[n_bins - 1 - idx] += notional
        return bins

    def stats(self, window_sec, symbol=None):
        """Long/short notional + count over the last window_sec seconds."""
        now = time.time() * 1000
        long_n = short_n = 0.0
        cnt = 0
        with self.lock:
            for ts, sym, side, notional in self.events:
                if (now - ts) / 1000 > window_sec:
                    continue
                if symbol and sym != symbol:
                    continue
                cnt += 1
                if side == "Buy":
                    long_n += notional
                else:
                    short_n += notional
        return {"long": long_n, "short": short_n, "count": cnt}
