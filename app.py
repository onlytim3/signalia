"""
Signalia — Render entrypoint.

A tiny Flask app (so Render's web service has a bound port + a /status page)
plus a background scheduler that runs the engine every RUN_INTERVAL_MIN minutes.

Run locally:   python app.py
Run on Render: gunicorn -w 1 app:app     (one worker — avoids duplicate schedulers)
"""
import datetime
import hmac
import os
import time
from collections import deque

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, session

import config as C
import engine
import liquidations
import scanner
import scorecard
import signals as S
import store
import watchlist as wl
import whales
from notifier import notify

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True   # pick up dashboard edits without a restart
app.secret_key = C.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
)
_last = {}
_last_scan = {}

# Persistence + live liquidation stream, handed to the engine.
store.init_db()
liq_monitor = liquidations.LiquidationMonitor(
    C.SYMBOLS, C.BYBIT_WS_CANDIDATES, C.LIQ_MAX_AGE_SEC, C.LIQ_BIN_SEC
)
liq_monitor.start()
engine.LIQ_MONITOR = liq_monitor
whale_tape = whales.WhaleTapeMonitor(C.WHALE_SYMBOLS, C.BYBIT_WS_CANDIDATES, C.WHALE_WINDOW_SEC)
whale_tape.start()
engine.WHALE_TAPE = whale_tape
_ws_down_since = [None]
_tape_down_since = [None]

# ---- Auth: PIN lock screen (humans) + token (machines) ----------------------
# Both unset (local dev) = open. /ping + /health stay open for uptime monitors.
# A browser hitting / while locked gets the digital-lock PIN pad; unlocking via
# POST /unlock sets a signed 30-day session. ?key=/X-Auth-Token still works for
# curl + automations. Wrong PINs are rate-limited per IP — 4 digits is a small
# space, so the lockout is what makes it safe.
_OPEN_PATHS = {"/ping", "/health", "/unlock", "/lock"}
_PIN_FAILS = {}            # ip -> deque of failure timestamps
PIN_MAX_FAILS = 5
PIN_LOCKOUT_SEC = 300


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def _authed():
    if C.DASH_TOKEN:
        supplied = request.args.get("key") or request.headers.get("X-Auth-Token", "")
        if supplied and hmac.compare_digest(supplied, C.DASH_TOKEN):
            return True
    if C.DASH_PIN and session.get("unlocked"):
        return True
    return False


@app.before_request
def _auth():
    if not (C.DASH_TOKEN or C.DASH_PIN) or request.path in _OPEN_PATHS:
        return None
    if _authed():
        return None
    if request.path == "/" and C.DASH_PIN:
        return render_template("lock.html")          # the digital lock
    return jsonify({"error": "unauthorized — unlock with the PIN at / "
                             "or pass ?key=<DASH_TOKEN>"}), 401


@app.route("/unlock", methods=["POST"])
def unlock():
    """PIN pad submit. Rate-limited: PIN_MAX_FAILS wrong tries per lockout window."""
    if not C.DASH_PIN:
        return jsonify({"ok": False, "error": "no PIN configured"}), 400
    ip, now = _client_ip(), time.time()
    fails = _PIN_FAILS.setdefault(ip, deque())
    while fails and now - fails[0] > PIN_LOCKOUT_SEC:
        fails.popleft()
    if len(fails) >= PIN_MAX_FAILS:
        retry = int(PIN_LOCKOUT_SEC - (now - fails[0])) + 1
        return jsonify({"ok": False, "locked": True, "retry_sec": retry}), 429
    pin = str((request.get_json(silent=True) or {}).get("pin", ""))
    if hmac.compare_digest(pin, C.DASH_PIN):
        session.permanent = True
        session["unlocked"] = True
        _PIN_FAILS.pop(ip, None)
        return jsonify({"ok": True})
    fails.append(now)
    return jsonify({"ok": False, "remaining": PIN_MAX_FAILS - len(fails)}), 401


@app.route("/lock")
def lock():
    """Walk-away button: drop the session and show the lock screen."""
    session.clear()
    return render_template("lock.html")


def _refresh_insights():
    """Rebuild watchlist insights against the latest scan (cheap: ~2 calls/name,
    ATH cached). Keeps the watchlist on the 15-min engine cadence instead of
    going stale between 30-min scans."""
    if not _last_scan.get("scan"):
        return
    syms = store.get_watchlist()
    _last_scan["watchlist_flags"] = scanner.watchlist_flags(_last_scan["scan"], syms)
    _last_scan["insights"] = wl.build(syms, engine.LIQ_MONITOR)
    _last_scan["watchlist"] = syms
    _last_scan["insights_ts"] = time.time()


def _job():
    global _last
    try:
        _last = engine.run_once()
        _refresh_insights()
    except Exception as e:
        print("run_once error:", e)


def _scan_job():
    global _last_scan
    try:
        _last_scan = engine.run_scan()
    except Exception as e:
        print("run_scan error:", e)


def _watchdog():
    """Dead-man's switch: alert if the engine goes stale or the WS stays down."""
    try:
        last = store.get_meta("last_run_ts")
        if last and (time.time() - float(last)) > C.STALE_RUN_MIN * 60:
            notify("Signalia DOWN", f"No successful run in {C.STALE_RUN_MIN}+ min.")
        if not liq_monitor.healthy():
            if _ws_down_since[0] is None:
                _ws_down_since[0] = time.time()
            elif (time.time() - _ws_down_since[0]) > C.WS_DOWN_MIN * 60:
                notify("Signalia", f"Liquidation WS disconnected {C.WS_DOWN_MIN}+ min.")
                _ws_down_since[0] = time.time()
        else:
            _ws_down_since[0] = None
        if not whale_tape.healthy():
            if _tape_down_since[0] is None:
                _tape_down_since[0] = time.time()
            elif (time.time() - _tape_down_since[0]) > C.WS_DOWN_MIN * 60:
                notify("Signalia", f"Whale tape WS disconnected {C.WS_DOWN_MIN}+ min.")
                _tape_down_since[0] = time.time()
        else:
            _tape_down_since[0] = None
    except Exception as e:
        print("watchdog error:", e)


@app.route("/")
def home():
    cfg = {
        "ladder_symbol": C.LADDER_SYMBOL,
        "reclaim": C.BTC_RECLAIM_LEVEL,
        "fng_confirm": C.FNG_CONFIRM,
        "interval_min": C.RUN_INTERVAL_MIN,
        "macro_ceiling": C.MACRO_CEILING,
        "confirm_caps": C.CONFIRM_CAPS,
        "asset_split": C.ASSET_SPLIT,
        "liq_symbols": C.SYMBOLS,
        "liq_warmup_sec": C.LIQ_WARMUP_SEC,
        "capital_base": C.CAPITAL_BASE,
        "overheat_alert": C.OVERHEAT_ALERT,
        "rv_stage": C.RV_STAGE_PCTILE,
        "whale_symbols": C.WHALE_SYMBOLS,
        "pin_enabled": bool(C.DASH_PIN),
    }
    return render_template("dashboard.html", cfg=cfg)


@app.route("/ping")
def ping():
    return "signalia running", 200


@app.route("/history")
def history():
    try:
        n = min(int(request.args.get("n", 192)), 1000)
    except (TypeError, ValueError):
        n = 192                                   # bad ?n= -> safe default, never 500
    return jsonify(store.recent_rows(n))


@app.route("/health")
def health():
    last = store.get_meta("last_run_ts")
    stale = bool(last and (time.time() - float(last)) > C.STALE_RUN_MIN * 60)
    def _silent(mon):
        return round(time.time() - mon.last_msg_ts) if mon.last_msg_ts else None
    return jsonify({"status": "ok", "ws_connected": liq_monitor.healthy(),
                    "tape_connected": whale_tape.healthy(), "stale": stale,
                    "ws_silent_sec": _silent(liq_monitor),
                    "tape_silent_sec": _silent(whale_tape),
                    "ws_url": liq_monitor.active_url,
                    "tape_url": whale_tape.active_url})


@app.route("/status")
def status():
    out = dict(_last) if _last else {"status": "warming up"}
    out["ws_connected"] = liq_monitor.healthy()
    return jsonify(out)


@app.route("/scan")
def scan():
    return jsonify(_last_scan or {"status": "no scan yet — hit /runscan"})


@app.route("/watchlist")
def watchlist_view():
    if not _last_scan:
        return jsonify({"status": "no scan yet"})
    return jsonify({"insights": _last_scan.get("insights"),
                    "flags": _last_scan.get("watchlist_flags")})


@app.route("/run")
def run():
    _job()
    return jsonify(_last)


@app.route("/runscan")
def runscan():
    _scan_job()
    return jsonify(_last_scan)


@app.route("/deployed", methods=["POST"])
def set_deployed():
    """Tell the system how much you've actually deployed, for sizing."""
    try:
        pct = float(request.args.get("pct", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "pct must be a number"}), 400
    if not 0 <= pct <= 100:                       # out-of-range would poison sizing
        return jsonify({"ok": False, "error": "pct must be 0-100"}), 400
    store.set_meta("deployed_pct", pct)
    store.set_meta("deployed_set_ts", time.time())   # staleness nudge anchor
    return jsonify({"deployed_pct": pct})


@app.route("/scorecard")
def scorecard_view():
    """The model grading its own past calls — forward returns by signal state."""
    return jsonify(scorecard.build())


@app.route("/watchlist-config", methods=["GET"])
def watchlist_config():
    return jsonify({"watchlist": store.get_watchlist(),
                    "streams": {"liq": C.SYMBOLS, "whale": C.WHALE_SYMBOLS},
                    "cg_map": store.get_cg_map()})


@app.route("/watchlist-config", methods=["POST"])
def watchlist_config_set():
    """Add/remove a watchlist symbol at runtime — validated live against Bybit,
    CoinGecko id auto-resolved so ATH tracking works for new names. No restart."""
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    sym = str(body.get("symbol", "")).upper().strip().replace("/", "").replace("-", "")
    if sym and not sym.endswith("USDT"):
        sym += "USDT"                                  # convenience: "doge" works
    if action not in ("add", "remove") or not sym:
        return jsonify({"ok": False, "error": "need action add|remove + symbol"}), 400
    cur = store.get_watchlist()

    if action == "remove":
        if sym not in cur:
            return jsonify({"ok": False, "error": f"{sym} is not on the watchlist"}), 404
        cur = [s for s in cur if s != sym]
        store.set_watchlist(cur)
        _refresh_insights()
        return jsonify({"ok": True, "watchlist": cur})

    if sym in cur:
        return jsonify({"ok": False, "error": f"{sym} is already on the watchlist"}), 409
    if len(cur) >= 12:
        return jsonify({"ok": False, "error": "watchlist capped at 12 names — focus is the feature"}), 400
    try:                                               # validate: real Bybit linear perp?
        ticker = S.bybit_ticker(sym)
    except Exception:
        return jsonify({"ok": False, "error": f"{sym} is not a tradable Bybit USDT perp"}), 400

    cur.append(sym)
    store.set_watchlist(cur)
    cg = wl.resolve_cg_id(sym)
    if cg:
        store.add_cg_id(sym, cg)
    _refresh_insights()
    streamed = sorted(set(C.SYMBOLS) | set(C.WHALE_SYMBOLS))
    return jsonify({
        "ok": True, "watchlist": cur, "price": ticker["price"], "cg_id": cg,
        "note": (f"ATH tracking via CoinGecko '{cg}'" if cg
                 else "no CoinGecko match — ATH distance unavailable for this name"),
        "streams_note": ("liquidation/whale streams stay on "
                         + "/".join(s.replace("USDT", "") for s in streamed)
                         + " until a restart (env LIQ_SYMBOLS / WHALE_SYMBOLS)"),
    })


@app.route("/test-alert")
def test_alert():
    """Fire a test notification through every configured channel (post-deploy check)."""
    notify("Signalia — test alert",
           "If you can read this, alert delivery works. The ladder has your back.")
    return jsonify({
        "sent": True,
        "telegram_configured": bool(C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID),
        "email_configured": bool(C.SMTP_HOST and C.SMTP_USER and C.EMAIL_TO),
        "note": "console always prints; telegram/email only fire when configured",
    })


_scheduler = BackgroundScheduler(daemon=True)
_scheduler.add_job(_job, "interval", minutes=C.RUN_INTERVAL_MIN)
_scheduler.add_job(_scan_job, "interval", minutes=max(30, C.RUN_INTERVAL_MIN * 2))
_scheduler.add_job(_watchdog, "interval", minutes=5)

_job()
_scan_job()
_scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
