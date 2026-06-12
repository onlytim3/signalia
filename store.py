"""
Signalia — time-series persistence (SQLite, stdlib only) + snapshot backup.

Logs every engine reading so adaptive thresholds have a rolling baseline and
the dashboard has history. On Render, point DB_FILE at the persistent disk.

Free-tier amnesia fix: when BACKUP_REPO/BACKUP_TOKEN are set, backup_db()
pushes a gzipped snapshot of the DB to a private GitHub repo on a schedule,
and init_db() restores it on boot if the local DB is missing or empty — so
baselines and scorecard history survive ephemeral-disk restarts.
"""
import base64
import gzip
import json
import os
import sqlite3
import tempfile
import time

import requests

import config as C

GH_API = "https://api.github.com"

_FIELDS = ["funding", "oi_change", "fng", "btc_price",
           "structural", "sentiment", "target", "liq_flush", "overheat",
           "whale_bias", "retail_bias"]


def _conn():
    c = sqlite3.connect(C.DB_FILE, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ---- Snapshot backup / restore (GitHub contents API; optional) ----------------
def _backup_enabled():
    return bool(C.BACKUP_REPO and C.BACKUP_TOKEN)


def _gh_headers(raw=False):
    return {"Authorization": f"Bearer {C.BACKUP_TOKEN}",
            "Accept": "application/vnd.github.raw+json" if raw
                      else "application/vnd.github+json",
            "User-Agent": "signalia/3.0"}


def _contents_url():
    return f"{GH_API}/repos/{C.BACKUP_REPO}/contents/{C.BACKUP_DB_PATH}"


def backup_db():
    """Push a gzipped, transactionally-consistent snapshot of the DB."""
    if not _backup_enabled():
        return {"ok": False, "reason": "backup not configured"}
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src, dst = sqlite3.connect(C.DB_FILE, timeout=10), sqlite3.connect(tmp)
        with dst:
            src.backup(dst)             # consistent copy even mid-write
        src.close(); dst.close()
        with open(tmp, "rb") as f:
            blob = gzip.compress(f.read())
    finally:
        os.unlink(tmp)
    sha = None                          # updating an existing file needs its sha
    r = requests.get(_contents_url(), headers=_gh_headers(),
                     params={"ref": C.BACKUP_BRANCH}, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
    body = {"message": "db snapshot "
                       + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "content": base64.b64encode(blob).decode(), "branch": C.BACKUP_BRANCH}
    if sha:
        body["sha"] = sha
    r = requests.put(_contents_url(), headers=_gh_headers(), json=body, timeout=60)
    ok = r.status_code in (200, 201)
    if not ok:
        print("backup_db failed:", r.status_code, r.text[:200])
    return {"ok": ok, "bytes": len(blob), "http": r.status_code}


def restore_db():
    """Pull the latest snapshot when the local DB is missing or has no readings
    (a fresh boot on an ephemeral disk). NEVER overwrites real local history."""
    if not _backup_enabled():
        return {"ok": False, "reason": "backup not configured"}
    if os.path.exists(C.DB_FILE):
        try:
            with _conn() as c:
                n = c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            if n > 0:
                return {"ok": False, "reason": f"local db already has {n} readings"}
        except sqlite3.Error:
            pass                        # unreadable/empty file -> restore over it
    r = requests.get(_contents_url(), headers=_gh_headers(raw=True),
                     params={"ref": C.BACKUP_BRANCH}, timeout=60)
    if r.status_code != 200:
        return {"ok": False, "reason": f"no snapshot ({r.status_code})"}
    with open(C.DB_FILE, "wb") as f:
        f.write(gzip.decompress(r.content))
    return {"ok": True, "bytes": len(r.content)}


def init_db():
    try:                                # no-op unless configured AND db empty
        res = restore_db()
        if res.get("ok"):
            print("store: restored db snapshot from backup repo,",
                  res["bytes"], "bytes (gz)")
    except Exception as e:
        print("store: restore_db error:", e)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                ts          REAL PRIMARY KEY,
                funding     REAL, oi_change REAL, fng REAL, btc_price REAL,
                structural  REAL, sentiment REAL, target REAL, liq_flush REAL,
                snapshot    TEXT
            )""")
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # scan-driven watchlist discovery: every extreme-screen appearance
        c.execute("""
            CREATE TABLE IF NOT EXISTS candidate_hits (
                ts REAL, symbol TEXT, screen TEXT,
                chg24h REAL, funding REAL, turnover REAL
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cand_sym "
                  "ON candidate_hits (symbol, ts)")
        # v3 migration: columns added after first release
        cols = {r[1] for r in c.execute("PRAGMA table_info(readings)")}
        for col in ("overheat", "whale_bias", "retail_bias"):
            if col not in cols:
                c.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")


# ---- Watchlist-candidate hits (scan-driven discovery) --------------------------
def add_candidate_hits(hits):
    """hits: [(ts, symbol, screen, chg24h, funding, turnover), ...]"""
    if not hits:
        return
    with _conn() as c:
        c.executemany("INSERT INTO candidate_hits VALUES (?, ?, ?, ?, ?, ?)", hits)


def candidate_hits(days):
    """All hits in the window, oldest -> newest; prunes anything older."""
    cutoff = time.time() - days * 86400
    with _conn() as c:
        c.execute("DELETE FROM candidate_hits WHERE ts < ?", (cutoff,))
        rows = c.execute(
            "SELECT ts, symbol, screen, chg24h, funding, turnover "
            "FROM candidate_hits WHERE ts >= ? ORDER BY ts ASC", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


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
