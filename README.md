# Signalia

An analytical **deployment-ladder** notification system for manual trading on Bybit.
It does **not** trade. Its only job: watch four signal layers, compute what fraction
of your dry powder *should* be deployed right now, and alert you (Telegram + email)
when the ladder moves or a trigger flips. You place the orders by hand.

## The core idea
It is **not** a buy/sell oscillator (those whipsaw you). The output is a single
number — your **target deployment %** — derived from the minimum of three
independent gates. All three must permit before you scale up:

```
target % = min( raw_desire , macro_ceiling , confirmation_cap )
```

- **raw_desire** = blend of structural + sentiment (how attractive the setup is)
- **macro_ceiling** = your speed limit set by the rate regime (the gate)
- **confirmation_cap** = how far price action lets you go (the accelerator)

The tool tells you *"you should be 30% deployed, you're at 10% — add a tranche,"*
or *"you're ahead of your own model — stop buying."* Emotion removed; staging enforced.

## The four layers
1. **Macro (gate)** — set MANUALLY in `MACRO_REGIME` (HOSTILE/NEUTRAL/SUPPORTIVE).
   It changes rarely (FOMC ~6wk, CPI monthly), so automating it is low-value;
   update it after each print. It *caps* deployment — it never times entries.
2. **Structural (bottom detector)** — three sub-signals: Bybit funding rate,
   open-interest flush (REST), and a **live liquidation stream** (Bybit All
   Liquidation WS, `allLiquidation.{symbol}`). A spike in *long* liquidations
   (`S="Buy"`) that then **decays** = forced selling spent = high score; a
   cascade still escalating scores 0. If the stream is still warming up, its
   weight redistributes to funding + OI automatically.
3. **Sentiment (contrarian confirm)** — Fear & Greed (Alternative.me) + BTC dominance.
4. **Price confirmation (accelerator)** — BTC reclaiming the 50d SMA (auto-tracked;
   pin a manual level via `BTC_RECLAIM_LEVEL`), funding flipping positive (debounced:
   current AND 3-period average must be positive, so one flicker can't whipsaw the
   cap), F&G back above 30. Each fired trigger lifts the confirmation cap.

Per-asset sizing of the deployed slice follows the strategy: **leverage BTC only**,
SOL takes size unlevered, catalyst leg stays small (`ASSET_SPLIT` in config.py).

## Files
- `config.py`    — every threshold/weight (tune here)
- `signals.py`   — fetchers + pure (static) scoring functions
- `baseline.py`  — **adaptive** percentile scoring vs rolling history (static fallback)
- `store.py`     — SQLite time-series persistence (baseline + history)
- `scanner.py`   — **market-wide scan** of all Bybit linear perps + screens + breadth
- `watchlist.py` — **deep per-asset insights** for the focused watchlist
- `sizing.py`    — position-aware $ tranche sizing (target % -> orders)
- `external.py`  — scaffolds: macro auto-suggest (FRED), DVOL (Deribit), ETF flows
- `liquidations.py` — live Bybit liquidation websocket monitor
- `engine.py`    — gather -> compute -> persist -> diff -> alert; plus run_scan()
- `notifier.py`  — Telegram + email
- `app.py`       — Flask + scheduler + WS + watchdog (Render entrypoint)
- `selftest.py`  — validates all logic offline (`python selftest.py`)

## v2 capabilities
- **Market scanner** (`/scan`, `/runscan`): every liquid USDT perp ranked by
  funding extremes, 24h movers, and turnover, plus a market **breadth** read.
  Watchlist names appearing in any extreme screen are flagged.
- **Watchlist deep insights** (`/watchlist`): per-asset adaptive structural score,
  funding, OI, live liquidations, and distance-from-ATH (with implied x-to-ATH).
- **Adaptive thresholds**: signals scored by percentile vs their own 30-day
  history, so "extreme" tracks the current regime. Falls back to static cuts
  until ~50 samples exist (`/status` shows `adaptive: true/false`).
- **Position-aware sizing**: POST `/deployed?pct=NN` to tell it how much you've
  deployed; reports then show the exact $ tranche to ADD/TRIM per asset
  (set `CAPITAL_BASE`).
- **Dead-man's switch**: a watchdog alerts you if the engine goes stale or the
  liquidation WS stays disconnected — a silent dead system is the real risk.

## v3 capabilities
- **Trend & cycle anchor** (`trend.py`): 50d/200d SMA position, golden/death state,
  20d range position (is this dip at range lows or mid-air?), 30d realized vol +
  percentile (suggests staging adds in 2–3 clips when violent). Free daily klines.
- **Overheat / top-risk mirror**: the ladder detects when to catch knives; this
  detects when to leave the party. Composite of funding premium, greed, 200d
  extension, and OI ballooning (0–100); alerts on crossing `OVERHEAT_ALERT`, plus
  golden/death-cross and 200d-SMA cross events.
- **Squeeze screen** (`/scan` → `squeeze`): crowded shorts (negative funding) INTO
  weakness (down hard), rank-summed — the catalyst-leg candidates.
- **Graceful degradation**: a flaky source (F&G, OI, klines…) no longer kills the
  run; the engine falls back to last-known values and flags `degraded` in reports
  and on the dashboard. The price ticker remains the only hard dependency.
- **Editable watchlist (no restart)**: add/remove names from the dashboard's
  watchlist card — validated live against Bybit (typos rejected instantly),
  CoinGecko id auto-resolved so ATH tracking works for any new name, capped at
  12 (focus is the feature). Stored in the DB; the `WATCHLIST` env var is only
  the first-boot seed. Insights refresh on the 15-min engine cadence (ATH
  cached 30 min) with an as-of timestamp on the card. Note: liquidation/whale
  websocket streams stay env-bound (`LIQ_SYMBOLS`/`WHALE_SYMBOLS`, restart to
  change); per-asset liq numbers are symbol-filtered, the ladder's flush score
  stays market-wide on purpose (cascades are systemic), and alt structural
  scores are labeled "vs BTC baseline" until per-symbol history exists.
- **Whale flow — smart money vs crowd** (`whales.py`) — venue-level evidence,
  honestly scoped (no labeled on-chain data). The publicTrade stream is split
  into **tiers** (whale ≥ $100k clips / mid / retail < $10k), yielding whale
  **volume**, **direction** (bias −1..+1 with a 15-min net-flow history),
  **participation share**, and the headline read: **whale-vs-retail divergence**
  (whales buying what retail sells = classic bottom; whales selling into retail
  buying = classic top). Plus **book depth imbalance** (±2% of mid; spoofable →
  context only), the **retail long/short account ratio**, and an **OI absorption**
  read. Alerts on a whale bid into fear (`WHALE_NET_ALERT_USD`) and on the tape
  flipping into smart-money accumulation/distribution divergence. Context +
  alerts only — none of it enters the ladder weights. Entity-labeled flows
  (exchange netflows, holder cohorts) need paid feeds and stay scaffolded.

- **Model Scorecard** (`scorecard.py`, `/scorecard`): the model grades its own
  past calls — every stored reading is checked against the actual forward move
  (4h/1d/3d), bucketed by signal state (washed-out structural, deep fear,
  overheat, whale bias, spent cascades), with hit-rates, sample sizes, and the
  same-period baseline so a drifting market can't fake an edge. Low-n buckets
  are dimmed, not hidden. A "Track record" line joins reports once any bucket
  reaches confidence. Zero new data sources; it compounds daily.
- **Adaptive overheat**: once ~50 readings exist, the overheat composite is
  percentiled against its own rolling history (the raw composite is what gets
  stored, so the baseline never feeds on its own output). "Crowded" becomes
  regime-relative, like the structural side.
- **Divergence alert persistence**: the smart-money divergence verdict must
  hold for two consecutive runs before alerting, and never re-fires until it
  fully decays to neutral — no flapping pings from a choppy tape.

### Scaffolded (need keys / paid feeds — wire after deploy)
- **Macro auto-suggest** (`external.suggest_regime`): set `FRED_API_KEY` to get a
  regime *suggestion* from 10y real yields; you still confirm `MACRO_REGIME`.
- **Implied vol** (`external.dvol`): set `DERIBIT_ENABLED=1` for BTC DVOL (tells
  you when long-dated calls are cheap vs rich).
- **ETF flows** (`external.etf_net_flow`): set `ETF_FLOW_OVERRIDE` to today's net
  $M manually until a Farside/SoSoValue scraper is added.
- Still open (Tier 3): multi-exchange aggregation, on-chain netflows, dashboard.

## Files

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env          # fill in your values, then export them
python selftest.py            # verify the logic
python engine.py --report     # one live run, forces a full report
python app.py                 # runs the loop + serves the dashboard on :10000
```

## Deploy on Render
**Easiest: the blueprint.** Push the repo, then Render -> **New -> Blueprint** —
`render.yaml` wires everything: build/start commands, `/health` checks, a 1 GB
persistent disk at `/data` (DB + alert state survive redeploys), an
auto-generated `DASH_TOKEN`, and secret slots for Telegram/SMTP. After deploy:
1. Set `DASH_PIN` (your 4-digit PIN) in the service's Environment. Opening the
   dashboard now greets you with a **digital lock screen** — tap the PIN, get a
   30-day session. Wrong tries are rate-limited (5 per 5 min per IP). The
   auto-generated `DASH_TOKEN` stays as the machine door (`?key=<token>`) for
   curl and automations; `/ping` + `/health` stay open for uptime monitors.
2. Set `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (and SMTP if you want email).
3. Hit `https://<app>.onrender.com/test-alert?key=<token>` — a test message
   must land in Telegram before you trust the alerting.
4. After each FOMC/CPI, update `MACRO_REGIME` in Environment (triggers a restart).

### Running on the FREE tier
Swap the free blueprint in before pushing: `cp render-free.yaml render.yaml`.
Two free-plan limits matter for an always-on signal engine, both worked around:
1. **Free services spin down after ~15 idle minutes** — and the scheduler,
   websockets, alerts, and watchdog all sleep with the process. Point an external
   pinger at `/ping` (auth-exempt) every 5–10 min: **UptimeRobot free** (5-min
   checks, recommended — it doubles as an uptime alert) and/or the bundled
   **GitHub Actions workflow** (`.github/workflows/keepalive.yml` — set the
   `KEEPALIVE_URL` repo variable to your onrender.com URL). GH cron can lag
   under load, so treat it as the fallback, not the primary.
2. **No persistent disk** — the SQLite history and alert state reset on every
   deploy/restart. The engine degrades gracefully (static thresholds until the
   adaptive baseline rebuilds over ~12h; one repeat "baseline established"
   alert per restart; liq/whale tapes re-warm 30 min). Live data is unaffected.
Cold starts (~30–60s + the boot engine run) only happen if the pinger misses.
The paid `render.yaml` (starter + 1 GB disk) removes both limits — that's the
upgrade path once the free tier's amnesia starts costing you signal quality.

**Manual setup instead:** New Web Service -> build `pip install -r requirements.txt`
-> start `gunicorn -w 1 -k gthread --threads 8 app:app` (**one worker** — the
scheduler + websockets live in-process; gthread keeps the dashboard responsive
while the engine runs) -> add the env vars from `.env.example` -> attach a disk
at `/data` and set `DB_FILE=/data/ladder.db`, `STATE_FILE=/data/state.json`.
7. Endpoints: `/` (live glassmorphism dashboard — ladder gauge, gates, confirmations,
   sizing, liquidations, scanner, watchlist, history; mobile-friendly), `/status`
   (current reading as JSON), `/scan`, `/watchlist`, `/history?n=192` (recent readings
   for the sparklines), `/run` + `/runscan` (manual triggers), `POST /deployed?pct=NN`,
   `/health`, `/ping` (plain-text uptime probe).

## Honest caveats (built in on purpose)
- **It does not call the bottom.** Nothing does. Its value is enforcing the
  staged ladder so you act on signal confluence, not adrenaline.
- The **macro layer is manual** by design — wire FRED/FedWatch later if you want
  it automated, but it moves too slowly to be worth it at v1.
- The **liquidation stream needs ~30 min warmup** (`LIQ_WARMUP_SEC`) after boot
  before its flush score is trusted; until then the structural score runs on
  funding + OI only. `/status` shows `ws_connected` so you can confirm the feed.
- **ETF flows** aren't ingested in v1 (no clean free feed) — add later as a layer.
- Backtest with suspicion: these signals fit the recent flushes, but three
  events is not a sample. Regimes shift.
- This is decision support, not financial advice.
