import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import DB_PATH, DEFAULTS

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, content_id TEXT UNIQUE, title_id TEXT, title TEXT,
  version TEXT, category TEXT, region TEXT, min_fw TEXT, size INTEGER,
  path TEXT, has_icon INTEGER, added REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT, source TEXT, item_id INTEGER,
  status TEXT DEFAULT 'queued', progress REAL DEFAULT 0, message TEXT DEFAULT '',
  created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
""")


def _migrate():
    def cols(table):
        return {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for table, name, decl in (("jobs", "autosend", "TEXT DEFAULT ''"),
                              ("jobs", "parent_id", "INTEGER"),
                              ("jobs", "attempts", "INTEGER DEFAULT 0"),
                              ("jobs", "retry_at", "REAL DEFAULT 0"),
                              ("items", "source_url", "TEXT")):
        if name not in cols(table):
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_migrate()
_conn.execute("CREATE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(item_id)")
# Jobs interrupted by a restart go back to the queue; paused jobs stay paused.
_conn.execute("UPDATE jobs SET status='failed', message='Cancelled' WHERE status='cancelled'")
_conn.execute("UPDATE jobs SET status='queued', message='requeued after restart' WHERE status='running'")


@contextmanager
def cur():
    with _lock:
        yield _conn


def q(sql, *args):
    with cur() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def x(sql, *args):
    with cur() as c:
        return c.execute(sql, args).lastrowid


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update({r["key"]: r["value"] for r in q("SELECT key, value FROM settings")})
    return s


def save_settings(values: dict):
    for k, v in values.items():
        if k in DEFAULTS:
            x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, str(v))


def add_job(kind, source="", item_id=None, status="queued", autosend="", parent_id=None) -> int:
    now = time.time()
    return x("INSERT INTO jobs(kind, source, item_id, status, autosend, parent_id, created, updated) VALUES(?,?,?,?,?,?,?,?)",
             kind, source, item_id, status, autosend, parent_id, now, now)


def update_job(job_id, **kw):
    kw["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    x(f"UPDATE jobs SET {cols} WHERE id=?", *kw.values(), job_id)


def finish_job(job_id, status, progress=None, message=None):
    sets = ["status=?", "updated=?"]
    args = [status, time.time()]
    if progress is not None:
        sets.append("progress=?")
        args.append(progress)
    if message is not None:
        sets.append("message=?")
        args.append(message)
    if status == "done":
        sets += ["attempts=0", "retry_at=0"]
    args.append(job_id)
    x(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", *args)
    if status in ("done", "failed"):
        rows = q("SELECT id, kind, item_id, message FROM jobs WHERE id=?", job_id)
        if rows:
            from . import notify
            notify.terminal(rows[0], status)


def backup(dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        target = sqlite3.connect(str(dest))
        try:
            _conn.backup(target)
        finally:
            target.close()


def claim_job(kinds) -> dict | None:
    marks = ",".join("?" * len(kinds))
    with cur() as c:
        row = c.execute(
            f"SELECT * FROM jobs WHERE status='queued' AND kind IN ({marks}) AND retry_at <= ? ORDER BY id LIMIT 1",
            (*kinds, time.time()),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (time.time(), row["id"]))
        return dict(row)
