"""
Signalia — live liquidation monitor (multi-venue WS).

Home venue is Bybit (allLiquidation.{symbol}, already in the repo's
"Buy = a LONG was liquidated" convention). Bybit's WS edges go dark from some
hosts' networks, so the monitor takes a list of venue specs (venues.py),
rotates when a connection proves undeliverable, and remembers the venue that
last delivered so restarts reconnect straight to the winner.

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

import store
import venues

websocket.setdefaulttimeout(15)   # connect/handshake can't hang a reconnect loop


class LiquidationMonitor:
    KIND = "liq"
    # a healthy connection refreshes last_msg_ts via data, app pongs, or
    # protocol pongs every ~20s — so >90s of total silence means the feed is
    # dead-but-open (or the subscribe never took) and we reconnect
    SILENT_SEC = 90

    def __init__(self, symbols, ws_source, max_age_sec, bin_sec):
        self.symbols = symbols
        # ws_source: one URL, a list of URLs (treated as Bybit mirrors), or a
        # list of venue specs — see venues.py
        self.specs = self._normalize(ws_source)
        self._spec_i = 0
        self._got_frame = False        # this connection proved real delivery
        self.max_age = max_age_sec
        self.bin_sec = bin_sec
        self.events = deque()          # (ts_ms, symbol, side, notional_usd)
        self.lock = threading.Lock()
        self.connected = False
        self.started_at = None
        self.last_msg_ts = None        # any server traffic (incl. pongs/acks)
        self._ws = None
        self._recall_winner()

    # ------------------------- venue spec plumbing --------------------------
    def _normalize(self, src):
        if isinstance(src, str):
            return [venues.bybit(self.KIND, self.symbols, src)]
        return [venues.bybit(self.KIND, self.symbols, u) if isinstance(u, str)
                else u for u in src]

    @property
    def spec(self):
        return self.specs[self._spec_i % len(self.specs)]

    @property
    def active_url(self):
        return self.spec["url"]

    @property
    def active_venue(self):
        return self.spec["name"]

    def _recall_winner(self):
        """Start the hunt at whichever venue delivered last time."""
        try:
            saved = store.get_meta(f"ws_winner_{self.KIND}")
        except Exception:
            return
        for i, s in enumerate(self.specs):
            if saved == f"{s['name']}|{s['url']}":
                self.specs.insert(0, self.specs.pop(i))
                return

    def _save_winner(self):
        try:
            store.set_meta(f"ws_winner_{self.KIND}",
                           f"{self.spec['name']}|{self.spec['url']}")
        except Exception:
            pass

    # -------------------------- WS callbacks --------------------------------
    def _on_open(self, ws):
        self.connected = True
        self.last_msg_ts = time.time()
        ws.send(self.spec["subscribe"])
        print(f"{self.KIND} ws: subscribe sent via {self.active_venue} "
              f"({self.active_url})")

    def _on_message(self, ws, msg):
        self.last_msg_ts = time.time()
        try:
            m = json.loads(msg)
        except Exception:
            return
        spec = self.spec
        if spec["rejected"](m):
            print(f"{self.KIND} ws: subscribe REJECTED by {self.active_venue}: "
                  f"{str(m)[:160]} — rotating")
            self._got_frame = False    # a rejection is not delivery
            ws.close()
            return
        # "alive" frames (data or app-level pongs) prove the subscription is
        # really delivering — a mere connect ack doesn't (geo-blocked edges
        # often ack and then go silent forever)
        if not self._got_frame and spec["alive"](m):
            self._got_frame = True
            self._save_winner()
        events = spec["parse"](m)
        if not events:
            return
        with self.lock:
            self.events.extend(events)
            self._prune()

    def _on_pong(self, ws, *a):
        self.last_msg_ts = time.time()     # protocol pongs count as traffic

    def _on_close(self, ws, *a):
        self.connected = False

    def _on_error(self, ws, err):
        self.connected = False
        print(f"{self.KIND} ws error ({self.active_venue}):", err)

    # ------------------------------ plumbing --------------------------------
    def _prune(self):
        cutoff = time.time() * 1000 - self.max_age * 1000
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def _ping_loop(self):
        while True:
            time.sleep(20)
            ws = self._ws
            if not (ws and self.connected):
                continue
            # staleness check FIRST: on a zombie connection send() can raise
            # forever, and a swallowed send error must never mask a dead feed
            silent = time.time() - self.last_msg_ts if self.last_msg_ts else 0
            if silent > self.SILENT_SEC:
                print(f"{self.KIND} ws: no traffic for {silent:.0f}s "
                      f"({self.active_venue}) — forcing reconnect")
                self._force_close(ws)
                continue
            payload = self.spec.get("ping")
            if not payload:
                continue               # venue keeps fresh via protocol pongs
            try:
                ws.send(payload)
            except Exception as e:
                print(f"{self.KIND} ws: ping send failed:", e, "— forcing reconnect")
                self._force_close(ws)

    def _force_close(self, ws):
        self.connected = False          # don't trust on_close to fire on a zombie
        try:
            ws.close()
        except Exception:
            pass

    def _run(self):
        while True:
            spec = self.spec
            self._got_frame = False
            try:
                self._ws = websocket.WebSocketApp(
                    spec["url"],
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                    on_pong=self._on_pong,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"{self.KIND} ws run error:", e)
            self.connected = False
            if not self._got_frame and len(self.specs) > 1:
                self._spec_i += 1       # venue never delivered — hunt on
                print(f"{self.KIND} ws: {spec['name']} ({spec['url']}) delivered "
                      f"nothing — rotating to {self.active_venue}")
            time.sleep(5)               # reconnect backoff

    def start(self):
        self.started_at = time.time()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()

    # ------------------------------ readers ---------------------------------
    def healthy(self):
        """Connected AND actually receiving traffic. Data/pong frames refresh
        last_msg_ts every ~20s on a live socket, so prolonged silence = dead
        feed even if the TCP connection still looks open."""
        return (self.connected and self.last_msg_ts is not None
                and (time.time() - self.last_msg_ts) < self.SILENT_SEC + 30)

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
