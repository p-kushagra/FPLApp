"""Async workers, ILEO, and the Understat graceful-degradation fallback."""
from __future__ import annotations

import pytest

from fpl_assistant import cache, temporal
from fpl_assistant.jobs import base as job_base
from fpl_assistant.jobs import tasks
from fpl_assistant.jobs.base import JobState
from fpl_assistant.jobs.runner_local import LocalThreadRunner, shutdown_executor
from fpl_assistant.strategy import eo as eo_mod
from fpl_assistant.strategy.eo import Exposure

from .conftest import FakeSession


@pytest.fixture
def runner(db_path):
    from fpl_assistant import db as db_module
    db_module.init_db(db_path)
    yield LocalThreadRunner(db_path, registry=dict(tasks.REGISTRY))
    shutdown_executor(wait=True)


@pytest.fixture
def sync_runner(db_path):
    from fpl_assistant import db as db_module
    db_module.init_db(db_path)
    return LocalThreadRunner(db_path, registry={}, synchronous=True)


# ==========================================================================
class TestJobRunner:
    def test_job_lifecycle(self, sync_runner):
        sync_runner.registry["ok"] = lambda conn, progress, **kw: {"done": True}
        job_id = sync_runner.submit("ok")
        status = sync_runner.status(job_id)
        assert status.state is JobState.DONE
        assert status.result == {"done": True}
        assert status.attempts == 1

    def test_row_is_written_before_dispatch(self, sync_runner):
        """A crash between insert and dispatch must leave a visible row."""
        seen = {}

        def peek(conn, progress, **kw):
            seen["state"] = conn.execute(
                "SELECT state FROM job LIMIT 1").fetchone()["state"]

        sync_runner.registry["peek"] = peek
        sync_runner.submit("peek")
        assert seen["state"] == JobState.RUNNING.value

    def test_failure_is_recorded_not_raised(self, sync_runner):
        def boom(conn, progress, **kw):
            raise RuntimeError("upstream exploded")

        sync_runner.registry["boom"] = boom
        job_id = sync_runner.submit("boom")     # must not raise
        status = sync_runner.status(job_id)
        assert status.state is JobState.FAILED
        assert "exploded" in status.error

    def test_progress_is_reported(self, sync_runner):
        def work(conn, progress, **kw):
            progress(0.5, "halfway")

        sync_runner.registry["work"] = work
        job_id = sync_runner.submit("work")
        # Terminal state overwrites progress with 1.0; the note survives.
        assert sync_runner.status(job_id).progress_note == "halfway"

    def test_unknown_job_is_rejected_at_submit(self, sync_runner):
        with pytest.raises(KeyError, match="unknown job"):
            sync_runner.submit("does_not_exist")

    def test_threaded_execution_completes(self, runner):
        runner.registry["ping"] = lambda conn, progress, **kw: "pong"
        job_id = runner.submit("ping")
        assert runner.wait(job_id, timeout=15).result == "pong"

    def test_kwargs_reach_the_task(self, sync_runner):
        sync_runner.registry["echo"] = lambda conn, progress, **kw: kw
        job_id = sync_runner.submit("echo", league_id=42, gw=7)
        assert sync_runner.status(job_id).result == {"league_id": 42, "gw": 7}

    # -- ADR-002 consequence: durability lives in the table, not a broker ----
    def test_orphaned_job_is_reaped_as_stale(self, db, db_path):
        db.execute(
            """INSERT INTO job (job_id, name, state, heartbeat_at, enqueued_at)
               VALUES ('orphan', 'poll_live', 'running',
                       '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"""
        )
        db.commit()
        assert job_base.reap_stale(db, heartbeat_timeout_s=60) == 1
        assert db.execute(
            "SELECT state FROM job WHERE job_id='orphan'").fetchone()["state"] == "stale"

    def test_live_job_is_not_reaped(self, db):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        db.execute(
            """INSERT INTO job (job_id, name, state, heartbeat_at, enqueued_at)
               VALUES ('live', 'poll_live', 'running', ?, ?)""", (now, now))
        db.commit()
        assert job_base.reap_stale(db, heartbeat_timeout_s=300) == 0

    def test_stale_job_within_budget_is_requeued(self, sync_runner, db_path):
        from fpl_assistant import db as db_module
        sync_runner.registry["retry_me"] = lambda conn, progress, **kw: "ok"
        conn = db_module.connect(db_path)
        conn.execute(
            """INSERT INTO job (job_id, name, args, state, attempts, max_attempts,
                                heartbeat_at, enqueued_at)
               VALUES ('old', 'retry_me', '{}', 'running', 1, 3,
                       '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()
        assert len(sync_runner.requeue_stale(heartbeat_timeout_s=60)) == 1

    def test_executor_is_a_singleton(self):
        """The thread-leak guard: Streamlit reruns must not spawn a pool each time."""
        from fpl_assistant.jobs.runner_local import get_executor
        pools = {id(get_executor()) for _ in range(50)}
        assert len(pools) == 1
        shutdown_executor(wait=True)

    def test_pending_lists_queued_and_running(self, db):
        db.execute(
            """INSERT INTO job (job_id, name, state, priority, enqueued_at)
               VALUES ('a', 'x', 'queued', 5, '2026-01-01T00:00:00+00:00')""")
        db.execute(
            """INSERT INTO job (job_id, name, state, priority, enqueued_at)
               VALUES ('b', 'y', 'done', 5, '2026-01-01T00:00:00+00:00')""")
        db.commit()
        assert [j.job_id for j in job_base.pending(db)] == ["a"]


# ==========================================================================
class TestUnderstatDegradation:
    """The R1 scenario: Understat must never be load-bearing."""

    def test_league_failure_flags_offline(self, db, monkeypatch):
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.league_players",
            lambda self, season: __import__(
                "fpl_assistant.sources.base", fromlist=["SourceResult"]
            ).SourceResult.unavailable("understat", "503"),
        )
        result = tasks.ingest_understat_league(db, season=2025)
        assert result["ok"] is False
        assert result["degraded"] is True
        assert tasks.understat_offline(db) is True

    def test_offline_flag_drives_the_ui_badge(self, db):
        tasks._flag_understat_offline(db, "rate limited")
        state = tasks.degradation_state(db)
        assert state["understat_offline"] is True
        assert "Understat Offline" in state["understat_badge"]
        assert "Baseline" in state["understat_badge"]

    def test_recovery_clears_the_badge(self, db):
        tasks._flag_understat_offline(db, "boom")
        tasks._flag_understat_online(db)
        state = tasks.degradation_state(db)
        assert state["understat_offline"] is False
        assert state["understat_badge"] is None

    def test_disabled_understat_makes_no_request(self, db):
        session = FakeSession()
        from fpl_assistant.sources.understat import UnderstatSource
        src = UnderstatSource(db, session, enabled=False)
        assert src.league_players(2025).quality.is_degraded
        assert session.calls == []

    def test_xp_falls_back_to_fpl_baseline(self, db):
        """The fallback that matters: projections still compute without xG data."""
        _seed_minimal(db)
        result = tasks.recompute_xp(db, gws=[2], understat_ok=False)
        assert result["ok"] is True
        assert result["understat_ok"] is False
        assert result["sources"].get("fpl_baseline", 0) > 0

    def test_recompute_xp_reads_the_offline_flag(self, db):
        _seed_minimal(db)
        tasks._flag_understat_offline(db, "down")
        assert tasks.recompute_xp(db, gws=[2])["understat_ok"] is False

    def test_fanout_isolates_one_players_failure(self, db, monkeypatch):
        from fpl_assistant.sources.base import SourceResult

        def flaky(self, understat_id):
            if understat_id == "bad":
                return SourceResult.unavailable("understat", "404")
            return SourceResult.ok([], "understat")

        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_matches", flaky)
        result = tasks.understat_fanout(db, understat_ids=["good1", "bad", "good2"])
        assert result["ok"] is True
        assert result["failed"] == ["bad"]
        assert result["players"] == 2, "one failure must not fail the batch"


# ==========================================================================
class TestILEO:
    def test_ileo_is_the_mean_multiplier(self, db):
        _seed_rivals(db, gw=10, picks={
            101: {1: 1, 2: 2},      # rival 101 owns p1 (x1), captains p2
            102: {1: 1, 3: 1},
            103: {2: 1, 3: 1},
        })
        values = eo_mod.ileo(db, 10, [101, 102, 103])
        assert values[1] == pytest.approx(2 / 3, abs=1e-3)
        assert values[2] == pytest.approx(3 / 3, abs=1e-3)

    def test_denominator_uses_rivals_actually_retrieved(self, db):
        """A partial freeze must not silently deflate everyone's ILEO."""
        _seed_rivals(db, gw=10, picks={101: {1: 1}, 102: {1: 1}})
        values = eo_mod.ileo(db, 10, [101, 102, 103, 104])
        assert values[1] == pytest.approx(1.0), "denominator must be 2, not 4"

    def test_swing_signs_classify_exposure(self, db):
        _seed_rivals(db, gw=10, picks={
            101: {1: 1, 2: 1, 3: 1},
            102: {1: 1, 2: 1, 3: 1},
        })
        _seed_mine(db, gw=10, picks={1: 2, 3: 1, 4: 1})
        matrix = eo_mod.swing_matrix(db, 10, [101, 102])
        by_id = {r.player_id: r for r in matrix.rows}

        assert by_id[1].exposure is Exposure.OVER        # captained, they start
        assert by_id[2].exposure is Exposure.UNDER       # they own, I do not
        assert by_id[3].exposure is Exposure.NEUTRALISED  # shared holding
        assert by_id[4].exposure is Exposure.OVER        # only I own

    def test_neutralised_players_cannot_move_rank(self, db):
        _seed_rivals(db, gw=10, picks={101: {5: 1}, 102: {5: 1}})
        _seed_mine(db, gw=10, picks={5: 1})
        row = next(r for r in eo_mod.swing_matrix(db, 10, [101, 102]).rows
                   if r.player_id == 5)
        assert row.swing == 0.0
        assert row.realised_swing == 0.0

    def test_realised_swing_uses_actual_points(self, db):
        _seed_rivals(db, gw=10, picks={101: {7: 0}, 102: {7: 0}})
        _seed_mine(db, gw=10, picks={7: 2})
        db.execute(
            "INSERT INTO player_gw(player_id, gw, minutes, total_points) "
            "VALUES (7, 10, 90, 9)")
        db.commit()
        row = next(r for r in eo_mod.swing_matrix(db, 10, [101, 102]).rows
                   if r.player_id == 7)
        assert row.swing == 2.0
        assert row.realised_swing == pytest.approx(18.0)

    def test_partial_matrix_is_flagged_with_a_note(self, db):
        _seed_rivals(db, gw=10, picks={101: {1: 1}})
        matrix = eo_mod.swing_matrix(db, 10, [101, 102, 103])
        assert matrix.partial is True
        assert "1 of 3" in matrix.coverage_note

    def test_complete_matrix_carries_no_note(self, db):
        _seed_rivals(db, gw=10, picks={101: {1: 1}, 102: {1: 1}})
        matrix = eo_mod.swing_matrix(db, 10, [101, 102])
        assert matrix.partial is False
        assert matrix.coverage_note is None

    def test_no_rivals_gives_an_empty_matrix_not_a_crash(self, db):
        matrix = eo_mod.swing_matrix(db, 10, [])
        assert matrix.rows == []
        assert matrix.rival_ids == []

    def test_buckets_partition_the_squad(self, db):
        _seed_rivals(db, gw=10, picks={101: {1: 1, 2: 1, 3: 1},
                                       102: {1: 1, 2: 1, 3: 1}})
        _seed_mine(db, gw=10, picks={1: 2, 3: 1, 4: 1})
        m = eo_mod.swing_matrix(db, 10, [101, 102])
        buckets = (len(m.needs_haul()) + len(m.needs_blank())
                   + len(m.neutralised()))
        assert buckets == len(m.rows)

    def test_captain_ileo_is_the_captaincy_share(self, db):
        _seed_rivals(db, gw=10, picks={101: {9: 2}, 102: {9: 1}, 103: {9: 2}},
                     captains={101: 9, 103: 9})
        assert eo_mod.captain_ileo(db, 10, [101, 102, 103])[9] == pytest.approx(2 / 3,
                                                                                abs=1e-3)

    def test_persist_writes_the_cache(self, db):
        _seed_rivals(db, gw=10, picks={101: {1: 1}, 102: {1: 2}})
        _seed_mine(db, gw=10, picks={1: 1})
        matrix = eo_mod.swing_matrix(db, 10, [101, 102], league_id=555)
        assert eo_mod.persist_ileo(db, matrix) == len(matrix.rows)
        row = db.execute(
            "SELECT * FROM ileo_cache WHERE league_id=555 AND player_id=1").fetchone()
        assert row["ileo"] == pytest.approx(1.5)
        assert row["swing_per_point"] == pytest.approx(-0.5)


# ==========================================================================
class TestFreezeSemantics:
    def test_freeze_refuses_before_the_deadline(self, db):
        temporal.sync_gw_state(db, [
            {"id": 10, "deadline_time": "2099-01-01T11:00:00Z", "is_next": True},
        ])
        result = tasks.freeze_rivals(db, league_id=1, gw=10, rival_ids=[101])
        assert result["ok"] is False
        assert "deadline" in result["reason"]

    def test_freeze_is_idempotent(self, db, monkeypatch):
        temporal.sync_gw_state(db, [
            {"id": 10, "deadline_time": "2020-01-01T11:00:00Z",
             "is_current": True, "finished": False},
        ])
        calls = {"n": 0}
        from fpl_assistant.sources.base import SourceResult

        def picks(self, team_id, gw, frozen=False):
            calls["n"] += 1
            return SourceResult.ok(
                {"picks": [{"element": 1, "position": 1, "multiplier": 1,
                            "is_captain": False, "is_vice_captain": False}]},
                "fpl")

        monkeypatch.setattr("fpl_assistant.sources.fpl.FplSource.picks", picks)

        first = tasks.freeze_rivals(db, league_id=1, gw=10, rival_ids=[101])
        second = tasks.freeze_rivals(db, league_id=1, gw=10, rival_ids=[101])

        assert first["frozen"] == 1
        assert second["frozen"] == 0 and second["skipped"] == 1
        assert calls["n"] == 1, "a second trigger must not refetch"

    def test_partial_freeze_is_kept_and_reported(self, db, monkeypatch):
        temporal.sync_gw_state(db, [
            {"id": 10, "deadline_time": "2020-01-01T11:00:00Z",
             "is_current": True, "finished": False},
        ])
        from fpl_assistant.sources.base import SourceResult

        def picks(self, team_id, gw, frozen=False):
            if team_id == 102:
                return SourceResult.unavailable("fpl", "500")
            return SourceResult.ok(
                {"picks": [{"element": 1, "position": 1, "multiplier": 1,
                            "is_captain": False, "is_vice_captain": False}]}, "fpl")

        monkeypatch.setattr("fpl_assistant.sources.fpl.FplSource.picks", picks)
        result = tasks.freeze_rivals(db, league_id=1, gw=10,
                                     rival_ids=[101, 102, 103])
        assert result["frozen"] == 2
        assert result["failed"] == 1
        assert result["partial"] is True, "must report the incomplete denominator"


# ==========================================================================
class TestCacheRevalidatorWiring:
    def test_stale_read_enqueues_a_real_job(self, sync_runner, db, clock):
        """Closes the Phase 1 loop: the SWR hook now reaches the job queue."""
        from fpl_assistant.jobs import install_cache_revalidator

        submitted: list[str] = []
        sync_runner.registry["refresh_reference"] = (
            lambda conn, progress, **kw: submitted.append("ran"))
        install_cache_revalidator(sync_runner)
        try:
            cache.get_or_revalidate(db, "fpl:bootstrap", "fpl_static",
                                    lambda: {"v": 1}, now=clock())
            clock.advance(cache.TIERS["fpl_static"].soft_ttl + 1)
            result = cache.get_or_revalidate(db, "fpl:bootstrap", "fpl_static",
                                             lambda: {"v": 2}, now=clock())
            assert result.quality.value == "stale"
            assert submitted == ["ran"]
        finally:
            cache.clear_revalidator()


# ==========================================================================
# helpers
# ==========================================================================
def _seed_rivals(conn, gw, picks, captains=None):
    captains = captains or {}
    for entry_id, elements in picks.items():
        for pid, mult in elements.items():
            conn.execute(
                """INSERT OR REPLACE INTO league_rival_pick
                     (entry_id, gw, player_id, position, multiplier,
                      is_captain, is_vice, frozen)
                   VALUES (?, ?, ?, 1, ?, ?, 0, 1)""",
                (entry_id, gw, pid, mult,
                 1 if captains.get(entry_id) == pid else 0),
            )
        conn.execute(
            """INSERT OR REPLACE INTO players (id, web_name) VALUES (?, ?)""",
            (next(iter(elements)), f"P{next(iter(elements))}"),
        )
    for pid in {p for e in picks.values() for p in e}:
        conn.execute("INSERT OR REPLACE INTO players (id, web_name) VALUES (?, ?)",
                     (pid, f"P{pid}"))
    conn.commit()


def _seed_mine(conn, gw, picks):
    for pid, mult in picks.items():
        conn.execute(
            """INSERT OR REPLACE INTO my_picks
                 (gw, player_id, position, multiplier, is_captain, is_vice)
               VALUES (?, ?, 1, ?, 0, 0)""",
            (gw, pid, mult),
        )
        conn.execute("INSERT OR REPLACE INTO players (id, web_name) VALUES (?, ?)",
                     (pid, f"P{pid}"))
    conn.commit()


def _seed_minimal(conn):
    """Two clubs, a handful of players, one fixture in GW2."""
    for tid, name in ((1, "AAA"), (2, "BBB")):
        conn.execute(
            """INSERT OR REPLACE INTO teams
                 (id, name, short_name, strength_attack_home, strength_attack_away,
                  strength_defence_home, strength_defence_away)
               VALUES (?, ?, ?, 1100, 1050, 1100, 1050)""",
            (tid, name, name))
    for pid, etype, team in ((1, 1, 1), (2, 2, 1), (3, 3, 1), (4, 4, 2)):
        conn.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, element_type, position, team_id, now_cost, status)
               VALUES (?, ?, ?, ?, ?, 5.0, 'a')""",
            (pid, f"P{pid}", etype,
             {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}[etype], team))
        conn.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points,
                  expected_goals, expected_assists, defensive_contribution, bonus)
               VALUES (?, 1, 90, 1, 5, 0.3, 0.2, 8, 1)""",
            (pid,))
    conn.execute(
        """INSERT OR REPLACE INTO fixtures
             (id, event, team_h, team_a, team_h_difficulty, team_a_difficulty,
              kickoff_time, finished)
           VALUES (100, 2, 1, 2, 3, 3, '2026-01-10T15:00:00Z', 0)""")
    conn.commit()
