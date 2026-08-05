"""Durable job state in SQLite.

A migration of a real repository runs for hours and will be interrupted - a reboot,
a dropped VPN, an operator pressing Stop. Every stage records its outcome here, so
re-running the same command resumes from the first stage that is not `succeeded`
instead of starting over.

The database is also what the web UI reads; it is deliberately the single source of
truth for progress so CLI and UI can never disagree.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .logging_setup import get_logger

log = get_logger("state")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    repo         TEXT NOT NULL,
    config_path  TEXT,
    workdir      TEXT,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    error        TEXT,
    summary      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_repo ON jobs(repo);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS stages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  REAL,
    finished_at REAL,
    progress    REAL NOT NULL DEFAULT 0,
    message     TEXT,
    error       TEXT,
    result      TEXT,
    UNIQUE(job_id, name)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    path     TEXT NOT NULL,
    created_at REAL NOT NULL,
    meta     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT,
    at        REAL NOT NULL,
    level     TEXT NOT NULL,
    stage     TEXT,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS sync_state (
    repo            TEXT PRIMARY KEY,
    last_svn_rev    INTEGER,
    last_sync_at    REAL,
    last_push_at    REAL,
    pushed_head     TEXT,
    cutover_at      REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
"""


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class StageRecord:
    name: str
    status: StageStatus
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress: float = 0.0
    message: str = ""
    error: str = ""
    result: Optional[Dict[str, Any]] = None

    @property
    def duration(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        if self.started_at:
            return time.time() - self.started_at
        return 0.0


class StateStore:
    """Thread-safe SQLite wrapper. One instance per process is expected."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        # executescript issues its own COMMIT, so it must not run inside our
        # explicit BEGIN IMMEDIATE transaction.
        conn = self._conn()
        with self._write_lock:
            conn.executescript(_SCHEMA)
        with self._connect() as txn:
            txn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # -- plumbing ------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- jobs ----------------------------------------------------------------

    def create_job(self, name: str, repo: str, config_path: str = "", workdir: str = "") -> str:
        job_id = uuid.uuid4().hex[:16]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id, name, repo, config_path, workdir, status, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, name, repo, config_path, workdir, JobStatus.PENDING.value, time.time()),
            )
        log.debug("created job %s for repo %s", job_id, repo)
        return job_id

    def start_job(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET status=?, started_at=?, error=NULL WHERE id=?",
                         (JobStatus.RUNNING.value, time.time(), job_id))

    def finish_job(self, job_id: str, status: JobStatus, error: str = "",
                   summary: Optional[Dict[str, Any]] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=?, summary=? WHERE id=?",
                (status.value, time.time(), error or None,
                 json.dumps(summary) if summary else None, job_id),
            )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    # Ties on created_at are not hypothetical: the Windows clock advances in ~15 ms
    # steps, so two jobs created back to back share a timestamp. Ordering by rowid as
    # well makes "the latest job" mean insertion order, which is what decides the job
    # a `migrate` re-run resumes.
    _JOB_ORDER = "ORDER BY created_at DESC, rowid DESC"

    def latest_job(self, repo: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            f"SELECT * FROM jobs WHERE repo=? {self._JOB_ORDER} LIMIT 1", (repo,)
        ).fetchone()
        return _job_row(row) if row else None

    def list_jobs(self, limit: int = 100, repo: Optional[str] = None) -> List[Dict[str, Any]]:
        if repo:
            rows = self._conn().execute(
                f"SELECT * FROM jobs WHERE repo=? {self._JOB_ORDER} LIMIT ?", (repo, limit)
            ).fetchall()
        else:
            rows = self._conn().execute(
                f"SELECT * FROM jobs {self._JOB_ORDER} LIMIT ?", (limit,)
            ).fetchall()
        return [_job_row(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.execute("DELETE FROM stages WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM events WHERE job_id=?", (job_id,))

    # -- stages --------------------------------------------------------------

    def stage_start(self, job_id: str, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stages(job_id, name, status, started_at, progress) VALUES(?,?,?,?,0) "
                "ON CONFLICT(job_id, name) DO UPDATE SET status=excluded.status, "
                "started_at=excluded.started_at, finished_at=NULL, error=NULL, progress=0",
                (job_id, name, StageStatus.RUNNING.value, time.time()),
            )

    def stage_progress(self, job_id: str, name: str, progress: float, message: str = "") -> None:
        progress = max(0.0, min(1.0, float(progress)))
        with self._connect() as conn:
            conn.execute("UPDATE stages SET progress=?, message=? WHERE job_id=? AND name=?",
                         (progress, message or None, job_id, name))

    def stage_finish(self, job_id: str, name: str, status: StageStatus,
                     result: Optional[Dict[str, Any]] = None, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stages(job_id, name, status, finished_at, progress, result, error) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id, name) DO UPDATE SET status=excluded.status, "
                "finished_at=excluded.finished_at, result=excluded.result, error=excluded.error, "
                "progress=CASE WHEN excluded.status='succeeded' THEN 1.0 ELSE stages.progress END",
                (job_id, name, status.value, time.time(),
                 1.0 if status == StageStatus.SUCCEEDED else 0.0,
                 json.dumps(result, default=str) if result is not None else None,
                 error or None),
            )

    def get_stage(self, job_id: str, name: str) -> Optional[StageRecord]:
        row = self._conn().execute(
            "SELECT * FROM stages WHERE job_id=? AND name=?", (job_id, name)
        ).fetchone()
        return _stage_row(row) if row else None

    def list_stages(self, job_id: str) -> List[StageRecord]:
        rows = self._conn().execute(
            "SELECT * FROM stages WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
        return [_stage_row(r) for r in rows]

    def stage_succeeded(self, job_id: str, name: str) -> bool:
        stage = self.get_stage(job_id, name)
        return bool(stage and stage.status == StageStatus.SUCCEEDED)

    def stage_result(self, job_id: str, name: str) -> Optional[Dict[str, Any]]:
        stage = self.get_stage(job_id, name)
        return stage.result if stage else None

    def reset_stages_from(self, job_id: str, name: str) -> None:
        """Invalidate `name` and everything recorded after it (used by --restart-from)."""
        row = self._conn().execute("SELECT id FROM stages WHERE job_id=? AND name=?",
                                   (job_id, name)).fetchone()
        with self._connect() as conn:
            if row:
                conn.execute("DELETE FROM stages WHERE job_id=? AND id>=?", (job_id, row["id"]))
            else:
                conn.execute("DELETE FROM stages WHERE job_id=?", (job_id,))

    # -- artifacts & events --------------------------------------------------

    def add_artifact(self, job_id: str, kind: str, path: str,
                     meta: Optional[Dict[str, Any]] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO artifacts(job_id, kind, path, created_at, meta) VALUES(?,?,?,?,?)",
                (job_id, kind, str(path), time.time(), json.dumps(meta) if meta else None),
            )

    def list_artifacts(self, job_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["meta"] = json.loads(r["meta"]) if r["meta"] else {}
            out.append(item)
        return out

    def add_event(self, job_id: Optional[str], level: str, message: str, stage: str = "") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO events(job_id, at, level, stage, message) VALUES(?,?,?,?,?)",
                         (job_id, time.time(), level, stage or None, message))

    def list_events(self, job_id: str, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
            (job_id, after_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- incremental sync ----------------------------------------------------

    def get_sync_state(self, repo: str) -> Dict[str, Any]:
        row = self._conn().execute("SELECT * FROM sync_state WHERE repo=?", (repo,)).fetchone()
        return dict(row) if row else {
            "repo": repo, "last_svn_rev": None, "last_sync_at": None, "last_push_at": None,
            "pushed_head": None, "cutover_at": None, "consecutive_failures": 0, "note": None,
        }

    def update_sync_state(self, repo: str, **fields: Any) -> None:
        allowed = {"last_svn_rev", "last_sync_at", "last_push_at", "pushed_head",
                   "cutover_at", "consecutive_failures", "note"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown sync_state fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO sync_state(repo) VALUES(?)", (repo,))
            conn.execute(f"UPDATE sync_state SET {assignments} WHERE repo=?", (*values, repo))

    def list_sync_state(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM sync_state ORDER BY repo").fetchall()
        return [dict(r) for r in rows]


def _job_row(row: sqlite3.Row) -> Dict[str, Any]:
    job = dict(row)
    job["summary"] = json.loads(row["summary"]) if row["summary"] else None
    return job


def _stage_row(row: sqlite3.Row) -> StageRecord:
    return StageRecord(
        name=row["name"],
        status=StageStatus(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        progress=row["progress"] or 0.0,
        message=row["message"] or "",
        error=row["error"] or "",
        result=json.loads(row["result"]) if row["result"] else None,
    )
