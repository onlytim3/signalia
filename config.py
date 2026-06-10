"""
Signalia — configuration.

Everything tunable lives here. The defaults encode the strategy we discussed:
survival-weighted, macro-gated, staged deployment. Override anything via env vars.
"""
import os

# ---- Symbols ----------------------------------------------------------------
# BTC drives the market-wide ladder (it's the regime/confirmation anchor).
LADDER_SYMBOL = os.getenv("LADDER_SYMBOL", "BTCUSDT")
# Watchlist: gets the full deep treatment (per-asset structural + liq + insights)
WATCHLIST = os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT").split(",")
# Symbols whose liquidations we stream (subset of watchlist; keep small for WS load)
SYMBOLS = os.getenv("LIQ_SYMBOLS", "BTCUSDT,SOLUSDT,ETHUSDT").split(",")

# ---- Bybit (public market data, no API key needed) --------------------------
# Default to Bybit's production mirror (api.bytick.com). The primary api.bybit.com
# domain is geo-blocked in some regions (incl. parts of the US) where it won't even
# resolve DNS; the mirror serves identical data and resolves globally. Override via
# env (e.g. BYBIT_BASE=https://api.bybit.com) where the primary is reachable.
BYBIT_BASE = os.getenv("BYBIT_BASE", "https://api.bytick.com")
OI_INTERVAL = "1h"     # open-interest sampling interval
OI_LOOKBACK = 24       # hours over which to measure the OI flush

# ---- LAYER 1 : MACRO REGIME (the gate / speed-limit) ------------------------
# Changes rarely (FOMC ~6wk, CPI monthly), so it's set MANUALLY here.
# After each FOMC / CPI print, update MACRO_REGIME and redeploy (or set env var).
# one of: "HOSTILE" | "NEUTRAL" | "SUPPORTIVE"
MACRO_REGIME = os.getenv("MACRO_REGIME", "HOSTILE")
MACRO_CEILING = {"HOSTILE": 40, "NEUTRAL": 70, "SUPPORTIVE": 100}

# ---- Raw-desire blend (structural vs sentiment) -----------------------------
W_STRUCTURAL = 0.55
W_SENTIMENT = 0.45

# ---- LAYER 2 : STRUCTURAL (bottom detector) ---------------------------------
# Three sub-signals (weights sum to 1). If the live liquidation stream isn't
# ready yet, its weight is redistributed across funding + OI automatically.
W_FUNDING = 0.50
W_OI_FLUSH = 0.20
W_LIQ_FLUSH = 0.30
# Funding rate (per-interval) -> contrarian score. Deeply negative = crowded
# shorts = bullish fuel = high score.
FUNDING_MAX_NEG = -0.0005   # <= this -> 100
FUNDING_MAX_POS = 0.0005    # >= this -> 0
# OI % change over lookback -> flush score. Sharp drop = forced selling spent.
OI_DROP_FULL = -0.12        # OI down >=12% -> 100
OI_RISE_ZERO = 0.05         # OI up   >= 5% -> 0

# ---- Live liquidation stream (Bybit All Liquidation WS) ---------------------
BYBIT_WS = os.getenv("BYBIT_WS", "wss://stream.bytick.com/v5/public/linear")  # mirror; see BYBIT_BASE
LIQ_MAX_AGE_SEC = 5400      # retain 90 min of events
LIQ_BIN_SEC = 900           # 15-min bins
LIQ_BINS = 6                # 6 bins = 90 min window for spike/decay detection
LIQ_WARMUP_SEC = 1800       # need 30 min of stream before trusting flush score
LIQ_DECAY_ALERT = 60        # flush score crossing this upward = cascade spent

# ---- LAYER 4 : CONFIRMATION TRIGGERS (the accelerator) ----------------------
BTC_RECLAIM_LEVEL = float(os.getenv("BTC_RECLAIM_LEVEL", "74000"))
FNG_CONFIRM = 30            # Fear & Greed back above this = sentiment confirm
# (third trigger = funding flips positive)
# number of confirmations fired -> max deployment cap
CONFIRM_CAPS = {0: 30, 1: 50, 2: 75, 3: 100}

# ---- Per-asset split of whatever capital is deployed ------------------------
# Downstream of the ladder. BTC takes leverage; SOL takes size; catalyst small.
ASSET_SPLIT = {"BTC": 0.55, "SOL": 0.30, "CATALYST": 0.15}

# ---- Alerting ---------------------------------------------------------------
ALERT_DELTA = float(os.getenv("ALERT_DELTA", "10"))   # pct-pt move that alerts
RUN_INTERVAL_MIN = int(os.getenv("RUN_INTERVAL_MIN", "15"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
DEEP_FEAR = 20             # extra alert when F&G crosses here

# ---- Dashboard / API auth ----------------------------------------------------
# Two complementary locks; both empty (default) = everything open for local dev.
#   DASH_PIN   — the human door: a 4-digit PIN entered on a lock screen in the
#                browser; unlocking sets a signed 30-day session cookie. Wrong
#                attempts are rate-limited (5 per 5 min per IP) because a 4-digit
#                space is small.
#   DASH_TOKEN — the machine door: ?key=<token> or X-Auth-Token header, for curl,
#                uptime checks beyond /ping, and automations like /test-alert.
# /ping and /health stay open either way.
DASH_PIN = os.getenv("DASH_PIN", "")
DASH_TOKEN = os.getenv("DASH_TOKEN", "")
# Signs the session cookie. Set it on deploys (render.yaml generates one) so
# unlocked sessions survive restarts; the local fallback regenerates per boot.
SECRET_KEY = os.getenv("SECRET_KEY", "") or os.urandom(32).hex()

# ---- Notification credentials (set as env vars on Render) -------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")


# ============================================================================
#  v2 UPGRADES
# ============================================================================

# ---- Persistence (time-series store) ----------------------------------------
DB_FILE = os.getenv("DB_FILE", "ladder.db")        # on Render -> /data/ladder.db

# ---- Adaptive thresholds (rolling-percentile scoring) -----------------------
BASELINE_DAYS = int(os.getenv("BASELINE_DAYS", "30"))   # rolling window
BASELINE_MIN_SAMPLES = 50      # below this, fall back to static _lin scoring

# ---- Market scanner ---------------------------------------------------------
SCAN_QUOTE = "USDT"                       # only scan USDT perps
SCAN_MIN_TURNOVER = float(os.getenv("SCAN_MIN_TURNOVER", "5000000"))  # 24h $ liquidity floor
SCAN_TOP_N = 10                           # how many names per screen
# enrich watchlist (not whole market) with ATH distance via CoinGecko
CG_IDS = {                                # Bybit symbol -> CoinGecko id
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana",
    "XRPUSDT": "ripple", "BNBUSDT": "binancecoin",
}

# ---- Position-aware sizing --------------------------------------------------
CAPITAL_BASE = float(os.getenv("CAPITAL_BASE", "0"))   # set to your dry-powder size
MIN_TRANCHE_PCT = 5     # don't ping to add/trim for moves smaller than this

# ---- Heartbeat / dead-man's switch ------------------------------------------
STALE_RUN_MIN = 45      # alert if engine hasn't run in this long
WS_DOWN_MIN = 10        # alert if liquidation WS stays disconnected this long

# ---- Scaffolded layers (need API keys / paid feeds; see modules) ------------
FRED_API_KEY = os.getenv("FRED_API_KEY", "")          # macro auto-suggest
DERIBIT_ENABLED = os.getenv("DERIBIT_ENABLED", "0") == "1"   # DVOL implied vol
ETF_FLOW_OVERRIDE = os.getenv("ETF_FLOW_OVERRIDE", "")       # manual daily net $ (millions)


# ============================================================================
#  v3 UPGRADES — trend anchor, top-risk mirror, confirm stability, resilience
# ============================================================================

# ---- Trend / cycle context (daily klines; free, keyless) ---------------------
KLINE_DAYS = 200       # daily candles fetched per run (also caps SMA_SLOW)
SMA_FAST = 50
SMA_SLOW = 200
RANGE_DAYS = 20        # short-range hi/lo: is this dip at range lows or mid-air?
RV_WINDOW = 30         # realized-vol window (days), annualized
RV_STAGE_PCTILE = 80   # above this vol percentile, suggest staging entries

# If BTC_RECLAIM_LEVEL is NOT pinned via env, the reclaim trigger follows the
# 50d SMA automatically — a static level goes stale as the market moves.
BTC_RECLAIM_AUTO = os.getenv("BTC_RECLAIM_LEVEL") is None

# ---- Overheat / top-risk (the mirror of the bottom detector) -----------------
# The ladder detects when to catch knives; this detects when to leave the party.
W_OH_FUNDING = 0.35    # longs paying rich premium = crowded
W_OH_FNG = 0.25        # euphoria
W_OH_EXT = 0.25        # price stretched above the 200d SMA
W_OH_OI = 0.15         # open interest ballooning into the move
OH_FUNDING_FULL = 0.0005   # per-interval funding at/above this -> component 100
OH_FNG_FLOOR = 50          # greed starts counting here
OH_FNG_FULL = 80
OH_EXT_FLOOR = 0.10        # +10% above 200d SMA -> starts counting
OH_EXT_FULL = 0.50         # +50% above -> component 100
OH_OI_FULL = 0.05          # OI +5% in 24h -> component 100
OVERHEAT_ALERT = 60        # alert when the composite crosses this upward

# ---- Confirmation stability ---------------------------------------------------
# funding confirm requires current > 0 AND mean of last N settled rates > 0,
# so a single flicker above zero can't flip the cap (30 -> 50) and spam alerts.
FUNDING_CONFIRM_N = 3

# ---- Squeeze screen (scanner composite: crowded shorts into weakness) --------
SQUEEZE_MIN_NEG_FUNDING = -0.0001   # funding at least this negative
SQUEEZE_MIN_DROP = -0.05            # and down at least 5% in 24h

# ---- Whale flow (free, venue-level: tape + book depth + crowd ratio) ----------
# Measures whale BEHAVIOR on Bybit, not wallets. Context + alerts only — it does
# NOT enter the ladder weights (same probation the liq stream served).
WHALE_SYMBOLS = os.getenv("WHALE_SYMBOLS", "BTCUSDT").split(",")  # busy stream; keep tight
WHALE_MIN_CLIP_USD = float(os.getenv("WHALE_MIN_CLIP_USD", "100000"))  # a "large clip"
WHALE_WINDOW_SEC = 4 * 3600         # rolling tape window
WHALE_WARMUP_SEC = int(os.getenv("WHALE_WARMUP_SEC", "1800"))  # trust tape after this
WHALE_DEPTH_PCT = 0.02              # book band ±2% of mid
WHALE_NET_ALERT_USD = float(os.getenv("WHALE_NET_ALERT_USD", "5000000"))
# alert fires when 1h net large-clip buying crosses the line while F&G <= FNG_CONFIRM
CROWD_LONG_EXTREME = 0.65           # retail long-account share above = crowded long
CROWD_SHORT_EXTREME = 0.35

# Tiered tape: everything below the whale line is kept too (binned, O(1) memory),
# splitting the crowd's flow from size so the two can be compared directly.
WHALE_RETAIL_MAX_USD = float(os.getenv("WHALE_RETAIL_MAX_USD", "10000"))
WHALE_BIN_SEC = 900                 # 15-min bins for sub-whale flow + direction bars
WHALE_DIR_BINS = 16                 # 16 x 15min = 4h of whale-direction history
