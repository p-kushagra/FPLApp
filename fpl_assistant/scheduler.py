"""Local background scheduler: routine syncs and the pre-deadline freeze.

The one job in this system that genuinely cannot be run on demand is the
projection freeze. `models.snapshot` must capture the forecast an hour before
the deadline; run it late and the snapshot is contaminated by team news, run it
never and the Process axis and the `ep_next` benchmark stay permanently empty.
Asking a person to remember a 16:30 Friday task every week is not a design.

So this is an APScheduler `BackgroundScheduler` living inside the Streamlit
process. That choice is deliberate over a broker or an OS-level cron:

* It costs nothing and needs no second process, which is the whole local-first
  constraint.
* It shares the SQLite file and the SWR cache with the app, so a sync it
  performs is immediately visible to the page.
* Its weakness is honest and bounded: it only runs while the app is open. Every
  job is therefore written to be *catch-up safe* rather than instant-critical --
  `freeze_projections` refuses a gameweek already frozen, one too far out and
  one past its deadline, so running it every ten minutes is correct and running
  it late is harmless. Missing a freeze entirely is recoverable with a forced
  capture, which is recorded as late in `deadline_source`.

Streamlit re-runs the whole script on every interaction, so the scheduler is a
process-wide singleton behind a lock. Creating one per script run would spawn a
scheduler thread per click.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Poll intervals. The freeze check is frequent because it is cheap and its
# window is narrow; the data syncs are slow because the upstream data is.
FREEZE_CHECK_MINUTES = 10
PRICE_SNAPSHOT_MINUTES = 60
REFERENCE_REFRESH_MINUTES = 180
XP_RECOMPUTE_MINUTES = 120
MINI_LEAGUE_MINUTES = 360
RIVAL_FREEZE_MINUTES = 20

_SCHEDULER: Any = None
_LOCK = threading.Lock()
_HISTORY: list[JobRun] = []
_HISTORY_LIMIT = 50


@dataclass
class JobRun:
    """One completed scheduler tick, for the status panel."""

    name: str
    started_at: str
    seconds: float
    ok: bool
    detail: str = ""


@dataclass
class SchedulerStatus:
    running: bool = False
    jobs: list[dict] = field(default_factory=list)
    history: list[JobRun] = field(default_factory=list)
    available: bool = True
    error: str | None = None


def available() -> bool:
    """Is APScheduler installed? The app must run fully without it."""
    try:
        import apscheduler  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------
# Job wrappers
# --------------------------------------------------------------------------
def _record(name: str, started: dt.datetime, ok: bool, detail: str) -> None:
    _HISTORY.append(JobRun(
        name=name, started_at=started.isoformat(timespec="seconds"),
        seconds=round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 2),
        ok=ok, detail=detail[:200]))
    del _HISTORY[:-_HISTORY_LIMIT]


def _run_task(db_path: Path, name: str, **kwargs) -> None:
    """Execute one registered job on its own connection.

    Never raises. A scheduler job that throws kills its own next run in some
    APScheduler configurations, and a background sync failing must never be
    able to take the foreground app with it.
    """
    from .db import connect
    from .jobs import tasks

    started = dt.datetime.now(dt.timezone.utc)
    func = tasks.REGISTRY.get(name)
    if func is None:
        _record(name, started, False, "no such job")
        return

    conn = None
    try:
        conn = connect(db_path)
        result = func(conn, **kwargs)
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        _record(name, started, ok, _summarise(name, result))
    except Exception as exc:                     # noqa: BLE001 - see docstring
        log.warning("scheduled job %s failed: %s", name, exc)
        _record(name, started, False, str(exc))
    finally:
        if conn is not None:
            conn.close()


def _summarise(name: str, result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)[:120]
    if name == "freeze_projections":
        frozen = result.get("frozen") or []
        if frozen:
            return "froze " + ", ".join(
                f"GW{r['gw']} ({r['rows']} rows)" for r in frozen)
        skipped = result.get("skipped") or []
        if skipped:
            return "; ".join(f"GW{r['gw']}: {r['reason']}" for r in skipped)
        return str(result.get("note", "nothing due"))
    keys = ("snapshots", "changes", "projections", "gameweeks", "rows", "verdict")
    parts = [f"{k}={result[k]}" for k in keys if k in result]
    return ", ".join(parts) or "ok"


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def start(db_path: Path, *, freeze_minutes: int = FREEZE_CHECK_MINUTES,
          include_syncs: bool = True) -> SchedulerStatus:
    """Start the background scheduler once per process. Idempotent."""
    global _SCHEDULER

    if not available():
        return SchedulerStatus(
            available=False,
            error="APScheduler is not installed - run: pip install APScheduler")

    with _LOCK:
        if _SCHEDULER is not None and getattr(_SCHEDULER, "running", False):
            return status()

        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        # coalesce + max_instances=1: after the laptop wakes from sleep, a
        # scheduler with a backlog would otherwise fire every missed run at
        # once and stampede the rate limiter.
        scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 600})

        scheduler.add_job(
            _run_task, IntervalTrigger(minutes=freeze_minutes),
            args=[db_path, "freeze_projections"], id="freeze_projections",
            name="Pre-deadline projection freeze", replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc))

        if include_syncs:
            for job_id, minutes, label in (
                    ("snapshot_prices", PRICE_SNAPSHOT_MINUTES,
                     "Price and transfer-flow snapshot"),
                    ("refresh_reference", REFERENCE_REFRESH_MINUTES,
                     "Bootstrap, fixtures and gameweek state"),
                    ("recompute_xp", XP_RECOMPUTE_MINUTES,
                     "Expected-points projection"),
                    ("ingest_mini_league", MINI_LEAGUE_MINUTES,
                     "Mini-league standings"),
                    # Gated on the deadline having passed and idempotent per
                    # entry, so a frequent tick is free outside a live gameweek.
                    ("freeze_rivals", RIVAL_FREEZE_MINUTES,
                     "Rival squad freeze (post-deadline)")):
                scheduler.add_job(
                    _run_task, IntervalTrigger(minutes=minutes),
                    args=[db_path, job_id], id=job_id, name=label,
                    replace_existing=True)

        scheduler.start()
        _SCHEDULER = scheduler
        log.info("background scheduler started")

    return status()


def shutdown(wait: bool = False) -> None:
    """Stop the scheduler. For tests and clean process exit."""
    global _SCHEDULER
    with _LOCK:
        if _SCHEDULER is not None:
            try:
                _SCHEDULER.shutdown(wait=wait)
            except Exception:                     # already stopping
                pass
            _SCHEDULER = None


def status() -> SchedulerStatus:
    """What the scheduler is doing, for the app's status panel."""
    if not available():
        return SchedulerStatus(
            available=False,
            error="APScheduler is not installed - run: pip install APScheduler")
    if _SCHEDULER is None or not getattr(_SCHEDULER, "running", False):
        return SchedulerStatus(running=False, history=list(reversed(_HISTORY)))

    jobs = []
    for job in _SCHEDULER.get_jobs():
        nxt = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": nxt.isoformat(timespec="seconds") if nxt else "-",
            "minutes_away": (round((nxt - dt.datetime.now(dt.timezone.utc))
                                   .total_seconds() / 60.0, 1) if nxt else None),
        })
    return SchedulerStatus(running=True, jobs=jobs,
                           history=list(reversed(_HISTORY)))


def run_now(db_path: Path, name: str, **kwargs) -> JobRun | None:
    """Fire one job immediately, on the calling thread. For the manual button."""
    _run_task(db_path, name, **kwargs)
    return _HISTORY[-1] if _HISTORY else None
