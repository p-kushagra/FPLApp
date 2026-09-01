"""Background daemon: scheduling, gating and the graceful-stop handshake.

The freeze timing test is the one that matters. `projection_snapshot` is
write-once and a late capture is permanently marked contaminated, so arming the
timer at the wrong moment is not a bug you can fix next week -- that gameweek's
clean snapshot is simply gone.
"""
from __future__ import annotations

import datetime as dt

import pytest

from fpl_assistant import daemon as daemon_mod


@pytest.fixture
def prepared(db_path):
    """A migrated database with a known GW3 deadline and GW1-2 played."""
    from fpl_assistant import db as db_module

    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    deadline = dt.datetime(2026, 9, 4, 17, 30, tzinfo=dt.timezone.utc)
    conn.execute(
        "INSERT OR REPLACE INTO gw_state (gw, deadline_time, is_next)"
        " VALUES (3, ?, 1)", (deadline.isoformat(),))
    conn.execute("INSERT OR REPLACE INTO players (id, web_name) VALUES (1,'X')")
    for gw in (1, 2):
        conn.execute(
            "INSERT OR REPLACE INTO player_gw (player_id, gw, minutes)"
            " VALUES (1, ?, 90)", (gw,))
    conn.commit()
    conn.close()
    return db_path, deadline


class TestPidAndStopFiles:
    def test_paths_sit_beside_the_database(self, db_path):
        assert daemon_mod.pid_path(db_path).parent == db_path.parent
        assert daemon_mod.stop_path(db_path).name == "daemon.stop"

    def test_pid_round_trip(self, db_path, tmp_path):
        path = tmp_path / "daemon.pid"
        daemon_mod.write_pid(path)
        assert daemon_mod.read_pid(path) > 0
        daemon_mod.clear_pid(path)
        assert daemon_mod.read_pid(path) is None

    def test_missing_pid_file_is_not_an_error(self, tmp_path):
        assert daemon_mod.read_pid(tmp_path / "absent.pid") is None

    def test_corrupt_pid_file_is_not_an_error(self, tmp_path):
        path = tmp_path / "daemon.pid"
        path.write_text("not a number", encoding="utf-8")
        assert daemon_mod.read_pid(path) is None

    def test_clearing_an_absent_file_is_a_noop(self, tmp_path):
        daemon_mod.clear_pid(tmp_path / "absent.pid")

    def test_our_own_pid_is_running(self):
        import os
        assert daemon_mod.is_running(os.getpid())

    def test_absurd_pid_is_not_running(self):
        assert not daemon_mod.is_running(0)
        assert not daemon_mod.is_running(-1)


class TestLogging:
    def test_rotating_handler_is_installed(self, tmp_path):
        import logging
        import logging.handlers

        daemon_mod.configure_logging(tmp_path / "logs" / "daemon.log")
        handlers = logging.getLogger("fpl").handlers
        rotating = [h for h in handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert rotating, "file logging must rotate; an unbounded log fills the disk"
        assert rotating[0].backupCount == daemon_mod.LOG_BACKUPS
        assert rotating[0].maxBytes == daemon_mod.LOG_MAX_BYTES

    def test_log_directory_is_created(self, tmp_path):
        target = tmp_path / "made" / "up" / "daemon.log"
        daemon_mod.configure_logging(target)
        assert target.parent.is_dir()


class TestFreezeTiming:
    def test_timer_is_armed_exactly_one_hour_before_the_deadline(self, prepared):
        db_path, deadline = prepared
        daemon = daemon_mod.Daemon(db_path)
        daemon.build()
        daemon.supervise_deadline()

        job = daemon.scheduler.get_job("freeze_gw3")
        assert job is not None, "the next deadline must arm a freeze timer"
        fire_at = job.trigger.run_date
        assert fire_at == deadline - dt.timedelta(
            minutes=daemon_mod.FREEZE_LEAD_MINUTES)

    def test_lead_time_is_sixty_minutes(self):
        assert daemon_mod.FREEZE_LEAD_MINUTES == 60

    def test_already_frozen_gameweek_is_not_rearmed(self, prepared):
        from fpl_assistant import db as db_module

        db_path, _ = prepared
        conn = db_module.connect(db_path)
        conn.execute(
            """INSERT OR REPLACE INTO projection_snapshot_meta (gw, rows)
               VALUES (3, 600)""")
        conn.commit()
        conn.close()

        daemon = daemon_mod.Daemon(db_path)
        daemon.build()
        daemon.supervise_deadline()
        assert daemon.scheduler.get_job("freeze_gw3") is None

    def test_no_deadline_arms_nothing(self, db_path):
        from fpl_assistant import db as db_module

        db_module.init_db(db_path)
        daemon = daemon_mod.Daemon(db_path)
        daemon.build()
        daemon.supervise_deadline()      # must not raise on an empty database


class TestSchedule:
    def test_every_job_is_registered(self, prepared):
        db_path, _ = prepared
        daemon = daemon_mod.Daemon(db_path)
        scheduler = daemon.build()
        ids = {j.id for j in scheduler.get_jobs()}
        assert {"matchday_poll", "deadline_supervisor", "price_monitor",
                "reference_refresh", "league_refresh", "rival_freeze"} <= ids

    def test_rival_freeze_runs_often_enough_to_catch_a_deadline(self, prepared):
        """Rival picks are readable only after the lock, so this cannot be
        armed at an exact moment like the projection freeze -- it polls."""
        db_path, _ = prepared
        job = daemon_mod.Daemon(db_path).build().get_job("rival_freeze")
        assert job.trigger.interval.total_seconds() <= 30 * 60

    def test_league_discovery_is_scheduled(self, prepared):
        db_path, _ = prepared
        job = daemon_mod.Daemon(db_path).build().get_job("league_refresh")
        assert job.trigger.interval.total_seconds() == (
            daemon_mod.LEAGUE_MINUTES * 60)

    def test_live_poll_is_on_a_sixty_second_beat(self, prepared):
        db_path, _ = prepared
        daemon = daemon_mod.Daemon(db_path)
        scheduler = daemon.build()
        job = scheduler.get_job("matchday_poll")
        assert job.trigger.interval.total_seconds() == 60

    def test_price_monitor_runs_at_0115_utc(self, prepared):
        db_path, _ = prepared
        daemon = daemon_mod.Daemon(db_path)
        scheduler = daemon.build()
        fields = {f.name: str(f) for f in scheduler.get_job(
            "price_monitor").trigger.fields}
        assert fields["hour"] == "1"
        assert fields["minute"] == "15"

    def test_jobs_coalesce_after_a_sleep(self, prepared):
        """A laptop waking from sleep must not fire every missed poll at once."""
        db_path, _ = prepared
        daemon = daemon_mod.Daemon(db_path)
        scheduler = daemon.build()
        assert scheduler._job_defaults["coalesce"] is True
        assert scheduler._job_defaults["max_instances"] == 1


class TestMatchdayGating:
    def _phase(self, monkeypatch, phase):
        from fpl_assistant import temporal

        class FakeState:
            def __init__(self):
                self.phase = phase
                self.scoring_gw = 2

        monkeypatch.setattr(temporal, "gw_state", lambda conn, **kw: FakeState())

    def test_no_poll_outside_a_live_gameweek(self, prepared, monkeypatch):
        db_path, _ = prepared
        self._phase(monkeypatch, "UPCOMING")

        daemon = daemon_mod.Daemon(db_path)
        called = []
        monkeypatch.setattr(daemon, "run_job",
                            lambda name, **kw: called.append(name))
        daemon.matchday_poll()
        assert called == [], "polling the live endpoint mid-week wastes budget"

    def test_polls_while_live(self, prepared, monkeypatch):
        db_path, _ = prepared
        self._phase(monkeypatch, "LIVE")

        daemon = daemon_mod.Daemon(db_path)
        called = []
        monkeypatch.setattr(daemon, "run_job",
                            lambda name, **kw: called.append(name))
        daemon.matchday_poll()
        assert called == ["poll_live"]

    def test_polls_while_settling(self, prepared, monkeypatch):
        """Bonus and auto-subs are still moving after the final whistle."""
        db_path, _ = prepared
        self._phase(monkeypatch, "SETTLING")

        daemon = daemon_mod.Daemon(db_path)
        called = []
        monkeypatch.setattr(daemon, "run_job",
                            lambda name, **kw: called.append(name))
        daemon.matchday_poll()
        assert called == ["poll_live"]


class TestJobIsolation:
    def test_a_failing_job_never_escapes(self, prepared, monkeypatch):
        """A background sync must not be able to stop the freeze an hour later."""
        from fpl_assistant.jobs import tasks

        db_path, _ = prepared

        def explode(conn, **kwargs):
            raise RuntimeError("upstream on fire")

        monkeypatch.setitem(tasks.REGISTRY, "boom", explode)
        daemon = daemon_mod.Daemon(db_path)
        assert daemon.run_job("boom") is None

    def test_unknown_job_is_reported_not_raised(self, prepared):
        db_path, _ = prepared
        assert daemon_mod.Daemon(db_path).run_job("no_such_job") is None

    def test_a_real_job_runs(self, prepared):
        db_path, _ = prepared
        result = daemon_mod.Daemon(db_path).run_job("freeze_projections")
        assert isinstance(result, dict)


class TestCli:
    def test_status_reports_not_running(self, prepared, monkeypatch, capsys):
        db_path, _ = prepared
        self._pin_db(monkeypatch, db_path)
        assert daemon_mod.main(["--status"]) == 1
        assert "not running" in capsys.readouterr().out

    def test_stop_on_a_dead_daemon_is_clean(self, prepared, monkeypatch, capsys):
        db_path, _ = prepared
        self._pin_db(monkeypatch, db_path)
        daemon_mod.pid_path(db_path).write_text("999999", encoding="utf-8")
        assert daemon_mod.main(["--stop"]) == 0
        assert not daemon_mod.pid_path(db_path).exists()

    def test_once_mode_runs_and_exits(self, prepared, monkeypatch):
        db_path, _ = prepared
        self._pin_db(monkeypatch, db_path)
        assert daemon_mod.main(["--once"]) == 0
        assert (db_path.parent / "daemon.log").exists()

    def _pin_db(self, monkeypatch, db_path):
        from fpl_assistant import config as config_mod

        real = config_mod.load_config()

        def fake_load():
            import dataclasses
            return dataclasses.replace(real, db_path=db_path)

        monkeypatch.setattr(config_mod, "load_config", fake_load)
        monkeypatch.setattr("fpl_assistant.daemon.load_config", fake_load,
                            raising=False)
