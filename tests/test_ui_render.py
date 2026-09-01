"""Page render checks and service-layer contracts.

Uses Streamlit's AppTest to mount each page headlessly against a real SQLite
file. This is the dry-run gate: it catches import errors, bad widget arguments
and unguarded attribute access -- the failures that only ever show up when the
page is actually opened.

The empty-database cases matter most. A decision page must degrade to a labelled
empty state on a fresh install, not raise.
"""
from __future__ import annotations

import pathlib

import pytest

from fpl_assistant.services import command_center, degrade, gw_summary

st_testing = pytest.importorskip("streamlit.testing.v1", reason="needs Streamlit")
AppTest = st_testing.AppTest

# AppTest resolves relative paths against the CALLING file, so anchor to the
# repo root explicitly rather than to tests/.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE1 = str(PROJECT_ROOT / "pages" / "0_Gameweek_Summary.py")
PAGE2 = str(PROJECT_ROOT / "pages" / "1_Command_Center.py")


@pytest.fixture
def seeded_db(db, db_path, monkeypatch):
    """A realistic-enough database: two clubs, a squad, history, fixtures."""
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("FPL_TEAM_ID", "12345")

    # Six clubs: 15 players at max 3 per club needs at least five.
    for tid, short in ((1, "AAA"), (2, "BBB"), (3, "CCC"),
                       (4, "DDD"), (5, "EEE"), (6, "FFF")):
        db.execute(
            """INSERT INTO teams (id, name, short_name, strength,
                 strength_attack_home, strength_attack_away,
                 strength_defence_home, strength_defence_away)
               VALUES (?, ?, ?, 3, 1100, 1050, 1100, 1050)""",
            (tid, f"Team {short}", short))

    positions = [(1, "GKP")] * 2 + [(2, "DEF")] * 5 + [(3, "MID")] * 5 + \
                [(4, "FWD")] * 3
    for pid, (etype, pos) in enumerate(positions, start=1):
        db.execute(
            """INSERT INTO players
                 (id, web_name, first_name, second_name, element_type, position,
                  team_id, now_cost, selected_by_percent, form, points_per_game,
                  total_points, status, minutes, starts)
               VALUES (?, ?, 'A', 'B', ?, ?, ?, 5.0, 10.0, 3.0, 3.0, 20, 'a', 180, 2)""",
            (pid, f"Player{pid}", etype, pos, (pid % 6) + 1))
        for gw in (1, 2):
            db.execute(
                """INSERT INTO player_gw
                     (player_id, gw, minutes, starts, total_points, goals_scored,
                      assists, clean_sheets, expected_goals, expected_assists,
                      defensive_contribution, saves, bps, bonus, ict_index)
                   VALUES (?, ?, 90, 1, ?, 0, 0, 0, 0.25, 0.15, 6, 0, 20, ?, 5.0)""",
                (pid, gw, 2 + (pid % 8), pid % 3))

    for pid in range(1, 16):
        db.execute(
            """INSERT INTO my_picks (gw, player_id, position, multiplier,
                                     is_captain, is_vice)
               VALUES (2, ?, ?, ?, ?, 0)""",
            (pid, pid, 1 if pid <= 11 else 0, 1 if pid == 1 else 0))
        db.execute(
            """INSERT INTO top_owned (gw, player_id, ownership_pct, captain_pct,
                                      sample_size)
               VALUES (2, ?, ?, ?, 50)""",
            (pid, 50.0 - pid, max(0.0, 20.0 - pid * 2)))

    fid = 1
    for gw in range(1, 9):
        for home, away in ((1, 2), (3, 4), (5, 6)):
            db.execute(
                """INSERT INTO fixtures (id, event, team_h, team_a,
                     team_h_difficulty, team_a_difficulty, kickoff_time, finished)
                   VALUES (?, ?, ?, ?, 3, 3, ?, ?)""",
                (fid, gw, home, away, f"2026-0{min(9, gw)}-10T15:00:00Z",
                 1 if gw <= 2 else 0))
            fid += 1

    db.execute("INSERT INTO meta(key, value) VALUES ('current_gw', '2')")
    db.execute("INSERT INTO meta(key, value) VALUES "
               "('fpl_last_ingest', '2026-01-01T00:00:00')")
    db.commit()
    return db


@pytest.fixture
def empty_db(db, db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(db_path))
    return db


def _run(page: str, timeout: int = 90):
    app = AppTest.from_file(page, default_timeout=timeout)
    app.run()
    return app


def _assert_clean(app, page: str):
    assert not app.exception, (
        f"{page} raised: "
        + "; ".join(str(e.value) for e in app.exception))


# ==========================================================================
class TestPagesMount:
    """The dry run: every page mounts without a runtime exception."""

    @pytest.mark.parametrize("page", [PAGE1, PAGE2])
    def test_mounts_with_data(self, seeded_db, page):
        app = _run(page)
        _assert_clean(app, page)
        assert app.title, f"{page} rendered no title"

    @pytest.mark.parametrize("page", [PAGE1, PAGE2])
    def test_mounts_on_an_empty_database(self, empty_db, page):
        """A fresh install must show a labelled empty state, not a traceback."""
        app = _run(page)
        _assert_clean(app, page)
        text = " ".join(str(m.value) for m in app.markdown) + \
               " ".join(str(c.value) for c in app.caption)
        assert "No data yet" in text or "Refresh Config" in text, (
            "empty database must name the action that fixes it")

    def test_page1_renders_the_kpi_row(self, seeded_db):
        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        labels = [m.label for m in app.metric]
        assert "GW points" in labels
        assert "Luck index" in labels

    def test_page1_has_all_four_tabs(self, seeded_db):
        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        assert len(app.tabs) >= 4

    def test_page2_renders_the_status_strip(self, seeded_db):
        app = _run(PAGE2)
        _assert_clean(app, PAGE2)
        labels = [m.label for m in app.metric]
        assert "Free transfers" in labels or "Compute projections" in " ".join(
            b.label for b in app.button)

    def test_page2_does_not_solve_on_load(self, seeded_db):
        """The ILP must be opt-in; a page that solves on navigation feels frozen."""
        import time
        start = time.monotonic()
        app = _run(PAGE2)
        elapsed = time.monotonic() - start
        _assert_clean(app, PAGE2)
        assert elapsed < 60, f"initial render took {elapsed:.1f}s"

    def test_no_page_shows_a_traceback(self, seeded_db):
        for page in (PAGE1, PAGE2):
            app = _run(page)
            for err in app.error:
                assert "Traceback" not in str(err.value), (
                    f"{page} leaked a traceback into the UI")



    def test_solve_button_produces_three_routes(self, seeded_db):
        """End-to-end through the UI: click Solve, get three legal routes."""
        from fpl_assistant.models import xp

        xp.project(seeded_db, [3, 4, 5], understat_ok=False, persist=True)

        app = _run(PAGE2, timeout=180)
        _assert_clean(app, PAGE2)

        buttons = [b for b in app.button if "Solve" in b.label]
        assert buttons, "the solve button must be present once projections exist"

        buttons[0].click().run()
        _assert_clean(app, PAGE2)

        labels = [m.label for m in app.metric]
        assert labels.count("Net xP") == 3, (
            f"expected three route cards, saw {labels.count('Net xP')}")

    def test_v1_pages_still_mount_after_the_ui_package_move(self):
        """ui.py became ui/, so every v1 page's `from fpl_assistant.ui import
        boot` must still resolve."""
        for name in ("1_My_Squad.py", "5_Captaincy.py",
                     "4_Template_and_Differentials.py"):
            page = PROJECT_ROOT / "pages" / name
            app = AppTest.from_file(str(page), default_timeout=90)
            app.run()
            assert not app.exception, (
                f"{name} broke: "
                + "; ".join(str(e.value) for e in app.exception))

# ==========================================================================
class TestErrorBoundaries:
    def test_boundary_contains_a_failure(self):
        from fpl_assistant.ui.components import error_boundary

        # Outside a Streamlit script context the calls are no-ops, but the
        # exception must still be swallowed rather than propagate.
        with error_boundary("test panel"):
            raise ValueError("boom")

    def test_safe_frame_tolerates_missing_keys(self):
        from fpl_assistant.ui.components import safe_frame

        frame = safe_frame([{"a": 1}, {"b": 2}], columns=["a", "b", "c"])
        assert list(frame.columns) == ["a", "b", "c"]
        assert len(frame) == 2

    def test_safe_frame_handles_no_rows(self):
        from fpl_assistant.ui.components import safe_frame

        assert safe_frame([], columns=["a", "b"]).empty


# ==========================================================================
class TestServiceContracts:
    """Services must never raise on partial data -- panels fail soft."""

    def test_gw_summary_builds_on_an_empty_database(self, empty_db):
        from fpl_assistant.config import load_config

        vm = gw_summary.build(empty_db, load_config(), degrade.collect(empty_db))
        assert vm.kpis.my_points == 0
        assert vm.variance == []
        assert not vm.has_squad

    def test_command_center_builds_on_an_empty_database(self, empty_db):
        from fpl_assistant.config import load_config

        vm = command_center.build(empty_db, load_config(),
                                  degrade.collect(empty_db))
        assert vm.routes == []
        assert vm.moves == []
        assert not vm.has_squad

    def test_gw_summary_survives_missing_rivals(self, seeded_db):
        from fpl_assistant.config import load_config

        vm = gw_summary.build(seeded_db, load_config(),
                              degrade.collect(seeded_db),
                              rival_ids=[999, 998])
        assert vm.swing is not None
        assert vm.swing.rows == []
        assert vm.errors == []

    def test_command_center_survives_no_projections(self, seeded_db):
        from fpl_assistant.config import load_config

        vm = command_center.build(seeded_db, load_config(),
                                  degrade.collect(seeded_db), run_solver=True)
        assert vm.routes == []          # nothing to optimise over
        assert "Traceback" not in " ".join(vm.errors)

    def test_command_center_solves_when_projections_exist(self, seeded_db):
        from fpl_assistant.config import load_config
        from fpl_assistant.models import xp

        cfg = load_config()
        window = [3, 4, 5]
        xp.project(seeded_db, window, understat_ok=False, persist=True)

        vm = command_center.build(seeded_db, cfg, degrade.collect(seeded_db),
                                  horizon=3, run_solver=True, time_limit=25,
                                  candidates_k=10)
        assert vm.routes, f"no routes: {vm.errors}"
        assert len(vm.routes) == 3

    def test_solved_routes_are_legal(self, seeded_db):
        """The UI must never display an illegal squad."""
        from fpl_assistant.config import load_config
        from fpl_assistant.models import xp
        from fpl_assistant.strategy import validator

        cfg = load_config()
        xp.project(seeded_db, [3, 4, 5], understat_ok=False, persist=True)
        vm = command_center.build(seeded_db, cfg, degrade.collect(seeded_db),
                                  horizon=3, run_solver=True, time_limit=25,
                                  candidates_k=10)

        players = {r["id"]: dict(r) for r in seeded_db.execute(
            "SELECT id, element_type, position, team_id, now_cost FROM players")}

        optimal = [p for p in vm.routes if p.status == "Optimal"]
        assert optimal, (
            "no route solved, so this test would pass without checking "
            f"anything: {[p.status for p in vm.routes]}")

        for path in optimal:
            result = validator.validate_path(path, players)
            assert result.legal, f"{path.profile}: {result.report()}"


# ==========================================================================
class TestDegradedStates:
    def test_understat_offline_produces_the_badge(self, seeded_db):
        from fpl_assistant.jobs import tasks

        tasks._flag_understat_offline(seeded_db, "rate limited")
        quality = degrade.collect(seeded_db)
        assert quality.understat_offline
        assert quality.understat_badge == (
            "Understat Offline - Using Baseline Stats")

    def test_baseline_share_flags_a_silent_fallback(self, seeded_db):
        """Understat healthy but unused still owes the operator a badge."""
        from fpl_assistant.models import xp

        xp.project(seeded_db, [3], understat_ok=False, persist=True)
        quality = degrade.collect(seeded_db)
        assert quality.baseline_share == 1.0
        assert quality.on_baseline
        assert not quality.understat_offline
        assert any("baseline" in n.lower() for n in quality.notes)

    def test_blocking_reason_names_the_fix(self, empty_db):
        reason = degrade.collect(empty_db).blocking_reason()
        assert reason and "Refresh Config" in reason

    def test_no_blocking_reason_once_data_exists(self, seeded_db):
        assert degrade.collect(seeded_db).blocking_reason() is None

    def test_page1_shows_the_degraded_banner(self, seeded_db):
        from fpl_assistant.jobs import tasks

        tasks._flag_understat_offline(seeded_db, "down")
        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        warnings = " ".join(str(w.value) for w in app.warning)
        assert "Understat Offline" in warnings


# ==========================================================================
# Phase 4 - full outage chain, source failure through to rendered badge
# ==========================================================================
class TestOutageReachesTheScreen:
    """R1 end to end.

    The existing degradation tests set the health flag directly. This class
    starts one level further back, at a genuinely failing Understat call, and
    follows the consequence all the way to text on a rendered page. Every link
    in that chain has failed independently at some point in this build; testing
    only the ends would not have caught any of them.
    """

    def _break_understat(self, monkeypatch):
        from fpl_assistant.sources.base import SourceResult
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.league_players",
            lambda self, season: SourceResult.unavailable(
                "understat", "503 Service Unavailable"))

    def test_outage_flows_from_source_failure_to_page1_banner(
            self, seeded_db, monkeypatch):
        from fpl_assistant.jobs import tasks

        self._break_understat(monkeypatch)
        result = tasks.ingest_understat_league(seeded_db, season=2025)

        assert result["ok"] is False and result["degraded"] is True
        assert tasks.understat_offline(seeded_db) is True

        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        assert "Understat Offline - Using Baseline Stats" in " ".join(
            str(w.value) for w in app.warning)

    def test_outage_also_badges_page2(self, seeded_db, monkeypatch):
        from fpl_assistant.jobs import tasks

        self._break_understat(monkeypatch)
        tasks.ingest_understat_league(seeded_db, season=2025)

        app = _run(PAGE2)
        _assert_clean(app, PAGE2)
        assert "Understat Offline" in " ".join(
            str(w.value) for w in app.warning)

    def test_projections_still_compute_during_the_outage(self, seeded_db,
                                                         monkeypatch):
        """Degradation must cost accuracy, never availability."""
        from fpl_assistant.jobs import tasks

        self._break_understat(monkeypatch)
        tasks.ingest_understat_league(seeded_db, season=2025)

        out = tasks.recompute_xp(seeded_db, gws=[3])
        assert out["ok"] and out["projections"] > 0
        assert out["understat_ok"] is False
        assert set(out["sources"]) == {"fpl_baseline"}

    def test_snapshot_taken_during_an_outage_records_the_fact(
            self, seeded_db, monkeypatch):
        """A frozen forecast must carry the quality it was made under."""
        import datetime as dt

        from fpl_assistant.jobs import tasks
        from fpl_assistant.models import snapshot as snap

        self._break_understat(monkeypatch)
        tasks.ingest_understat_league(seeded_db, season=2025)

        line = snap.deadline_for(seeded_db, 3)
        at = line.when - dt.timedelta(minutes=59)
        result = snap.capture(seeded_db, 3, now=at)

        assert result.frozen
        assert result.understat_ok is False
        assert snap.snapshot_meta(seeded_db, 3)["understat_ok"] == 0

    def test_recovery_clears_the_banner_on_the_page(self, seeded_db):
        from fpl_assistant.jobs import tasks

        tasks._flag_understat_offline(seeded_db, "boom")
        tasks._flag_understat_online(seeded_db)

        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        assert "Understat Offline" not in " ".join(
            str(w.value) for w in app.warning)


class TestProcessAxisOnThePage:
    def test_page1_states_the_caveat_when_nothing_is_frozen(self, seeded_db):
        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        text = " ".join(str(i.value) for i in app.info)
        assert "before kickoff" in text or "frozen" in text

    def test_page1_drops_the_caveat_once_a_snapshot_exists(self, seeded_db):
        from fpl_assistant.models import snapshot as snap

        snap.capture(seeded_db, 2, force=True)
        app = _run(PAGE1)
        _assert_clean(app, PAGE1)
        assert not any("No pre-deadline projection" in str(i.value)
                       for i in app.info)
