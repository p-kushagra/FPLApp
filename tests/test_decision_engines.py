"""Role arbitrage, price momentum, template/differentials and Monte Carlo.

These four modules produce the recommendations the operator acts on, so the
tests pin the *claims* rather than the plumbing: a defender is only tagged an
attacking wing-back when he really is crossing, a price velocity really is
normalised by ownership, a differential really has cleared all three gates, and
10,000 simulations really do finish inside the performance budget.
"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from fpl_assistant import price_predictor as pp
from fpl_assistant.models import arbitrage, stochastic, template


# ==========================================================================
# Fixtures
# ==========================================================================
def _team(db, tid=1, short="CLB"):
    db.execute(
        """INSERT OR REPLACE INTO teams
             (id, name, short_name, strength_attack_home, strength_attack_away,
              strength_defence_home, strength_defence_away)
           VALUES (?, ?, ?, 1200, 1150, 1200, 1150)""",
        (tid, f"Club {tid}", short))


def _player(db, pid, position, *, cost=5.0, team=1, own=5.0, status="a",
            pens=None, corners=None, freekicks=None, minutes=180):
    etype = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}[position]
    db.execute(
        """INSERT OR REPLACE INTO players
             (id, web_name, team_id, element_type, position, now_cost,
              selected_by_percent, status, minutes, penalties_order,
              corners_order, freekicks_order, transfers_in_event,
              transfers_out_event)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
        (pid, f"P{pid}", team, etype, position, cost, own, status, minutes,
         pens, corners, freekicks))


def _gw(db, pid, gw, *, minutes=90, threat=0.0, creativity=0.0, xgi=0.0,
        xg=0.0, cbi=0.0, points=2):
    db.execute(
        """INSERT OR REPLACE INTO player_gw
             (player_id, gw, minutes, starts, total_points, threat, creativity,
              expected_goal_involvements, expected_goals, expected_assists,
              clearances_blocks_interceptions, defensive_contribution, bps,
              bonus, saves, clean_sheets, goals_scored, assists)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 0, ?, 0, 20, 0, 0, 0, 0, 0)""",
        (pid, gw, minutes, points, threat, creativity, xgi, xg, cbi))


@pytest.fixture
def league(db):
    """Twelve typical players per position, so medians are meaningful."""
    for tid in range(1, 7):
        _team(db, tid, f"T{tid}")

    pid = 100
    # Ordinary players: DEF defend, MID create, FWD shoot.
    for position, threat, creativity, xgi, xg, cbi in (
            ("DEF", 8.0, 6.0, 0.05, 0.03, 12.0),
            ("MID", 20.0, 20.0, 0.20, 0.12, 4.0),
            ("FWD", 45.0, 12.0, 0.45, 0.35, 1.5)):
        for _ in range(12):
            _player(db, pid, position, team=(pid % 6) + 1)
            for gw in (1, 2):
                _gw(db, pid, gw, threat=threat, creativity=creativity,
                    xgi=xgi, xg=xg, cbi=cbi)
            pid += 1
    db.commit()
    return db


# ==========================================================================
# Role arbitrage
# ==========================================================================
class TestArbitrageDetection:
    def test_ordinary_players_are_not_flagged(self, league):
        profile = arbitrage.role_profile(league, 100)
        assert profile.role == arbitrage.ROLE_AS_LISTED
        assert not profile.is_arbitrage

    def test_midfielder_playing_as_striker_is_tagged(self, league):
        """Forward-grade shot volume with a midfielder's listing."""
        _player(league, 1, "MID", cost=6.0)
        for gw in (1, 2):
            _gw(league, 1, gw, threat=70.0, creativity=10.0, xgi=0.60,
                xg=0.50, cbi=1.0)
        league.commit()

        profile = arbitrage.role_profile(league, 1)
        assert profile.oop_striker
        assert arbitrage.BADGE_OOP_STRIKER in profile.badges
        assert profile.premium_per90 > 0
        assert profile.compared_to == "FWD"

    def test_inside_forward_is_distinguished_from_striker(self, league):
        """High involvement, ordinary shot volume: creator, not a nine."""
        _player(league, 2, "MID", cost=7.0)
        for gw in (1, 2):
            _gw(league, 2, gw, threat=60.0, creativity=55.0, xgi=0.55,
                xg=0.14, cbi=1.0)
        league.commit()

        profile = arbitrage.role_profile(league, 2)
        assert profile.oop_inside_forward
        assert not profile.oop_striker
        assert arbitrage.BADGE_OOP_INSIDE in profile.badges

    def test_attacking_wingback_needs_threat_and_crossing(self, league):
        _player(league, 3, "DEF", cost=4.5)
        for gw in (1, 2):
            _gw(league, 3, gw, threat=40.0, creativity=45.0, xgi=0.30,
                xg=0.12, cbi=3.0)
        league.commit()

        profile = arbitrage.role_profile(league, 3)
        assert profile.attacking_wingback
        assert arbitrage.BADGE_WINGBACK in profile.badges

    def test_set_piece_centre_back_is_not_a_wingback(self, league):
        """Box threat without crossing volume is a corner specialist."""
        _player(league, 4, "DEF", cost=4.5)
        for gw in (1, 2):
            _gw(league, 4, gw, threat=40.0, creativity=2.0, xgi=0.30,
                xg=0.25, cbi=3.0)
        league.commit()

        profile = arbitrage.role_profile(league, 4)
        assert not profile.attacking_wingback
        assert any("wing-back" in n for n in profile.notes)

    def test_defensive_workload_vetoes_the_tag(self, league):
        """Attacking output alone is not enough -- a CB who scored twice."""
        _player(league, 5, "DEF", cost=4.5)
        for gw in (1, 2):
            _gw(league, 5, gw, threat=40.0, creativity=45.0, xgi=0.30,
                xg=0.12, cbi=30.0)          # still defending heavily
        league.commit()
        assert not arbitrage.role_profile(league, 5).is_arbitrage

    def test_deeper_role_is_detected(self, league):
        _player(league, 6, "MID", cost=5.0)
        for gw in (1, 2):
            _gw(league, 6, gw, threat=1.0, creativity=1.0, xgi=0.01,
                xg=0.0, cbi=25.0)
        league.commit()
        assert arbitrage.role_profile(league, 6).role == arbitrage.ROLE_DEEPER

    def test_cameo_minutes_are_not_evidence(self, league):
        _player(league, 7, "MID", cost=5.0)
        _gw(league, 7, 1, minutes=20, threat=90.0, xgi=0.9, xg=0.8)
        league.commit()
        assert arbitrage.role_profile(league, 7).sample == 0


class TestSetPieceBadges:
    def test_penalty_taker_is_tagged(self, league):
        _player(league, 8, "MID", pens=1)
        _gw(league, 8, 1, threat=20.0, creativity=20.0, xgi=0.2)
        league.commit()
        assert arbitrage.BADGE_PENALTIES in arbitrage.role_profile(league, 8).badges

    def test_corner_taker_is_tagged(self, league):
        _player(league, 9, "MID", corners=1)
        _gw(league, 9, 1, threat=20.0, creativity=20.0, xgi=0.2)
        league.commit()
        assert arbitrage.BADGE_CORNERS in arbitrage.role_profile(league, 9).badges

    def test_third_choice_free_kicks_is_not_tagged(self, league):
        _player(league, 10, "MID", freekicks=5)
        _gw(league, 10, 1, threat=20.0, creativity=20.0, xgi=0.2)
        league.commit()
        badges = arbitrage.role_profile(league, 10).badges
        assert arbitrage.BADGE_FREEKICKS not in badges

    def test_badges_available_without_any_minutes(self, league):
        """A new signing on penalties is knowable before he has played."""
        _player(league, 11, "FWD", pens=1, corners=1, minutes=0)
        league.commit()
        badges = arbitrage.badges_for(league, [11])[11]
        assert arbitrage.BADGE_PENALTIES in badges
        assert arbitrage.BADGE_CORNERS in badges

    def test_missing_player_yields_no_badges(self, league):
        assert arbitrage.badges_for(league, [99999]) == {99999: []}

    def test_empty_request(self, league):
        assert arbitrage.badges_for(league, []) == {}


class TestArbitrageRanking:
    def test_penalty_duty_outranks_an_equivalent_player(self, league):
        base = arbitrage.RoleProfile(player_id=1, cost=5.0, attack_ratio=3.0)
        with_pens = arbitrage.RoleProfile(player_id=2, cost=5.0,
                                          attack_ratio=3.0, on_penalties=True)
        assert arbitrage.score(with_pens) > arbitrage.score(base)

    def test_cheaper_is_better_all_else_equal(self, league):
        cheap = arbitrage.RoleProfile(player_id=1, cost=4.0, attack_ratio=3.0)
        dear = arbitrage.RoleProfile(player_id=2, cost=6.0, attack_ratio=3.0)
        assert arbitrage.score(cheap) > arbitrage.score(dear)

    def test_high_ownership_erodes_the_edge(self, league):
        rare = arbitrage.RoleProfile(player_id=1, cost=5.0, attack_ratio=3.0,
                                     ownership=1.0)
        common = arbitrage.RoleProfile(player_id=2, cost=5.0, attack_ratio=3.0,
                                       ownership=45.0)
        assert arbitrage.score(rare) > arbitrage.score(common)

    def test_candidates_returns_sorted_profiles(self, league):
        _player(league, 12, "DEF", cost=4.0, pens=1)
        for gw in (1, 2):
            _gw(league, 12, gw, threat=40.0, creativity=45.0, xgi=0.3, cbi=2.0)
        league.commit()
        found = arbitrage.candidates(league, limit=10)
        scores = [arbitrage.score(p) for p in found]
        assert scores == sorted(scores, reverse=True)

    def test_empty_database_returns_nothing(self, db):
        assert arbitrage.candidates(db) == []


# ==========================================================================
# Price momentum
# ==========================================================================
class TestVelocity:
    def test_matches_the_stated_formula(self):
        # (in - out) / ownership
        assert pp.velocity(100_000, 20_000, 10.0) == pytest.approx(8_000.0)

    def test_sign_follows_net_flow(self):
        assert pp.velocity(10, 1_000, 5.0) < 0
        assert pp.velocity(1_000, 10, 5.0) > 0

    def test_low_ownership_is_floored(self):
        """A 0.01%-owned player must not manufacture an infinite velocity."""
        tiny = pp.velocity(1_000, 0, 0.01)
        floored = pp.velocity(1_000, 0, pp.MIN_OWNERSHIP)
        assert tiny == pytest.approx(floored)

    def test_zero_ownership_does_not_divide_by_zero(self):
        assert pp.velocity(500, 0, 0.0) == pytest.approx(500 / pp.MIN_OWNERSHIP)

    def test_same_net_flow_ranks_by_ownership(self):
        """Identical net transfers matter more for a less-owned player."""
        assert pp.velocity(50_000, 0, 2.0) > pp.velocity(50_000, 0, 40.0)


class TestPriceForecast:
    def _seed(self, db, tin, tout, own=10.0):
        _team(db)
        db.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, team_id, element_type, position, now_cost,
                  selected_by_percent, transfers_in_event, transfers_out_event,
                  status)
               VALUES (1, 'Subject', 1, 3, 'MID', 7.0, ?, ?, ?, 'a')""",
            (own, tin, tout))
        db.commit()

    def test_strong_inflow_predicts_a_rise(self, db):
        self._seed(db, 200_000, 10_000)
        f = pp.forecast(db, persist=False)[0]
        assert f.direction == pp.DIRECTION_RISE
        assert f.p_rise > 0.5

    def test_strong_outflow_predicts_a_fall(self, db):
        self._seed(db, 5_000, 200_000)
        f = pp.forecast(db, persist=False)[0]
        assert f.direction == pp.DIRECTION_FALL
        assert f.p_fall > 0.5

    def test_no_flow_holds(self, db):
        self._seed(db, 0, 0)
        f = pp.forecast(db, persist=False)[0]
        assert f.direction == pp.DIRECTION_HOLD
        assert f.basis == pp.BASIS_NONE

    def test_single_snapshot_is_labelled_as_a_total(self, db):
        self._seed(db, 100_000, 0)
        f = pp.forecast(db, persist=False)[0]
        assert f.basis == pp.BASIS_EVENT_TOTAL
        assert f.confidence != "high"
        assert any("single snapshot" in n for n in f.notes)

    def test_recent_change_locks_the_player(self, db):
        self._seed(db, 300_000, 0)
        now = dt.datetime.now(dt.timezone.utc)
        db.execute(
            """INSERT INTO price_change
                 (player_id, changed_at, old_cost, new_cost, direction)
               VALUES (1, ?, 6.9, 7.0, 1)""",
            ((now - dt.timedelta(hours=3)).isoformat(),))
        db.commit()
        f = pp.forecast(db, now=now, persist=False)[0]
        assert f.locked
        assert f.direction == pp.DIRECTION_HOLD

    def test_forecast_persists(self, db):
        self._seed(db, 150_000, 0)
        pp.forecast(db, persist=True)
        assert db.execute(
            "SELECT COUNT(*) c FROM price_prediction").fetchone()["c"] == 1

    def test_hours_to_change_is_always_ahead(self):
        for hour in (0, 1, 2, 13, 23):
            now = dt.datetime(2026, 9, 1, hour, 15, tzinfo=dt.timezone.utc)
            assert 0 < pp.hours_to_next_change(now) <= 24


class TestTicker:
    def test_empty_database(self, db):
        assert pp.ticker(db).rising == []

    def test_owned_lists_are_filtered_to_the_squad(self, db):
        _team(db)
        for pid, tin in ((1, 300_000), (2, 250_000)):
            db.execute(
                """INSERT OR REPLACE INTO players
                     (id, web_name, team_id, element_type, position, now_cost,
                      selected_by_percent, transfers_in_event,
                      transfers_out_event, status)
                   VALUES (?, ?, 1, 3, 'MID', 7.0, 10.0, ?, 0, 'a')""",
                (pid, f"P{pid}", tin))
        db.commit()
        ticker = pp.ticker(db, squad=[1])
        assert {f.player_id for f in ticker.owned_rising} == {1}
        assert len(ticker.rising) == 2

    def test_migration_buckets_by_price(self, db):
        _team(db)
        for pid, cost in ((1, 4.5), (2, 6.5), (3, 9.5), (4, 13.0)):
            db.execute(
                """INSERT OR REPLACE INTO players
                     (id, web_name, team_id, element_type, position, now_cost,
                      selected_by_percent, transfers_in_event,
                      transfers_out_event, status)
                   VALUES (?, ?, 1, 3, 'MID', ?, 10.0, 1000, 0, 'a')""",
                (pid, f"P{pid}", cost))
        db.commit()
        brackets = {m["bracket"] for m in pp.ticker(db).migration}
        assert len(brackets) == 4


# ==========================================================================
# Template and differentials
# ==========================================================================
class TestTemplate:
    def _own(self, db, pid, pct, gw=2, sample=100):
        db.execute(
            """INSERT OR REPLACE INTO top_owned
                 (gw, player_id, ownership_pct, captain_pct, sample_size)
               VALUES (?, ?, ?, 0, ?)""", (gw, pid, pct, sample))

    def test_high_ownership_becomes_template_core(self, league):
        self._own(league, 100, 82.0)
        league.commit()
        report = template.build(league, 2)
        assert any(a.player_id == 100 for a in report.core)
        assert report.basis == template.BASIS_TOP_SAMPLE

    def test_coverage_reflects_what_you_own(self, league):
        self._own(league, 100, 82.0)
        self._own(league, 101, 75.0)
        league.commit()
        assert template.build(league, 2, squad=[100]).coverage == 0.5
        assert template.build(league, 2, squad=[100, 101]).coverage == 1.0

    def test_missing_template_asset_is_a_gap(self, league):
        self._own(league, 100, 88.0)
        league.commit()
        gaps = template.build(league, 2, squad=[]).gaps
        assert gaps and gaps[0].risk == "critical gap"

    def test_sample_falls_back_to_the_latest_gameweek(self, league):
        """Planning happens for a gameweek that has not been sampled yet."""
        self._own(league, 100, 82.0, gw=2)
        league.commit()
        assert template.build(league, 5).basis == template.BASIS_TOP_SAMPLE

    def test_global_ownership_is_the_documented_fallback(self, league):
        report = template.build(league, 2)
        assert report.basis == template.BASIS_GLOBAL
        assert report.basis_caveat is not None

    def test_differential_must_clear_every_gate(self, league):
        _player(league, 50, "MID", cost=5.5, own=3.0, team=1)
        for gw in (1, 2):
            _gw(league, 50, gw, threat=40.0, creativity=30.0, xgi=0.9, xg=0.5)
        # An easy run for team 1 so the fixture gate passes.
        for fid, gw in enumerate(range(3, 6), start=500):
            league.execute(
                """INSERT OR REPLACE INTO fixtures
                     (id, event, team_h, team_a, team_h_difficulty,
                      team_a_difficulty, finished)
                   VALUES (?, ?, 1, 6, 2, 2, 0)""", (fid, gw))
        league.commit()
        report = template.build(league, 2)
        assert any(d.player_id == 50 for d in report.differentials)

    def test_popular_player_is_never_a_differential(self, league):
        _player(league, 51, "MID", cost=5.5, own=45.0)
        for gw in (1, 2):
            _gw(league, 51, gw, threat=40.0, xgi=0.9, xg=0.5)
        league.commit()
        report = template.build(league, 2)
        assert not any(d.player_id == 51 for d in report.differentials)

    def test_hard_fixtures_veto_a_differential(self, league):
        _player(league, 52, "MID", cost=5.5, own=2.0, team=2)
        for gw in (1, 2):
            _gw(league, 52, gw, threat=40.0, xgi=0.9, xg=0.5)
        for fid, gw in enumerate(range(3, 6), start=600):
            league.execute(
                """INSERT OR REPLACE INTO fixtures
                     (id, event, team_h, team_a, team_h_difficulty,
                      team_a_difficulty, finished)
                   VALUES (?, ?, 2, 6, 5, 5, 0)""", (fid, gw))
        league.commit()
        report = template.build(league, 2)
        assert not any(d.player_id == 52 for d in report.differentials)

    def test_funnel_names_the_binding_gate(self, league):
        report = template.build(league, 2, max_fdr=0.5)   # impossible to pass
        assert report.differentials == []
        assert report.funnel["fdr"] == 0
        assert "fixture-difficulty" in (report.binding_gate or "")

    def test_percentile_helpers(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert template.percentile_threshold(values, 0) == 1.0
        assert template.percentile_threshold(values, 100) == 5.0
        assert template.percentile_threshold(values, 50) == 3.0
        assert template.percentile_of(values, 3.0) == 40.0

    def test_blank_fixtures_score_as_the_worst_case(self, league):
        fdr, label = template.fixture_outlook(league, 1, 90, horizon=3)
        assert fdr == 5.0
        assert "blank" in label


# ==========================================================================
# Monte Carlo
# ==========================================================================
def _sim_world(db):
    _team(db, 1, "AAA")
    _team(db, 2, "BBB")
    _player(db, 1, "FWD", cost=8.0)
    for gw in (1, 2):
        _gw(db, 1, gw, threat=45.0, xgi=0.6, xg=0.5, points=6)
    db.execute(
        """INSERT OR REPLACE INTO fixtures
             (id, event, team_h, team_a, team_h_difficulty, team_a_difficulty,
              kickoff_time, finished)
           VALUES (1, 3, 1, 2, 3, 3, '2026-09-12T14:00:00Z', 0)""")
    db.commit()


class TestMonteCarlo:
    def test_ten_thousand_runs_inside_the_budget(self, db):
        """Performance gate: 10k runs must complete in under two seconds."""
        _sim_world(db)
        start = time.perf_counter()
        stochastic.simulate(db, 3, [1], runs=10_000)
        assert time.perf_counter() - start < 2.0

    def test_distribution_is_ordered(self, db):
        _sim_world(db)
        d = stochastic.simulate(db, 3, [1], runs=5_000)[1]
        assert d.floor <= d.median <= d.ceiling
        assert d.floor <= d.mean <= d.ceiling

    def test_mean_tracks_the_deterministic_model(self, db):
        """The simulation samples the xP model; it is not a second opinion."""
        _sim_world(db)
        d = stochastic.simulate(db, 3, [1], runs=10_000)[1]
        assert d.mean == pytest.approx(d.xp_reference, abs=1.0)

    def test_identical_seeds_reproduce(self, db):
        _sim_world(db)
        first = stochastic.simulate(db, 3, [1], runs=2_000, seed=7)[1]
        second = stochastic.simulate(db, 3, [1], runs=2_000, seed=7)[1]
        assert first.mean == second.mean
        assert first.p_haul == second.p_haul

    def test_probabilities_are_valid(self, db):
        _sim_world(db)
        d = stochastic.simulate(db, 3, [1], runs=5_000)[1]
        for value in (d.p_haul, d.p_blank, d.p_return):
            assert 0.0 <= value <= 1.0

    def test_blank_gameweek_scores_nothing(self, db):
        _sim_world(db)
        d = stochastic.simulate(db, 9, [1], runs=500)[1]
        assert d.mean == 0.0
        assert "blank" in " ".join(d.notes)

    def test_points_are_never_negative(self, db):
        _sim_world(db)
        assert stochastic.simulate(db, 3, [1], runs=5_000)[1].floor >= 0.0

    def test_profile_classifies_spread(self):
        steady = stochastic.Distribution(player_id=1, gw=1, mean=5.0,
                                         floor=4.0, ceiling=6.0)
        explosive = stochastic.Distribution(player_id=2, gw=1, mean=5.0,
                                            floor=0.0, ceiling=16.0)
        assert steady.profile == "steady"
        assert explosive.profile == "explosive"

    def test_squad_aggregate_sums_players(self, db):
        _sim_world(db)
        _player(db, 2, "MID", cost=6.0, team=1)
        for gw in (1, 2):
            _gw(db, 2, gw, threat=20.0, xgi=0.25, xg=0.15)
        db.commit()
        squad = stochastic.simulate_squad(db, 3, [1, 2], runs=2_000)
        # The aggregate is rounded to one decimal, the parts to two.
        assert squad.mean == pytest.approx(
            sum(d.mean for d in squad.players), abs=0.05)
        assert squad.notes                       # correlation caveat is stated

    def test_captain_multiplier_is_applied(self, db):
        _sim_world(db)
        plain = stochastic.simulate_squad(db, 3, [1], runs=2_000)
        doubled = stochastic.simulate_squad(db, 3, [1], runs=2_000,
                                            multipliers={1: 2.0})
        assert doubled.mean == pytest.approx(plain.mean * 2, abs=0.01)

    def test_scenario_branches_include_the_schedule(self, db):
        _sim_world(db)
        branches = stochastic.scenario_branches(db, 3, [1], runs=500)
        assert branches[0].label == "Scheduled"
