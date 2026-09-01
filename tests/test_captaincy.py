"""Shield vs Sword captaincy matrix.

The premise under test: captaincy is a RANK decision, not a points decision.
Ranking by expected points alone cannot distinguish "match the field so its
ceiling cannot hurt me" from "take unmatched variance because expected points
provably cannot close this gap".
"""
from __future__ import annotations

import pytest

from fpl_assistant.models import xp as xp_model
from fpl_assistant.strategy import captaincy
from fpl_assistant.strategy.captaincy import Regime


class TestRegimeSelection:
    def test_leading_always_shields(self):
        call = captaincy.regime(deficit_points=-30, gameweeks_left=10)
        assert call.regime is Regime.SHIELD
        assert "Leading" in call.reason

    def test_small_deficit_shields(self):
        """Below the noise floor, form closes the gap without taking risk."""
        call = captaincy.regime(deficit_points=10, gameweeks_left=20)  # 0.5/GW
        assert call.regime is Regime.SHIELD
        assert call.required_edge == pytest.approx(0.5)

    def test_large_deficit_draws_the_sword(self):
        call = captaincy.regime(deficit_points=60, gameweeks_left=10)  # 6.0/GW
        assert call.regime is Regime.SWORD
        assert "variance" in call.reason

    def test_threshold_is_the_switch_point(self):
        assert captaincy.regime(19, 10).regime is Regime.SHIELD   # 1.9/GW
        assert captaincy.regime(21, 10).regime is Regime.SWORD    # 2.1/GW

    def test_same_deficit_flips_as_the_season_runs_out(self):
        """30 points behind is patient in November and desperate in April."""
        assert captaincy.regime(30, 25).regime is Regime.SHIELD   # 1.2/GW
        assert captaincy.regime(30, 5).regime is Regime.SWORD     # 6.0/GW

    def test_zero_gameweeks_does_not_divide_by_zero(self):
        call = captaincy.regime(deficit_points=10, gameweeks_left=0)
        assert call.gameweeks_left == 1
        assert call.regime is Regime.SWORD

    def test_threshold_is_tunable(self):
        assert captaincy.regime(30, 10, threshold=5.0).regime is Regime.SHIELD
        assert captaincy.regime(30, 10, threshold=1.0).regime is Regime.SWORD


class TestShieldSwordMatrix:
    @pytest.fixture
    def seeded(self, db):
        """Three archetypes: template premium, differential, and a bench-warmer."""
        for tid, short in ((1, "MCI"), (2, "BRE")):
            db.execute("INSERT INTO teams(id, name, short_name) VALUES (?, ?, ?)",
                       (tid, short, short))
        rows = [
            # id, name, team, xp, p_haul, p_floor
            (1, "Haaland", 1, 8.4, 0.42, 0.72),   # template: high EO
            (2, "Mbeumo", 2, 6.1, 0.31, 0.55),    # differential: low EO
            (3, "Benchy", 2, 1.2, 0.02, 0.10),    # neither
        ]
        for pid, name, team, xp, haul, floor in rows:
            db.execute(
                """INSERT INTO players (id, web_name, position, element_type, team_id)
                   VALUES (?, ?, 'FWD', 4, ?)""", (pid, name, team))
            db.execute(
                """INSERT INTO xp_projection
                     (player_id, gw, run_id, fixtures, xp_total, p_haul_12,
                      p_floor_5, source, computed_at)
                   VALUES (?, 15, 'run1', 1, ?, ?, ?, 'understat',
                           '2026-01-01T00:00:00+00:00')""",
                (pid, xp, haul, floor))
        db.commit()
        return db

    def test_matrix_ranks_both_indices(self, seeded):
        options = captaincy.matrix(seeded, gw=15, run_id="run1",
                                   ileo_cap={1: 0.71, 2: 0.08, 3: 0.0})
        by_name = {o.web_name: o for o in options}

        assert by_name["Haaland"].shield > by_name["Mbeumo"].shield
        assert by_name["Mbeumo"].sword > by_name["Haaland"].sword

    def test_classification_follows_the_indices(self, seeded):
        options = captaincy.matrix(seeded, gw=15, run_id="run1",
                                   ileo_cap={1: 0.71, 2: 0.08, 3: 0.0})
        by_name = {o.web_name: o for o in options}
        assert by_name["Haaland"].classification == "Shield"
        assert by_name["Mbeumo"].classification == "Sword"

    def test_shield_is_zero_when_nobody_owns_you(self, seeded):
        """Shield is meaningless without field exposure to match."""
        options = captaincy.matrix(seeded, gw=15, run_id="run1", ileo_cap={})
        assert all(o.shield == 0.0 for o in options)
        assert any(o.sword > 0 for o in options), "Sword still works with no EO"

    def test_ranks_are_dense_and_start_at_one(self, seeded):
        options = captaincy.matrix(seeded, gw=15, run_id="run1",
                                   ileo_cap={1: 0.71, 2: 0.08})
        assert sorted(o.shield_rank for o in options) == [1, 2, 3]
        assert sorted(o.sword_rank for o in options) == [1, 2, 3]

    def test_empty_projections_give_an_empty_matrix(self, db):
        assert captaincy.matrix(db, gw=99) == []

    def test_candidate_filter_restricts_the_pool(self, seeded):
        options = captaincy.matrix(seeded, gw=15, run_id="run1",
                                   candidate_ids=[1])
        assert [o.player_id for o in options] == [1]


class TestRecommendation:
    @pytest.fixture
    def options(self, db):
        for tid in (1, 2):
            db.execute("INSERT INTO teams(id, name, short_name) VALUES (?, 'T', 'T')",
                       (tid,))
        for pid, name, xp, haul, floor in (
            (1, "Haaland", 8.4, 0.42, 0.72),
            (2, "Mbeumo", 6.1, 0.31, 0.55),
        ):
            db.execute(
                """INSERT INTO players (id, web_name, position, element_type, team_id)
                   VALUES (?, ?, 'FWD', 4, 1)""", (pid, name))
            db.execute(
                """INSERT INTO xp_projection
                     (player_id, gw, run_id, fixtures, xp_total, p_haul_12,
                      p_floor_5, source, computed_at)
                   VALUES (?, 15, 'r', 1, ?, ?, ?, 'understat', '2026-01-01')""",
                (pid, xp, haul, floor))
        db.commit()
        return captaincy.matrix(db, gw=15, run_id="r",
                                ileo_cap={1: 0.71, 2: 0.08})

    def test_shield_regime_picks_the_template(self, options):
        pick, why = captaincy.recommend(options, captaincy.regime(5, 20))
        assert pick.web_name == "Haaland"
        assert "Shield" in why

    def test_sword_regime_picks_the_differential(self, options):
        pick, why = captaincy.recommend(options, captaincy.regime(60, 10))
        assert pick.web_name == "Mbeumo"
        assert "Sword" in why

    def test_reasoning_names_the_arithmetic(self, options):
        _, why = captaincy.recommend(options, captaincy.regime(60, 10))
        assert "pts/GW" in why

    def test_shield_advice_acknowledges_the_alternative(self, options):
        """A recommendation the operator cannot interrogate is just assertive."""
        _, why = captaincy.recommend(options, captaincy.regime(5, 20))
        assert "Mbeumo" in why, "must say what it declined and why"

    def test_no_options_returns_no_pick(self):
        pick, why = captaincy.recommend([], captaincy.regime(0, 10))
        assert pick is None
        assert "No projections" in why


class TestTailProbabilities:
    """P(haul) and P(floor) drive both indices, so their shape matters."""

    def test_tail_is_monotone_in_the_mean(self):
        low = xp_model._tail(3.0, 3.0, 12.0)
        high = xp_model._tail(9.0, 3.0, 12.0)
        assert high > low

    def test_tail_is_monotone_in_the_threshold(self):
        assert xp_model._tail(6.0, 3.0, 5.0) > xp_model._tail(6.0, 3.0, 12.0)

    def test_more_variance_raises_the_ceiling(self):
        """Two players with equal xP: the volatile one hauls more often."""
        steady = xp_model._tail(6.0, 1.0, 12.0)
        volatile = xp_model._tail(6.0, 5.0, 12.0)
        assert volatile > steady, "this is the entire basis of the Sword index"

    def test_zero_variance_is_a_step_function(self):
        assert xp_model._tail(15.0, 0.0, 12.0) == 1.0
        assert xp_model._tail(2.0, 0.0, 12.0) == 0.0

    def test_stays_a_probability(self):
        for mean in (0.0, 5.0, 30.0):
            for sd in (0.1, 3.0, 20.0):
                assert 0.0 <= xp_model._tail(mean, sd, 12.0) <= 1.0
