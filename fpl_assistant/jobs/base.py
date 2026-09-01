"""Job runner protocol and shared state model.

Two implementations share this contract: `LocalThreadRunner` (default) and
`CeleryRunner` (opt-in). The task bodies in `tasks.py` are plain functions
registered by both, so switching backends changes submission only -- never the
work itself.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"        # orphaned by a killed process; re-enqueued on boot
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    name: str
    state: JobState
    progress: float = 0.0
    progress_note: str = ""
    attempts: int = 0
    result: Any = None
    error: str | None = None
    duration_seconds: float | None = None

    @property
    def running(self) -> bool:
        return self.state is JobState.RUNNING

    @property
    def ok(self) -> bool:
        return self.state is JobState.DONE


class JobRunner(Protocol):
    def submit(self, name: str, **kwargs: Any) -> str: ...
    def status(self, job_id: str) -> JobStatus | None: ...
    def cancel(self, job_id: str) -> bool: ...
    def reap_stale(self, heartbeat_timeout_s: int = 300) -> int: ...


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------
# `job` table access, shared by both runners
# --------------------------------------------------------------------------
def record_queued(conn: sqlite3.Connection, job_id: str, name: str,
                  args: dict, priority: int, runner: str,
                  max_attempts: int = 3) -> None:
    """Write the row BEFORE the work is dispatched.

    Order matters: if the process dies between the insert and the dispatch, the
    job is visible as queued rather than silently lost.
    """
    conn.execute(
        """INSERT INTO job
             (job_id, name, args, state, priority, attempts, max_attempts,
              progress, runner, enqueued_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, 0.0, ?, ?)""",
        (job_id, name, json.dumps(args, default=str), JobState.QUEUED.value,
         priority, max_attempts, runner, _now()),
    )
    conn.commit()


def mark_running(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute(
        """UPDATE job SET state = ?, started_at = ?, heartbeat_at = ?,
                          attempts = attempts + 1
           WHERE job_id = ?""",
        (JobState.RUNNING.value, _now(), _now(), job_id),
    )
    conn.commit()


def heartbeat(conn: sqlite3.Connection, job_id: str,
              progress: float | None = None, note: str | None = None) -> None:
    """Prove liveness. A job that stops heartbeating is reaped as stale."""
    if progress is None and note is None:
        conn.execute("UPDATE job SET heartbeat_at = ? WHERE job_id = ?",
                     (_now(), job_id))
    else:
        conn.execute(
            """UPDATE job SET heartbeat_at = ?,
                              progress = COALESCE(?, progress),
                              progress_note = COALESCE(?, progress_note)
               WHERE job_id = ?""",
            (_now(), progress, note, job_id),
        )
    conn.commit()


def mark_done(conn: sqlite3.Connection, job_id: str, result: Any = None) -> None:
    conn.execute(
        """UPDATE job SET state = ?, finished_at = ?, progress = 1.0, result = ?
           WHERE job_id = ?""",
        (JobState.DONE.value, _now(), json.dumps(result, default=str), job_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    conn.execute(
        "UPDATE job SET state = ?, finished_at = ?, error = ? WHERE job_id = ?",
        (JobState.FAILED.value, _now(), str(error)[:1000], job_id),
    )
    conn.commit()


def read_status(conn: sqlite3.Connection, job_id: str) -> JobStatus | None:
    row = conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None

    duration = None
    if row["started_at"] and row["finished_at"]:
        try:
            start = dt.datetime.fromisoformat(row["started_at"])
            end = dt.datetime.fromisoformat(row["finished_at"])
            duration = (end - start).total_seconds()
        except ValueError:
            duration = None

    result = None
    if row["result"]:
        try:
            result = json.loads(row["result"])
        except (TypeError, ValueError):
            result = row["result"]

    return JobStatus(
        job_id=row["job_id"], name=row["name"], state=JobState(row["state"]),
        progress=float(row["progress"] or 0.0),
        progress_note=row["progress_note"] or "",
        attempts=int(row["attempts"] or 0),
        result=result, error=row["error"], duration_seconds=duration,
    )


def pending(conn: sqlite3.Connection) -> list[JobStatus]:
    rows = conn.execute(
        """SELECT job_id FROM job WHERE state IN (?, ?)
           ORDER BY priority DESC, enqueued_at""",
        (JobState.QUEUED.value, JobState.RUNNING.value),
    ).fetchall()
    return [s for s in (read_status(conn, r["job_id"]) for r in rows) if s]


def reap_stale(conn: sqlite3.Connection, heartbeat_timeout_s: int = 300) -> int:
    """Mark jobs orphaned by a killed process.

    The documented consequence of ADR-002: durability comes from this table, not
    from a broker, so a hard kill leaves `running` rows that nothing will ever
    finish. Reaping on boot makes them visible instead of mysterious.
    """
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(seconds=heartbeat_timeout_s)).isoformat()
    cur = conn.execute(
        """UPDATE job SET state = ?, error = 'orphaned: no heartbeat'
           WHERE state = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
        (JobState.STALE.value, JobState.RUNNING.value, cutoff),
    )
    conn.commit()
    return cur.rowcount


def retryable(conn: sqlite3.Connection) -> list[dict]:
    """Stale jobs still inside their attempt budget."""
    return [
        dict(r) for r in conn.execute(
            """SELECT job_id, name, args FROM job
               WHERE state = ? AND attempts < max_attempts""",
            (JobState.STALE.value,),
        )
    ]
