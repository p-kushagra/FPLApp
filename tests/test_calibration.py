"""Phase 4 - the calibration gate and the pre-deadline snapshot.

The single most important test in this file is `TestLeakGuard`. Every other
number the calibration gate produces is meaningless if a projection for a played
gameweek can see that gameweek's own results, and that failure is silent: a
leaked backtest does not crash, it just reports a model that looks good. So the
leak is tested directly, by constructing a player whose history and whose target
gameweek disagree sharply and asserting the forecast follows the history.
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from fpl_assistant.models import calibration as cal
from fpl_assistant.models import snapshot as snap
from fpl_assistant.models import xp as xp_mod
from fpl_assistant.services import degrade, gw_summary

DEADLINE = dt.datetime(2026, 9, 4, 17, 30, tzinfo=dt.timezone.utc)


# ==========================================================================
# Fixtures
# ==========================================================================
def _seed(conn, *, gws=(1, 2), players=24, deadline=DEADLINE):
    """A minimal but structurally real universe.

    Six clubs so squad legality is reachable, and a deliberate spread of scoring
    rates so that ranking tests have something to rank.
    """
    for tid in range(1, 7):
        conn.execute(
            """INSERT OR REPLACE INTO teams
                 (id, name, short_name, strength_attack_home,
                  strength_attack_away, strength_defence_home,
                  strength_defence_away)
               VALUES (?, ?, ?, 1200, 1150, 1200, 1150)""",
            (tid, f"Club {tid}", f"C{tid}"))

    for pid in range(1, players + 1):
        etype = 1 if pid % 8 == 0 else (2 if pid % 3 == 0 else
                                        (3 if pid % 3 == 1 else 4))
        pos = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}[etype]
        conn.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, first_name, second_name, team_id, element_type,
                  position, now_cost, status, ep_next, selected_by_percent,
                  total_points)
               VALUES (?, ?, 'A', 'B', ?, ?, ?, ?, 'a', ?, 5.0, 0)""",
            (pid, f"P{pid}", (pid % 6) + 1, etype, pos,
             4.0 + (pid % 8) * 0.5, 2.0 + (pid % 5)))

    # Fixtures: every club plays every gameweek, three matches per round.
    fid = 0
    for gw in range(1, max(gws) + 3):
        kickoff = deadline + dt.timedelta(minutes=90, days=7 * (gw - 3))
        for home, away in ((1, 2), (3, 4), (5, 6)):
            fid += 1
            conn.execute(
                """INSERT OR REPLACE INTO fixtures
                     (id, event, team_h, team_a, team_h_difficulty,
                      team_a_difficulty, kickoff_time, finished)
                   VALUES (?, ?, ?, ?, 3, 3, ?, ?)""",
                (fid, gw, home, away, kickoff.isoformat(), int(gw <= max(gws))))

    # History. Scoring rate rises with player id so xP has a real gradient.
    for gw in gws:
        for pid in range(1, players + 1):
            rate = pid / players
            conn.execute(
                """INSERT OR REPLACE INTO player_gw
                     (player_id, gw, minutes, starts, total_points,
                      goals_scored, assists, clean_sheets, expected_goals,
                      expected_assists, defensive_contribution, saves, bonus,
                      bps, yellow_cards, red_cards)
                   VALUES (?, ?, 90, 1, ?, ?, 0, 0, ?, ?, 4, 0, ?, 20, 0, 0)""",
                (pid, gw, round(2 + 8 * rate), int(rate > 0.7),
                 round(0.6 * rate, 2), round(0.3 * rate, 2), int(rate > 0.8)))
    conn.commit()


@pytest.fixture
def universe(db):
    _seed(db)
    return db


# ==========================================================================
# Anti-leakage - the load-bearing test in this module
# ==========================================================================
class TestLeakGuard:
    """A backtest that can see its own answer reports a model that isn't real."""

    @pytest.fixture
    def two_faced(self, db):
        """One player who was dreadful in GW1 and superb in GW2.

        An honest forecast for GW2 must reflect the dreadful GW1. A leaked one
        reflects the superb GW2. The gap between those is the whole test.
        """
        _seed(db, gws=(1,), players=24)
        db.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points, goals_scored,
                  assists, clean_sheets, expected_goals, expected_assists,
                  defensive_contribution, saves, bonus, bps, yellow_cards,
                  red_cards)
               VALUES (1, 1, 90, 1, 1, 0, 0, 0, 0.01, 0.01, 1, 0, 0, 5, 0, 0)""")
        db.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points, goals_scored,
                  assists, clean_sheets, expected_goals, expected_assists,
                  defensive_contribution, saves, bonus, bps, yellow_cards,
                  red_cards)
               VALUES (1, 2, 90, 1, 20, 3, 1, 0, 3.20, 1.10, 9, 0, 3, 90, 0, 0)""")
        db.commit()
        return db

    def _xp(self, conn, gw, **kw):
        out = xp_mod.project(conn, [gw], player_ids=[1], persist=False, **kw)
        return out[(1, gw)].total

    def test_as_of_withholds_the_target_gameweek(self, two_faced):
        honest = self._xp(two_faced, 2, as_of=1)
        leaked = self._xp(two_faced, 2, as_of=2)
        assert leaked > honest * 1.25, (
            f"as_of is not filtering: honest={honest:.3f} leaked={leaked:.3f}. "
            "A projection built with as_of=1 must not be able to see GW2.")

    def test_default_as_of_is_the_leaked_one(self, two_faced):
        """Guards the reason `as_of` has to be passed explicitly.

        The default is right for live planning and wrong for backtesting. If
        this ever stops being true the calibration harness can drop the
        argument -- until then, forgetting it silently leaks.
        """
        assert self._xp(two_faced, 2) == pytest.approx(
            self._xp(two_faced, 2, as_of=2))

    def test_observations_are_built_out_of_sample(self, two_faced):
        obs = {o.player_id: o for o in
               cal.observations(two_faced, [2], prefer_snapshot=False)}
        assert obs[1].xp < 5.0, (
            f"xP {obs[1].xp:.2f} for a player who scored 1 point in the only "
            "prior gameweek - the harness is reading GW2")

    def test_neutralised_availability_ignores_todays_injury(self, two_faced):
        two_faced.execute(
            "UPDATE players SET status='i', chance_of_playing_next_round=0 "
            "WHERE id=1")
        two_faced.commit()
        live = self._xp(two_faced, 2, as_of=1)
        replay = self._xp(two_faced, 2, as_of=1, neutralise_availability=True)
        assert live == pytest.approx(0.0, abs=1e-9)
        assert replay > 0.0, (
            "a replay of GW2 must not apply an injury recorded today")


# ==========================================================================
# Metric primitives
# ==========================================================================
class TestMetrics:
    def test_rmse_is_zero_for_a_perfect_forecast(self):
        assert cal.rmse([1, 2, 3], [1, 2, 3]) == pytest.approx(0.0)

    def test_rmse_matches_hand_calculation(self):
        # errors 1, -1, 2 -> sqrt((1+1+4)/3)
        assert cal.rmse([2, 1, 5], [1, 2, 3]) == pytest.approx(
            math.sqrt(6 / 3))

    def test_rmse_punishes_outliers_harder_than_mae(self):
        pred, obs = [0, 0, 10], [0, 0, 0]
        assert cal.rmse(pred, obs) > cal.mae(pred, obs)

    def test_bias_signs_over_forecasting_positive(self):
        assert cal.bias([3, 3], [1, 1]) == pytest.approx(2.0)
        assert cal.bias([1, 1], [3, 3]) == pytest.approx(-2.0)

    def test_spearman_is_one_for_a_monotone_map(self):
        assert cal.spearman([1, 2, 3, 4], [10, 20, 31, 44]) == pytest.approx(1.0)

    def test_spearman_is_minus_one_when_reversed(self):
        assert cal.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)

    def test_spearman_handles_ties_without_raising(self):
        assert not math.isnan(cal.spearman([1, 1, 2, 3], [1, 2, 2, 3]))

    def test_empty_input_is_nan_not_an_exception(self):
        assert math.isnan(cal.rmse([], []))
        assert math.isnan(cal.mae([], []))


# ==========================================================================
# Decile monotonicity
# ==========================================================================
def _obs(pairs):
    return [cal.Observation(player_id=i, gw=2, position="MID", xp=x,
                            actual=a, minutes=90, source="test")
            for i, (x, a) in enumerate(pairs)]


class TestDecileGate:
    def test_perfectly_ordered_model_is_monotonic(self):
        report = cal.deciles(_obs([(i / 10, i / 10) for i in range(100)]))
        assert report.monotonic
        assert report.inversions == []
        assert report.spearman == pytest.approx(1.0)

    def test_reversed_model_is_rejected(self):
        report = cal.deciles(_obs([(i / 10, 10 - i / 10) for i in range(100)]))
        assert not report.monotonic
        assert report.spearman < 0

    def test_random_ordering_is_rejected(self):
        """A model with no ranking power must fail the gate, not scrape past."""
        actuals = [(i * 37) % 100 for i in range(100)]
        report = cal.deciles(_obs([(i / 10, a) for i, a in enumerate(actuals)]))
        assert not report.monotonic

    def test_one_local_inversion_is_tolerated(self):
        """Ten noisy buckets invert occasionally even for a good model."""
        pairs = [(i / 10, i / 10) for i in range(100)]
        pairs[45] = (4.5, 0.0)   # dent decile 5 slightly
        report = cal.deciles(_obs(pairs))
        assert len(report.inversions) <= cal.MAX_DECILE_INVERSIONS
        assert report.monotonic

    def test_lift_reports_top_minus_bottom(self):
        report = cal.deciles(_obs([(i / 10, i / 10) for i in range(100)]))
        assert report.lift > 0

    def test_too_few_rows_produces_no_deciles_rather_than_junk(self):
        report = cal.deciles(_obs([(1.0, 1.0), (2.0, 2.0)]))
        assert report.deciles == []
        assert not report.monotonic


# ==========================================================================
# Baselines and the verdict
# ==========================================================================
class TestBaselines:
    def test_ep_next_is_unavailable_without_a_snapshot(self, universe):
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        ep = next(b for b in report.baselines if b.name == cal.BASELINE_EP_NEXT)
        assert ep.unavailable is not None
        assert "history" in ep.unavailable

    def test_ep_next_becomes_available_once_frozen(self, universe):
        snap.capture(universe, 2, force=True)
        report = cal.evaluate(universe, [2])
        ep = next(b for b in report.baselines if b.name == cal.BASELINE_EP_NEXT)
        assert ep.unavailable is None
        assert ep.n > 0

    def test_positional_mean_is_always_computable(self, universe):
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        pm = next(b for b in report.baselines
                  if b.name == cal.BASELINE_POSITIONAL)
        assert pm.unavailable is None

    def test_model_is_scored_on_the_rows_the_baseline_covers(self, universe):
        """Comparing two different populations is not a comparison."""
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        for b in report.baselines:
            if b.unavailable is None and b.name != cal.BASELINE_POSITIONAL:
                assert b.n <= report.n_rows


class TestVerdict:
    def test_thin_sample_refuses_to_return_a_verdict(self, universe):
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        assert report.verdict == "INSUFFICIENT_EVIDENCE"
        assert not report.passed
        assert any("sample too thin" in b for b in report.blockers)

    def test_thin_sample_does_not_claim_failure_either(self, universe):
        """'Not proven' and 'proven bad' are different claims."""
        assert cal.evaluate(universe, [2],
                            prefer_snapshot=False).verdict != "FAIL"

    def test_no_data_is_reported_not_crashed(self, db):
        report = cal.evaluate(db, [99])
        assert report.n_rows == 0
        assert not report.passed

    def test_gw1_is_excluded_from_evaluable_gameweeks(self, universe):
        """GW1 has no prior history, so nothing out-of-sample can be built."""
        assert 1 not in cal.evaluable_gws(universe)

    def test_report_persists_and_reloads(self, universe):
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        cal.persist(universe, report)
        stored = cal.latest_run(universe)
        assert stored["run_id"] == report.run_id
        assert stored["rmse_model"] == pytest.approx(report.rmse)

    def test_format_report_renders_without_a_baseline(self, universe):
        text = cal.format_report(cal.evaluate(universe, [2],
                                              prefer_snapshot=False))
        assert "VERDICT" in text
        assert "unavailable" in text


class TestAffineFit:
    def test_fit_is_withheld_on_too_few_gameweeks(self, universe):
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        fits = cal.fit_affine(universe, report)
        assert fits
        assert all(not f.applied for f in fits), (
            "a one-fold fit must not be allowed to move recommendations")
        assert cal.active_fit(universe) == {}

    def test_fit_never_increases_in_sample_error(self, universe):
        """Least squares cannot do worse in-sample. A violation means a bug."""
        report = cal.evaluate(universe, [2], prefer_snapshot=False)
        for f in cal.fit_affine(universe, report):
            assert f.rmse_after <= f.rmse_before + 1e-6

    def test_fit_recovers_a_known_linear_distortion(self):
        """Halve every projection; the fit must find slope ~2."""
        obs = [cal.Observation(player_id=i, gw=2, position="MID",
                               xp=i / 20.0, actual=i / 10.0, minutes=90,
                               source="t") for i in range(1, 60)]
        report = cal.CalibrationReport(run_id="x", created_at="", gws=[2])

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def executemany(self, *a): pass

        fit = cal.fit_affine(_Conn(), report, obs, persist=False)[0]
        assert fit.slope == pytest.approx(2.0, abs=0.01)
        assert fit.intercept == pytest.approx(0.0, abs=0.01)


# ==========================================================================
# Snapshot pipeline
# ==========================================================================
class TestDeadlineResolution:
    def test_official_deadline_wins_over_the_estimate(self, universe):
        universe.execute(
            "INSERT OR REPLACE INTO gw_state (gw, deadline_time) VALUES (3, ?)",
            ("2026-09-04T11:00:00Z",))
        universe.commit()
        line = snap.deadline_for(universe, 3)
        assert line.source == snap.DEADLINE_OFFICIAL
        assert line.when.hour == 11

    def test_estimate_is_ninety_minutes_before_first_kickoff(self, universe):
        line = snap.deadline_for(universe, 3)
        assert line.source == snap.DEADLINE_ESTIMATED
        first = universe.execute(
            "SELECT MIN(kickoff_time) FROM fixtures WHERE event=3").fetchone()[0]
        expected = dt.datetime.fromisoformat(
            first.replace("Z", "+00:00")) - dt.timedelta(minutes=90)
        assert line.when == expected

    def test_unknown_gameweek_reports_unknown_not_an_exception(self, universe):
        line = snap.deadline_for(universe, 38)
        assert line.source == snap.DEADLINE_UNKNOWN
        assert line.when is None

    def test_freeze_target_is_one_hour_before(self, universe):
        line = snap.deadline_for(universe, 3)
        assert (line.when - line.freeze_at()).total_seconds() == 3600


class TestCaptureWindow:
    def _at(self, conn, gw, minutes_before):
        return snap.deadline_for(conn, gw).when - dt.timedelta(
            minutes=minutes_before)

    def test_refuses_when_too_early(self, universe):
        check = snap.check_due(universe, 3, self._at(universe, 3, 60 * 24))
        assert not check.due
        assert "too early" in check.reason

    def test_due_inside_the_window(self, universe):
        assert snap.check_due(universe, 3, self._at(universe, 3, 59)).due

    def test_refuses_after_the_deadline(self, universe):
        check = snap.check_due(universe, 3, self._at(universe, 3, -1))
        assert not check.due
        assert "passed" in check.reason

    def test_capture_honours_the_refusal(self, universe):
        result = snap.capture(universe, 3, now=self._at(universe, 3, 60 * 24))
        assert not result.frozen
        assert result.rows == 0
        assert not snap.has_snapshot(universe, 3)

    def test_scheduler_picks_up_only_gameweeks_in_window(self, universe):
        at = self._at(universe, 3, 59)
        assert [c.gw for c in snap.due(universe, at)] == [3]

    def test_scheduler_is_quiet_outside_the_window(self, universe):
        assert snap.due(universe, self._at(universe, 3, 60 * 24)) == []


class TestWriteOnce:
    @pytest.fixture
    def frozen(self, universe):
        at = snap.deadline_for(universe, 3).when - dt.timedelta(minutes=59)
        snap.capture(universe, 3, now=at)
        return universe

    def test_capture_writes_every_player(self, frozen):
        assert snap.has_snapshot(frozen, 3)
        rows = frozen.execute(
            "SELECT COUNT(*) FROM projection_snapshot WHERE gw=3").fetchone()[0]
        assert rows == frozen.execute(
            "SELECT COUNT(*) FROM players").fetchone()[0]

    def test_second_capture_is_refused(self, frozen):
        before = snap.load(frozen, 3)
        at = snap.deadline_for(frozen, 3).when - dt.timedelta(minutes=30)
        result = snap.capture(frozen, 3, now=at)
        assert not result.frozen
        assert "write-once" in result.reason
        assert [r["xp_total"] for r in snap.load(frozen, 3)] == \
               [r["xp_total"] for r in before]

    def test_a_later_projection_cannot_rewrite_history(self, frozen):
        """The whole point: results arriving must not change the forecast."""
        before = {r["player_id"]: r["xp_total"] for r in snap.load(frozen, 3)}
        frozen.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points, goals_scored,
                  assists, expected_goals, expected_assists, bonus, bps)
               VALUES (1, 3, 90, 1, 24, 4, 0, 4.0, 0.5, 3, 99)""")
        frozen.commit()
        at = snap.deadline_for(frozen, 3).when - dt.timedelta(minutes=10)
        snap.capture(frozen, 3, now=at)
        after = {r["player_id"]: r["xp_total"] for r in snap.load(frozen, 3)}
        assert after == before

    def test_ep_next_is_captured_alongside(self, frozen):
        assert all(r["ep_next"] is not None for r in snap.load(frozen, 3))

    def test_meta_records_provenance(self, frozen):
        meta = snap.snapshot_meta(frozen, 3)
        assert meta["rows"] > 0
        assert meta["deadline_source"] == snap.DEADLINE_ESTIMATED
        assert 58 <= meta["lead_minutes"] <= 60

    def test_frozen_gws_lists_it(self, frozen):
        assert snap.frozen_gws(frozen) == [3]


class TestLateCapture:
    def test_forced_late_capture_is_tagged(self, universe):
        result = snap.capture(universe, 2, force=True)
        assert result.frozen
        assert result.deadline_source.endswith("+late")
        assert any("after the deadline" in n for n in result.notes)

    def test_forced_late_capture_does_not_leak(self, db):
        """A catch-up snapshot must replay, not score itself."""
        _seed(db, gws=(1, 2))
        db.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points, goals_scored,
                  assists, expected_goals, expected_assists, bonus, bps)
               VALUES (1, 2, 90, 1, 24, 4, 0, 4.00, 0.90, 3, 99)""")
        db.commit()
        snap.capture(db, 2, force=True)
        frozen = {r["player_id"]: r["xp_total"] for r in snap.load(db, 2)}
        honest = xp_mod.project(db, [2], player_ids=[1], persist=False,
                                as_of=1, neutralise_availability=True)[(1, 2)]
        assert frozen[1] == pytest.approx(honest.total, abs=1e-6), (
            "a forced late capture read the gameweek it was freezing")

    def test_understat_outage_is_recorded_on_the_snapshot(self, universe):
        at = snap.deadline_for(universe, 3).when - dt.timedelta(minutes=59)
        result = snap.capture(universe, 3, now=at, understat_ok=False)
        assert result.frozen
        assert snap.snapshot_meta(universe, 3)["understat_ok"] == 0
        assert any("baseline" in n for n in result.notes)


# ==========================================================================
# Page 1: the Process axis
# ==========================================================================
class TestProcessAxis:
    def test_luck_only_without_a_snapshot(self, universe):
        vm = gw_summary.build(universe, _cfg(), degrade.collect(universe), gw=2)
        assert vm.variance_mode == "luck_only"
        assert vm.variance_caveat is not None

    def test_a_posthoc_projection_does_not_unlock_the_process_axis(self,
                                                                  universe):
        """The regression this replaced.

        The old rule was "full if any xP is non-zero", which `recompute_xp`
        satisfies for a played gameweek using a projection that has already
        seen the result. Only a frozen snapshot may unlock the second axis.
        """
        xp_mod.project(universe, [2], persist=True)
        assert universe.execute(
            "SELECT COUNT(*) FROM xp_projection WHERE gw=2").fetchone()[0] > 0
        vm = gw_summary.build(universe, _cfg(), degrade.collect(universe), gw=2)
        assert vm.variance_mode == "luck_only"

    def test_snapshot_unlocks_the_process_axis(self, universe):
        snap.capture(universe, 2, force=True)
        vm = gw_summary.build(universe, _cfg(), degrade.collect(universe), gw=2)
        assert vm.variance_mode == "full"
        assert vm.variance_caveat is None
        assert any(r.process != 0 for r in vm.variance)

    def test_snapshot_meta_reaches_the_view_model(self, universe):
        snap.capture(universe, 2, force=True)
        vm = gw_summary.build(universe, _cfg(), degrade.collect(universe), gw=2)
        assert vm.snapshot_meta is not None
        assert vm.snapshot_meta["rows"] > 0

    def test_variance_survives_a_gameweek_with_no_snapshot_columns(self,
                                                                  universe):
        rows = gw_summary._variance(universe, 2, None, squad_only=False,
                                    frozen=False)
        assert rows
        assert all(r.xp == 0.0 for r in rows)


def _cfg():
    from fpl_assistant.config import load_config
    return load_config()


# ==========================================================================
# Job wiring
# ==========================================================================
class TestJobs:
    def test_freeze_job_is_registered(self):
        from fpl_assistant.jobs.tasks import REGISTRY
        assert "freeze_projections" in REGISTRY
        assert "calibrate" in REGISTRY

    def test_freeze_job_is_a_noop_outside_the_window(self, universe):
        from fpl_assistant.jobs import tasks
        out = tasks.freeze_projections(universe)
        assert out["ok"]
        assert out["frozen"] == []

    def test_freeze_job_captures_an_explicit_gameweek(self, universe):
        from fpl_assistant.jobs import tasks
        out = tasks.freeze_projections(universe, gws=[2], force=True)
        assert out["ok"]
        assert out["frozen"] and out["frozen"][0]["rows"] > 0
        assert snap.has_snapshot(universe, 2)

    def test_calibrate_job_records_a_verdict(self, universe):
        from fpl_assistant.jobs import tasks
        out = tasks.calibrate(universe, gws=[2])
        assert out["ok"]
        assert out["verdict"] in ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
        assert cal.latest_run(universe) is not None
