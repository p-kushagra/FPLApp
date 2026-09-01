"""Default job runner: a thread pool plus the `job` table for durability.

ADR-002. The heavy jobs are I/O-bound HTTP fan-outs, not CPU work, so the GIL is
not the bottleneck -- the rate limiter is. A broker would add two failure modes
and a second process to a single-user localhost app without making any of this
faster.

Two implementation details carry most of the risk:

1. **One executor per process, ever.** Streamlit re-runs the whole script on
   every interaction. Creating a pool at module scope per run leaks threads
   until the machine crawls, roughly half an hour into a session. The executor
   is a module-level singleton and `get_runner()` is what callers use.

2. **Each job opens its own connection.** SQLite connections are not safe to
   share across threads, so a worker never touches the caller's connection.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..db import connect
from . import base
from .base import JobState, JobStatus

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 4

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def get_executor(max_workers: int = DEFAULT_WORKERS) -> ThreadPoolExecutor:
    """The process-wide pool. Created once; never per script run."""
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadPoolExecutor(
                    max_workers=max_workers, thread_name_prefix="fpl-job")
    return _EXECUTOR


def shutdown_executor(wait: bool = False) -> None:
    """Tear the pool down. For tests and for a clean process exit."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=wait)
            _EXECUTOR = None


class LocalThreadRunner:
    """In-process runner. Durable through `job`, not through a broker."""

    name = "local"

    def __init__(self, db_path: Path, registry: dict[str, Callable] | None = None,
                 max_workers: int = DEFAULT_WORKERS, synchronous: bool = False):
        self.db_path = Path(db_path)
        self.registry = registry if registry is not None else {}
        self.max_workers = max_workers
        # `synchronous` runs jobs inline. Used by tests, and available as the
        # JOB_RUNNER=sync escape hatch when threads are the suspect.
        self.synchronous = synchronous
        self._futures: dict[str, Future] = {}

    # -- submission --------------------------------------------------------
    def submit(self, name: str, priority: int = 5, **kwargs: Any) -> str:
        if name not in self.registry:
            raise KeyError(f"unknown job {name!r}; registered: {sorted(self.registry)}")

        job_id = base.new_job_id()
        conn = connect(self.db_path)
        try:
            base.record_queued(conn, job_id, name, kwargs, priority, self.name)
        finally:
            conn.close()

        if self.synchronous:
            self._run(job_id, name, kwargs)
        else:
            self._futures[job_id] = get_executor(self.max_workers).submit(
                self._run, job_id, name, kwargs)
        return job_id

    # -- execution ---------------------------------------------------------
    def _run(self, job_id: str, name: str, kwargs: dict) -> Any:
        """Worker body. Owns its connection; never raises to the pool."""
        conn = connect(self.db_path)
        try:
            base.mark_running(conn, job_id)
            fn = self.registry[name]

            def report(progress: float, note: str = "") -> None:
                base.heartbeat(conn, job_id, progress, note)

            result = fn(conn=conn, progress=report, **kwargs)
            base.mark_done(conn, job_id, result)
            return result
        except Exception as exc:  # noqa: BLE001 - a failed job is data, not a crash
            log.warning("job %s (%s) failed: %s", job_id, name, exc)
            try:
                base.mark_failed(conn, job_id, repr(exc))
            except Exception:
                log.exception("could not record failure for job %s", job_id)
            return None
        finally:
            conn.close()

    # -- inspection --------------------------------------------------------
    def status(self, job_id: str) -> JobStatus | None:
        conn = connect(self.db_path)
        try:
            return base.read_status(conn, job_id)
        finally:
            conn.close()

    def pending(self) -> list[JobStatus]:
        conn = connect(self.db_path)
        try:
            return base.pending(conn)
        finally:
            conn.close()

    def wait(self, job_id: str, timeout: float | None = None) -> JobStatus | None:
        """Block until a job finishes. Tests and CLI use this; the UI does not."""
        future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.status(job_id)

    def cancel(self, job_id: str) -> bool:
        future = self._futures.get(job_id)
        cancelled = future.cancel() if future is not None else False
        if cancelled:
            conn = connect(self.db_path)
            try:
                conn.execute("UPDATE job SET state = ? WHERE job_id = ?",
                             (JobState.CANCELLED.value, job_id))
                conn.commit()
            finally:
                conn.close()
        return cancelled

    # -- recovery ----------------------------------------------------------
    def reap_stale(self, heartbeat_timeout_s: int = 300) -> int:
        conn = connect(self.db_path)
        try:
            return base.reap_stale(conn, heartbeat_timeout_s)
        finally:
            conn.close()

    def requeue_stale(self, heartbeat_timeout_s: int = 300) -> list[str]:
        """Reap orphans, then re-enqueue those with attempts remaining."""
        self.reap_stale(heartbeat_timeout_s)
        conn = connect(self.db_path)
        try:
            candidates = base.retryable(conn)
        finally:
            conn.close()

        requeued = []
        for row in candidates:
            if row["name"] not in self.registry:
                continue
            import json
            try:
                args = json.loads(row["args"] or "{}")
            except (TypeError, ValueError):
                args = {}
            requeued.append(self.submit(row["name"], **args))
        return requeued
