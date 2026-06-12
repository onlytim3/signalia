"""
Signalia — whale flow (free, venue-level evidence; no labeled on-chain data).

Honest scope: this measures whale BEHAVIOR on the venue you trade — not wallets.
The publicTrade stream delivers EVERY trade, so one subscription yields three
tiers at no extra cost:

    retail  < WHALE_RETAIL_MAX_USD   (the crowd's tape)
    mid     in between               (dolphins)
    whale  >= WHALE_MIN_CLIP_USD     (size)

From the tiers we derive:
  - whale VOLUME (buy + sell), participation share of the whole tape
  - whale DIRECTION: bias = (buy-sell)/(buy+sell) in -1..+1, plus per-bin
    net history (direction over time)
  - whale-vs-retail SENTIMENT DIVERGENCE: whales accumulating what retail
    sells = classic bottom behavior; whales distributing into retail buying
    = classic top behavior
  - book depth imbalance (passive interest; spoofable -> context never gate)
  - the long/short ACCOUNT ratio (retail positioning, contrarian)

None of it enters the ladder weights (context + alerts only — same probation
path the liquidation stream took). Entity-labeled flows (exchange netflows,
holder cohorts) need paid feeds and stay in external.py as scaffolds. Pure
math lives in small helpers so selftest covers it offline.
"""
import json
import threading
import time
from collections import deque

import requests
import websocket  # websocket-client

import config as C
import store
import venues

HEADERS = {"User-Agent": "signalia/3.0"}
TIMEOUT = 15


def _get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ------------------------------ pure math -------------------------------------
def bias(buy, sell):
    """Directional lean of a tape segment: -1 (all selling) .. +1 (all buying)."""
    total = buy + sell
    if total <= 0:
        return None
    return round((buy - sell) / total, 3)


def direction_read(b):
    """Plain-english label for a whale bias value."""
    if b is None:
        return None
    if b >= 0.25:
        return "ACCUMULATING HARD"
    if b >= 0.10:
        return "ACCUMULATING"
    if b <= -0.25:
        return "DISTRIBUTING HARD"
    if b <= -0.10:
        return "DISTRIBUTING"
    return "BALANCED"


def divergence_read(whale_bias, retail_bias):
    """
    The smart-money-vs-crowd verdict. Whales and retail leaning OPPOSITE ways
    is the information-rich state; aligned tapes just confirm the trend.
    """
    if whale_bias is None or retail_bias is None:
        return None
    if whale_bias >= 0.15 and retail_bias <= -0.05:
        return {"key": "smart_accumulation",
                "text": "whales are buying what retail is selling — classic bottom behavior"}
    if whale_bias <= -0.15 and retail_bias >= 0.05:
        return {"key": "smart_distribution",
                "text": "whales are selling into retail buying — classic top behavior"}
    if whale_bias >= 0.10 and retail_bias >= 0.10:
        return {"key": "aligned_buying", "text": "whales and retail both buying — trend confirmation"}
    if whale_bias <= -0.10 and retail_bias <= -0.10:
        return {"key": "aligned_selling", "text": "whales and retail both selling — pressure confirmed"}
    return {"key": "neutral", "text": "no meaningful whale/retail divergence"}


def _book_imbalance(bids, asks, pct):
    """bids/asks: [(price, qty), ...] best-first. Notional within ±pct of mid."""
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    bid_usd = sum(p * q for p, q in bids if p >= mid * (1 - pct))
    ask_usd = sum(p * q for p, q in asks if p <= mid * (1 + pct))
    total = bid_usd + ask_usd
    if total <= 0:
        return None
    return {"bid_usd": round(bid_usd), "ask_usd": round(ask_usd),
            "bid_share": round(bid_usd / total, 3)}


def crowd_read(long_share):
    """Contrarian label for the retail long/short account ratio."""
    if long_share is None:
        return None
    if long_share >= C.CROWD_LONG_EXTREME:
        return "crowded long"
    if long_share <= C.CROWD_SHORT_EXTREME:
        return "crowded short"
    return "balanced"


def absorption_note(oi_change, chg24h):
    """OI building while price holds = someone is absorbing the selling."""
    if oi_change is None or chg24h is None:
        return None
    if oi_change >= 0.01 and -0.02 <= chg24h <= 0.005:
        return "OI building into a flat tape — absorption"
    return None


# ------------------------------ REST fetchers ---------------------------------
def depth_imbalance(symbol, pct=None):
    pct = pct or C.WHALE_DEPTH_PCT
    j = _get(f"{C.BYBIT_BASE}/v5/market/orderbook",
             {"category": "linear", "symbol": symbol, "limit": 200})
    r = j["result"]
    bids = [(float(p), float(q)) for p, q in r["b"]]
    asks = [(float(p), float(q)) for p, q in r["a"]]
    return _book_imbalance(bids, asks, pct)


def account_ratio(symbol):
    """Latest retail long-account share (0..1)."""
    j = _get(f"{C.BYBIT_BASE}/v5/market/account-ratio",
             {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1})
    rows = j["result"]["list"]
    return float(rows[0]["buyRatio"]) if rows else None


# ------------------------------ whale tape (WS) --------------------------------
class WhaleTapeMonitor:
    """
    Tiered trade tape. Whale clips keep full events (for windows/bins);
    retail + mid flow is aggregated into fixed time bins so the full firehose
    costs O(1) memory. Same multi-venue plumbing as LiquidationMonitor:
    background thread, venue-spec rotation when a connection proves
    undeliverable, winner memory, warmup gate before the engine trusts it.
    """

    KIND = "trade"
    # a healthy connection refreshes last_msg_ts via data or pongs every ~20s
    SILENT_SEC = 90

    def __init__(self, symbols, ws_source, max_age_sec, min_clip_usd=None,
                 retail_max_usd=None, bin_sec=900):
        self.symbols = symbols
        # ws_source: one URL, list of URLs (Bybit mirrors), or venue specs
        self.specs = self._normalize(ws_source)
        self._spec_i = 0
        self._got_frame = False
        self.max_age = max_age_sec
        self.min_clip = min_clip_usd or C.WHALE_MIN_CLIP_USD
        self.retail_max = retail_max_usd or C.WHALE_RETAIL_MAX_USD
        self.bin_sec = bin_sec
        self.events = deque()          # whale clips: (ts_ms, side, notional_usd)
        self.bins = {}                 # bin_idx -> [r_buy, r_sell, m_buy, m_sell]
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
        print(f"whale ws: subscribe sent via {self.active_venue} "
              f"({self.active_url})")

    def _on_message(self, ws, msg):
        self.last_msg_ts = time.time()
        try:
            m = json.loads(msg)
        except Exception:
            return
        spec = self.spec
        if spec["rejected"](m):
            print(f"whale ws: subscribe REJECTED by {self.active_venue}: "
                  f"{str(m)[:160]} — rotating")
            self._got_frame = False    # a rejection is not delivery
            ws.close()
            return
        if not self._got_frame and spec["alive"](m):
            self._got_frame = True     # real delivery proven — remember winner
            self._save_winner()
        trades = spec["parse"](m)
        if not trades:
            return
        with self.lock:
            for ts, side, notional in trades:
                if notional >= self.min_clip:
                    self.events.append((ts, side, notional))
                    continue
                # aggregate the sub-whale tape into time bins (bounded memory)
                idx = int(ts / 1000) // self.bin_sec
                b = self.bins.get(idx)
                if b is None:
                    b = self.bins[idx] = [0.0, 0.0, 0.0, 0.0]
                if notional < self.retail_max:
                    b[0 if side == "Buy" else 1] += notional   # retail
                else:
                    b[2 if side == "Buy" else 3] += notional   # mid ("dolphins")
            self._prune()

    def _on_pong(self, ws, *a):
        self.last_msg_ts = time.time()     # protocol pongs count as traffic

    def _on_close(self, ws, *a):
        self.connected = False

    def _on_error(self, ws, err):
        self.connected = False
        print(f"whale ws error ({self.active_venue}):", err)

    # ------------------------------ plumbing --------------------------------
    def _prune(self):
        cutoff = time.time() * 1000 - self.max_age * 1000
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        min_idx = int(cutoff / 1000) // self.bin_sec
        for idx in [i for i in self.bins if i < min_idx]:
            del self.bins[idx]

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
                print(f"whale ws: no traffic for {silent:.0f}s "
                      f"({self.active_venue}) — forcing reconnect")
                self._force_close(ws)
                continue
            payload = self.spec.get("ping")
            if not payload:
                continue               # venue keeps fresh via protocol pongs
            try:
                ws.send(payload)
            except Exception as e:
                print("whale ws: ping send failed:", e, "— forcing reconnect")
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
                print("whale ws run error:", e)
            self.connected = False
            if not self._got_frame and len(self.specs) > 1:
                self._spec_i += 1       # venue never delivered — hunt on
                print(f"whale ws: {spec['name']} ({spec['url']}) delivered "
                      f"nothing — rotating to {self.active_venue}")
            time.sleep(5)               # reconnect backoff

    def start(self):
        self.started_at = time.time()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()

    # ------------------------------ readers ---------------------------------
    def healthy(self):
        """Connected AND actually receiving traffic (see LiquidationMonitor)."""
        return (self.connected and self.last_msg_ts is not None
                and (time.time() - self.last_msg_ts) < self.SILENT_SEC + 30)

    def ready(self, warmup_sec):
        return self.started_at is not None and (time.time() - self.started_at) >= warmup_sec

    def delta(self, window_sec):
        """Whale-clip buy/sell notional + net over the last window_sec."""
        now = time.time() * 1000
        buy = sell = 0.0
        count = 0
        with self.lock:
            for ts, side, notional in self.events:
                if (now - ts) / 1000 <= window_sec:
                    count += 1
                    if side == "Buy":
                        buy += notional
                    else:
                        sell += notional
        return {"buy": round(buy), "sell": round(sell),
                "net": round(buy - sell), "count": count}

    def summary(self, window_sec):
        """
        Full tiered read over the window: whale volume/direction/largest clip,
        retail + mid flow, whale participation share, and the whale-vs-retail
        divergence verdict. This is the panel the dashboard renders.
        """
        now = time.time() * 1000
        w_buy = w_sell = biggest = 0.0
        w_count = 0
        with self.lock:
            for ts, side, notional in self.events:
                if (now - ts) / 1000 > window_sec:
                    continue
                w_count += 1
                biggest = max(biggest, notional)
                if side == "Buy":
                    w_buy += notional
                else:
                    w_sell += notional
            min_idx = int((now / 1000 - window_sec)) // self.bin_sec
            r_buy = r_sell = m_buy = m_sell = 0.0
            for idx, (rb, rs, mb, ms) in self.bins.items():
                if idx >= min_idx:
                    r_buy += rb; r_sell += rs; m_buy += mb; m_sell += ms

        w_vol, r_vol, m_vol = w_buy + w_sell, r_buy + r_sell, m_buy + m_sell
        total = w_vol + r_vol + m_vol
        w_bias, r_bias = bias(w_buy, w_sell), bias(r_buy, r_sell)
        return {
            "whale": {"buy": round(w_buy), "sell": round(w_sell),
                      "net": round(w_buy - w_sell), "vol": round(w_vol),
                      "count": w_count, "bias": w_bias,
                      "avg_clip": round(w_vol / w_count) if w_count else None,
                      "max_clip": round(biggest) if biggest else None,
                      "direction": direction_read(w_bias)},
            "mid": {"net": round(m_buy - m_sell), "vol": round(m_vol),
                    "bias": bias(m_buy, m_sell)},
            "retail": {"buy": round(r_buy), "sell": round(r_sell),
                       "net": round(r_buy - r_sell), "vol": round(r_vol),
                       "bias": r_bias},
            "whale_share": round(w_vol / total, 3) if total > 0 else None,
            "divergence": divergence_read(w_bias, r_bias),
        }

    def whale_net_bins(self, n_bins, bin_sec):
        """Whale net flow per time bin, oldest -> newest (direction over time)."""
        now = time.time() * 1000
        out = [0.0] * n_bins
        with self.lock:
            for ts, side, notional in self.events:
                idx = int((now - ts) / 1000 // bin_sec)
                if 0 <= idx < n_bins:
                    out[n_bins - 1 - idx] += notional if side == "Buy" else -notional
        return [round(x) for x in out]
