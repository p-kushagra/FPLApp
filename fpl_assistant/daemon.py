"""Standalone background scheduler daemon.

`scheduler.py` runs jobs *inside* the Streamlit process, which is convenient but
only alive while a browser tab is open. The pre-deadline projection freeze is
the one task that cannot tolerate that: it has to fire an hour before a Friday
evening deadline whether or not anyone opened the dashboard, and a missed freeze
is unrecoverable -- `projection_snapshot` is write-once, and a late capture is
recorded as contaminated rather than clean.

So this is a separate long-lived process, started alongside the UI by
`launch_fpl.bat` and stopped by `stop_fpl.bat`.

Three schedules, each matched to how fast the thing it watches actually moves:

* **Matchday poll, every 60s.** Gated on the temporal phase, so it is a cheap
  no-op outside a live gameweek. Polling the live endpoint on a Tuesday would
  burn the rate budget for nothing.
* **Pre-deadline freeze, exactly one hour before the lock.** A supervisor job
  reads the next deadline and arms a one-shot timer at deadline minus 60
  minutes. A slower safety net also runs, so a daemon started *inside* the
  window, or one whose deadline moved after arming, still captures.
* **Price monitor, 01:15 UTC nightly.** FPL applies price changes in a batch at
  roughly 01:30; 01:15 samples the transfer flow just before it, which is what
  makes the velocity model's threshold calibration possible.

Everything here is defensive by construction. A job that raises must never kill
the scheduler, and shutdown must never interrupt a write -- SQLite in WAL mode
survives a hard kill, but a half-finished multi-statement freeze would leave a
partial gameweek that the write-once guard then refuses to complete.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Matchday polling cadence. FPL's own live endpoint updates on roughly this
# beat, so anything faster only costs rate budget.
LIVE_POLL_SECONDS = 60

# How far ahead of the deadline the projection freeze fires.
FREEZE_LEAD_MINUTES = 60

# The supervisor re-reads deadlines on this cadence and re-arms the one-shot
# timer. Also the safety net that catches a daemon started inside the window.
SUPERVISOR_MINUTES = 15

# Nightly price batch. FPL applies changes ~01:30 UTC; sample just before.
PRICE_HOUR_UTC, PRICE_MINUTE_UTC = 1, 15

# Reference data (deadlines, fixtures, bootstrap) refresh cadence.
REFERENCE_MINUTES = 180

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5

log = logging.getLogger("fpl.daemon")


# --------------------------------------------------------------------------
# Logging and pid file
# --------------------------------------------------------------------------
def configure_logging(log_path: Path, verbose: bool = False) -> None:
    """File logging with rotation, plus a console mirror when attached."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("fpl")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    rotating = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8")
    rotating.setFormatter(fmt)
    root.addHandler(rotating)

    # When launched from a .vbs there is no console; a stream handler on a
    # detached stdout raises on write, so it is only attached when usable.
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)


def pid_path(db_path: Path) -> Path:
    return db_path.parent / "daemon.pid"


def stop_path(db_path: Path) -> Path:
    """Sentinel file used to request a graceful stop.

    Windows has no real SIGTERM: `os.kill(pid, SIGTERM)` from another process
    calls TerminateProcess, which kills immediately and never runs the shutdown
    handler. That is precisely the case this daemon must avoid -- a freeze
    interrupted mid-write leaves a partial gameweek the write-once guard will
    then refuse to complete. A sentinel file the main loop polls gives a real
    graceful stop on every platform.
    """
    return db_path.parent / "daemon.stop"


def write_pid(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pid(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def is_running(pid: int) -> bool:
    """Is this pid alive? Windows has no signal 0, so tasklist is the check."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10, check=False).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Job execution
# --------------------------------------------------------------------------
class Daemon:
    """Owns the scheduler, the database path and the shutdown handshake."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.scheduler: Any = None
        self._stopping = False

    # -- one job run ------------------------------------------------------
    def run_job(self, name: str, **kwargs) -> dict | None:
        """Execute a registered job on its own connection. Never raises.

        A scheduler job that throws can suppress its own next run under some
        APScheduler configurations, and a failed background sync must never be
        able to stop the freeze that runs an hour later.
        """
        from .db import connect
        from .jobs import tasks

        func = tasks.REGISTRY.get(name)
        if func is None:
            log.error("no such job: %s", name)
            return None

        started = time.perf_counter()
        conn = None
        try:
            conn = connect(self.db_path)
            result = func(conn, **kwargs)
            elapsed = time.perf_counter() - started
            log.info("%s ok in %.2fs  %s", name, elapsed, _summarise(result))
            return result if isinstance(result, dict) else None
        except Exception:
            log.exception("%s FAILED after %.2fs", name,
                          time.perf_counter() - started)
            return None
        finally:
            if conn is not None:
                conn.close()

    # -- scheduled tasks --------------------------------------------------
    def matchday_poll(self) -> None:
        """Poll live scoring, but only while a gameweek is actually running."""
        from . import temporal
        from .db import connect

        conn = None
        try:
            conn = connect(self.db_path)
            state = temporal.gw_state(conn)
            phase = getattr(state.phase, "value", str(state.phase))
        except Exception:
            log.exception("could not read gameweek phase; skipping live poll")
            return
        finally:
            if conn is not None:
                conn.close()

        if phase not in ("LIVE", "SETTLING"):
            log.debug("phase %s - no live poll", phase)
            return

        log.info("matchday poll (phase %s, gw %s)", phase, state.scoring_gw)
        self.run_job("poll_live")

    def freeze_now(self, gw: int | None = None) -> None:
        """Capture the pre-deadline projection freeze."""
        log.info("pre-deadline freeze firing%s",
                 f" for GW{gw}" if gw else "")
        # Projections are recomputed first so the frozen vector reflects the
        # freshest team news rather than whatever was last cached.
        self.run_job("recompute_xp")
        self.run_job("freeze_projections")

    def supervise_deadline(self) -> None:
        """Arm a one-shot timer at the next deadline minus the lead time.

        Re-armed on every supervisor tick rather than scheduled once at start,
        because FPL moves deadlines when fixtures are rearranged and a timer
        armed against a stale deadline would fire at the wrong moment.
        """
        from .db import connect
        from .models import snapshot as snapshot_mod

        conn = None
        try:
            conn = connect(self.db_path)
            row = conn.execute("SELECT MAX(gw) FROM player_gw").fetchone()
            played = int(row[0] or 0) if row else 0

            now = dt.datetime.now(dt.timezone.utc)
            armed = False
            for gw in range(played + 1, played + 4):
                if snapshot_mod.has_snapshot(conn, gw):
                    continue
                line = snapshot_mod.deadline_for(conn, gw)
                if line.when is None:
                    continue
                fire_at = line.when - dt.timedelta(minutes=FREEZE_LEAD_MINUTES)

                if fire_at <= now < line.when:
                    # Already inside the window -- a daemon started late, or a
                    # deadline that moved closer. Capture immediately rather
                    # than waiting for a timer that would never fire.
                    log.info("GW%s already inside the freeze window; "
                             "capturing now", gw)
                    self.freeze_now(gw)
                elif fire_at > now:
                    self._arm(gw, fire_at, line)
                    armed = True
                break

            if not armed:
                log.debug("no upcoming deadline to arm")
        except Exception:
            log.exception("deadline supervisor failed")
        finally:
            if conn is not None:
                conn.close()

    def _arm(self, gw: int, fire_at: dt.datetime, line) -> None:
        from apscheduler.triggers.date import DateTrigger

        job_id = f"freeze_gw{gw}"
        self.scheduler.add_job(
            self.freeze_now, DateTrigger(run_date=fire_at), args=[gw],
            id=job_id, name=f"Pre-deadline freeze GW{gw}",
            replace_existing=True, misfire_grace_time=1800)
        hours = (fire_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        log.info("armed freeze for GW%s at %s (%.1fh away, deadline %s, "
                 "source %s)", gw, fire_at.isoformat(timespec="minutes"),
                 hours, line.when.isoformat(timespec="minutes"), line.source)

    def price_monitor(self) -> None:
        """Nightly transfer-flow snapshot, just before FPL's price batch."""
        log.info("nightly price monitor")
        self.run_job("snapshot_prices")

    def reference_refresh(self) -> None:
        """Bootstrap, fixtures and gameweek deadlines."""
        self.run_job("refresh_reference")

    # -- lifecycle --------------------------------------------------------
    def build(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        # coalesce + max_instances=1: after a laptop wakes from sleep a
        # scheduler with a backlog would otherwise fire every missed minute of
        # live polling at once and trip the rate limiter.
        scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 300})

        scheduler.add_job(
            self.matchday_poll, IntervalTrigger(seconds=LIVE_POLL_SECONDS),
            id="matchday_poll", name="Live scoring poll (60s when active)")

        scheduler.add_job(
            self.supervise_deadline,
            IntervalTrigger(minutes=SUPERVISOR_MINUTES),
            id="deadline_supervisor", name="Pre-deadline freeze supervisor",
            next_run_time=dt.datetime.now(dt.timezone.utc))

        scheduler.add_job(
            self.price_monitor,
            CronTrigger(hour=PRICE_HOUR_UTC, minute=PRICE_MINUTE_UTC),
            id="price_monitor", name="Nightly price and transfer-flow monitor")

        scheduler.add_job(
            self.reference_refresh, IntervalTrigger(minutes=REFERENCE_MINUTES),
            id="reference_refresh", name="Reference data refresh",
            next_run_time=dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=20))

        self.scheduler = scheduler
        return scheduler

    def stop(self, *_args) -> None:
        """Graceful shutdown: let the running job finish its write first.

        `wait=True` is the whole point. SQLite in WAL mode survives a hard
        kill, but a freeze interrupted mid-write leaves a partial gameweek that
        the write-once guard will then refuse to complete, and the snapshot for
        that deadline is gone for good.
        """
        if self._stopping:
            return
        self._stopping = True
        log.info("shutdown requested - waiting for in-flight jobs")
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=True)
            except Exception:
                log.exception("scheduler shutdown raised")
        clear_pid(pid_path(self.db_path))
        log.info("daemon stopped cleanly")

    def serve(self) -> int:
        """Run until signalled. Returns a process exit code."""
        pidfile = pid_path(self.db_path)
        existing = read_pid(pidfile)
        if existing and existing != os.getpid() and is_running(existing):
            log.error("daemon already running (pid %s) - refusing to start a "
                      "second one against the same database", existing)
            return 1

        stopfile = stop_path(self.db_path)
        clear_pid(stopfile)          # a stale sentinel would stop us instantly
        write_pid(pidfile)
        self.build()

        signal.signal(signal.SIGINT, self.stop)
        with contextlib.suppress(AttributeError, ValueError, OSError):
            signal.signal(signal.SIGTERM, self.stop)

        self.scheduler.start()
        log.info("daemon started (pid %s, db %s)", os.getpid(), self.db_path)
        for job in self.scheduler.get_jobs():
            log.info("  job %-22s next %s", job.id,
                     getattr(job, "next_run_time", None))

        try:
            while not self._stopping:
                if stopfile.exists():
                    log.info("stop sentinel seen")
                    clear_pid(stopfile)
                    break
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.stop()
        return 0


def _summarise(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    keys = ("gw", "players", "rows", "snapshots", "changes", "projections",
            "gameweeks", "quality", "frozen", "skipped", "note")
    parts = []
    for key in keys:
        if key in result:
            value = result[key]
            if isinstance(value, list):
                value = f"{len(value)} item(s)"
            parts.append(f"{key}={value}")
    return " ".join(parts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fpl_assistant.daemon",
        description="Background scheduler for the FPL Squad Assistant.")
    parser.add_argument("--once", action="store_true",
                        help="run every job once and exit (smoke test)")
    parser.add_argument("--status", action="store_true",
                        help="report whether a daemon is running")
    parser.add_argument("--stop", action="store_true",
                        help="signal a running daemon to stop")
    parser.add_argument("--timeout", type=int, default=45,
                        help="seconds to wait for a graceful stop")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    from .config import load_config
    cfg = load_config()
    db_path = Path(cfg.db_path)
    configure_logging(db_path.parent / "daemon.log", verbose=args.verbose)

    pidfile = pid_path(db_path)

    if args.status:
        pid = read_pid(pidfile)
        if pid and is_running(pid):
            print(f"running (pid {pid})")
            return 0
        print("not running")
        return 1

    if args.stop:
        pid = read_pid(pidfile)
        if not pid or not is_running(pid):
            print("daemon not running")
            clear_pid(pidfile)
            return 0

        stopfile = stop_path(db_path)
        stopfile.write_text("stop", encoding="utf-8")
        print(f"stop requested (pid {pid}) - waiting for in-flight jobs...")

        # Give the daemon time to finish whatever it is mid-way through. The
        # freeze is the long pole and takes a few seconds on a full player set.
        for _ in range(args.timeout):
            time.sleep(1)
            if not is_running(pid):
                print("daemon stopped cleanly")
                return 0

        print(f"daemon did not exit within {args.timeout}s; leaving it alone "
              f"rather than killing it mid-write. Check "
              f"{db_path.parent / 'daemon.log'}")
        clear_pid(stopfile)
        return 1

    daemon = Daemon(db_path)
    if args.once:
        print("running each job once...")
        daemon.build()
        daemon.reference_refresh()
        daemon.matchday_poll()
        daemon.supervise_deadline()
        daemon.price_monitor()
        print(f"done - see {db_path.parent / 'daemon.log'}")
        return 0

    return daemon.serve()


if __name__ == "__main__":
    raise SystemExit(main())
