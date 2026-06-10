"""
Signalia — time-series persistence (SQLite, stdlib only).

Logs every engine reading so adaptive thresholds have a rolling baseline and
the dashboard has history. On Render, point DB_FILE at the persistent disk.
"""
import json
import sqlite3
import time

import config as C

_FIELDS = ["funding", "oi_change", "fng", "btc_price",
           "structural", "sentiment", "target", "liq_flush", "overheat",
           "whale_bias", "retail_bias"]


def _conn():
    c = sqlite3.connect(C.DB_FILE, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                ts          REAL PRIMARY KEY,
                funding     REAL, oi_change REAL, fng REAL, btc_price REAL,
                structural  REAL, sentiment REAL, target REAL, liq_flush REAL,
                snapshot    TEXT
            )""")
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # v3 migration: columns added after first release
        cols = {r[1] for r in c.execute("PRAGMA table_info(readings)")}
        for col in ("overheat", "whale_bias", "retail_bias"):
            if col not in cols:
                c.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")


def insert_reading(snap):
    row = {f: snap.get(f) for f in _FIELDS}
    if row.get("target") is None:   # target lives nested under ladder
        row["target"] = (snap.get("ladder") or {}).get("target")
    if snap.get("overheat_raw") is not None:
        # persist the RAW composite, not the adaptive percentile — otherwise the
        # rolling baseline would feed on its own output and drift
        row["overheat"] = snap["overheat_raw"]
    row["ts"] = time.time()
    row["snapshot"] = json.dumps(snap, default=str)
    cols = ", ".join(row.keys())
    ph = ", ".join("?" for _ in row)
    with _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO readings ({cols}) VALUES ({ph})",
                  list(row.values()))


def recent_values(field, days):
    """All non-null values of `field` within the last `days`, oldest->newest."""
    cutoff = time.time() - days * 86400
    with _conn() as c:
        rows = c.execute(
            f"SELECT {field} FROM readings WHERE ts >= ? AND {field} IS NOT NULL "
            f"ORDER BY ts ASC", (cutoff,)).fetchall()
    return [r[0] for r in rows]


def recent_rows(limit=200):
    """Most recent readings (numeric fields only), oldest -> newest."""
    with _conn() as c:
        rows = c.execute(
            f"SELECT ts, {', '.join(_FIELDS)} FROM readings "
            f"ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---- Runtime watchlist (editable from the dashboard; env is just the seed) ---
def get_watchlist():
    raw = get_meta("watchlist")
    if raw:
        try:
            syms = json.loads(raw)
            if isinstance(syms, list):
                return syms
        except Exception:
            pass
    return list(C.WATCHLIST)


def set_watchlist(symbols):
    set_meta("watchlist", json.dumps(list(symbols)))


def get_cg_map():
    """CoinGecko id map: hardcoded seeds merged with runtime-resolved entries."""
    merged = dict(C.CG_IDS)
    raw = get_meta("cg_ids")
    if raw:
        try:
            merged.update(json.loads(raw))
        except Exception:
            pass
    return merged


def add_cg_id(symbol, cg_id):
    extra = {}
    raw = get_meta("cg_ids")
    if raw:
        try:
            extra = json.loads(raw)
        except Exception:
            pass
    extra[symbol] = cg_id
    set_meta("cg_ids", json.dumps(extra))


def set_meta(k, v):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))


def get_meta(k, default=None):
    with _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return r[0] if r else default
