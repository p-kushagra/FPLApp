"""Bayesian priors: imputation, translation, blending, seeding, xP wiring.

The failure mode this whole module exists to prevent is quiet: two gameweeks
into a season the raw per-90 rates are mostly variance, and a model that
believes them produces confidently wrong transfers. So the tests here pin the
three behaviours that make the prior layer trustworthy -- the promoted-asset
matrix cells hold their calibrated values exactly, the credibility ramp hits
its endpoints (0 minutes = 100% prior, 720+ = 100% current), and an unseeded
database changes nothing at all.
"""
from __future__ import annotations

import pytest

from fpl_assistant import ingest_history
from fpl_assistant.models import priors as priors_mod
from fpl_assistant.models import xp as xp_mod


# ==========================================================================
# Imputation matrix
# ==========================================================================
class TestImputationMatrix:
    def test_promoted_def_cell_is_exact(self):
        for price in (4.0, 4.3, 4.5):
            p = priors_mod.imputed_prior("DEF", price)
            assert (p.npxg90, p.xa90, p.xcs_rate) == (0.02, 0.04, 0.16), price

    def test_promoted_mid_cell_is_exact(self):
        for price in (5.1, 5.5):
            p = priors_mod.imputed_prior("MID", price)
            assert (p.npxg90, p.xa90, p.xcs_rate) == (0.05, 0.08, 0.16), price

    def test_promoted_fwd_cell_is_exact(self):
        for price in (5.6, 6.0):
            p = priors_mod.imputed_prior("FWD", price)
            assert (p.npxg90, p.xa90) == (0.28, 0.12), price

    def test_price_conditions_the_prior(self):
        """An expensive unknown is not priced like a promoted squad body."""
        cheap = priors_mod.imputed_prior("FWD", 5.8)
        premium = priors_mod.imputed_prior("FWD", 9.0)
        assert premium.npxg90 > cheap.npxg90
        assert premium.xa90 > cheap.xa90

    def test_goalkeepers_get_no_attacking_rate(self):
        p = priors_mod.imputed_prior("GKP", 4.5)
        assert p.npxg90 == 0.0
        assert p.xcs_rate > 0

    def test_unknown_position_falls_back_to_mid(self):
        p = priors_mod.imputed_prior("???", 5.2)
        assert p == priors_mod.imputed_prior("MID", 5.2)

    def test_missing_price_uses_the_cheapest_band(self):
        p = priors_mod.imputed_prior("DEF", None)
        assert (p.npxg90, p.xa90) == (0.02, 0.04)

    def test_imputed_priors_carry_provenance(self):
        p = priors_mod.imputed_prior("MID", 5.0)
        assert p.source == priors_mod.SOURCE_IMPUTED
        assert p.minutes == 0.0


# ==========================================================================
# Championship translation
# ==========================================================================
class TestTranslation:
    def test_championship_rates_take_the_haircut(self):
        assert priors_mod.translate(1.0, "CHAMPIONSHIP") == pytest.approx(0.68)

    def test_premier_league_rates_pass_through(self):
        assert priors_mod.translate(0.5, "PL") == 0.5

    def test_missing_competition_means_no_haircut(self):
        assert priors_mod.translate(0.5, None) == 0.5

    def test_case_insensitive(self):
        assert priors_mod.translate(1.0, "Championship") == pytest.approx(0.68)


# ==========================================================================
# The credibility ramp
# ==========================================================================
class TestBlending:
    def test_zero_minutes_is_pure_prior(self):
        assert priors_mod.blend(9.9, 0, 0.3) == pytest.approx(0.3)

    def test_zero_minutes_weight_is_zero(self):
        assert priors_mod.blend_weight(0) == 0.0
        assert priors_mod.blend_weight(None) == 0.0

    def test_midpoint_at_360_minutes(self):
        assert priors_mod.blend_weight(360) == pytest.approx(0.5)
        assert priors_mod.blend(1.0, 360, 0.0) == pytest.approx(0.5)

    def test_full_credibility_at_720_minutes(self):
        assert priors_mod.blend_weight(720) == 1.0
        assert priors_mod.blend(1.0, 720, 99.0) == pytest.approx(1.0)

    def test_weight_never_exceeds_one(self):
        assert priors_mod.blend_weight(3000) == 1.0

    def test_negative_minutes_clamp_to_zero(self):
        assert priors_mod.blend_weight(-90) == 0.0

    def test_formula_matches_hand_calculation(self):
        # 180 of 720 minutes -> w=0.25; 0.25*0.8 + 0.75*0.2 = 0.35
        assert priors_mod.blend(0.8, 180, 0.2) == pytest.approx(0.35)


# ==========================================================================
# Read path over historical_player_baselines
# ==========================================================================
def _baseline(conn, pid, season, source, minutes, npxg=0.3, xa=0.2,
              xcs=0.25, dc=6.0, comp="PL"):
    conn.execute(
        """INSERT OR REPLACE INTO historical_player_baselines
             (player_id, season_name, source, competition, total_minutes,
              npxg90_prior, xa90_prior, xcs_rate_prior, defcon_rate_prior,
              ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'now')""",
        (pid, season, source, comp, minutes, npxg, xa, xcs, dc))


class TestReadPath:
    def test_unseeded_player_returns_none(self, db):
        assert priors_mod.player_prior(db, 1) is None

    def test_unseeded_table_loads_empty(self, db):
        assert priors_mod.load_priors(db) == {}

    def test_pre_v4_database_degrades_to_empty(self, db):
        """A database that never migrated must behave exactly like v2."""
        db.execute("DROP VIEW IF EXISTS pre_gw_projections")
        db.execute("DROP TABLE historical_player_baselines")
        assert priors_mod.load_priors(db) == {}

    def test_latest_qualifying_season_wins(self, db):
        _baseline(db, 1, "2024/25", "fpl_history", 2000, npxg=0.10)
        _baseline(db, 1, "2025/26", "fpl_history", 2000, npxg=0.50)
        assert priors_mod.player_prior(db, 1).npxg90 == pytest.approx(0.50)

    def test_understat_beats_fpl_history_within_a_season(self, db):
        """Understat's npxG excludes penalties; FPL's xG does not."""
        _baseline(db, 1, "2025/26", "fpl_history", 2000, npxg=0.60)
        _baseline(db, 1, "2025/26", "understat", 2000, npxg=0.45)
        assert priors_mod.player_prior(db, 1).npxg90 == pytest.approx(0.45)

    def test_cameo_season_is_not_evidence(self, db):
        """90 minutes last year says the player did not play, not how they play."""
        _baseline(db, 1, "2025/26", "fpl_history", 90, npxg=2.0)
        _baseline(db, 1, "2024/25", "fpl_history", 2500, npxg=0.30)
        assert priors_mod.player_prior(db, 1).npxg90 == pytest.approx(0.30)

    def test_imputed_row_is_the_last_resort(self, db):
        _baseline(db, 1, "2025/26", "fpl_history", 90, npxg=2.0)
        _baseline(db, 1, "imputed", "imputed", 0, npxg=0.05)
        prior = priors_mod.player_prior(db, 1)
        assert prior.source == "imputed"
        assert prior.npxg90 == pytest.approx(0.05)

    def test_championship_row_is_haircut_at_read_time(self, db):
        _baseline(db, 1, "2025/26", "fpl_history", 2000, npxg=1.0, xa=0.5,
                  comp="CHAMPIONSHIP")
        prior = priors_mod.player_prior(db, 1)
        assert prior.npxg90 == pytest.approx(0.68)
        assert prior.xa90 == pytest.approx(0.34)
        # Outcome frequencies (CS, DefCon) are not chance-creation rates and
        # keep their face value.
        assert prior.xcs_rate == pytest.approx(0.25)

    def test_load_priors_matches_single_lookup(self, db):
        _baseline(db, 1, "2025/26", "fpl_history", 2000, npxg=0.4)
        _baseline(db, 2, "imputed", "imputed", 0, npxg=0.05)
        loaded = priors_mod.load_priors(db)
        assert loaded[1] == priors_mod.player_prior(db, 1)
        assert loaded[2] == priors_mod.player_prior(db, 2)


# ==========================================================================
# Seeding
# ==========================================================================
class TestSeedFplHistory:
    def test_per90_arithmetic(self, db):
        report = ingest_history.SeedReport()
        ingest_history.seed_fpl_history(db, 1, [{
            "season_name": "2025/26", "minutes": 1800,
            "expected_goals": 10.0, "expected_assists": 5.0,
            "clean_sheets": 8, "defensive_contribution": 120,
        }], report)
        row = db.execute(
            "SELECT * FROM historical_player_baselines WHERE player_id=1"
        ).fetchone()
        assert row["npxg90_prior"] == pytest.approx(0.5)     # 90*10/1800
        assert row["xa90_prior"] == pytest.approx(0.25)
        assert row["xcs_rate_prior"] == pytest.approx(0.4)   # 90*8/1800
        assert row["defcon_rate_prior"] == pytest.approx(6.0)
        assert row["total_minutes"] == 1800

    def test_only_recent_seasons_are_kept(self, db):
        report = ingest_history.SeedReport()
        seasons = [{"season_name": f"20{20 + i}/{21 + i}", "minutes": 900,
                    "expected_goals": 1, "expected_assists": 1,
                    "clean_sheets": 1} for i in range(6)]
        ingest_history.seed_fpl_history(db, 1, seasons, report)
        n = db.execute("SELECT COUNT(*) c FROM historical_player_baselines"
                       " WHERE player_id=1").fetchone()["c"]
        assert n == ingest_history.MAX_SEASONS

    def test_zero_minute_seasons_are_skipped(self, db):
        report = ingest_history.SeedReport()
        stored = ingest_history.seed_fpl_history(db, 1, [
            {"season_name": "2025/26", "minutes": 0, "expected_goals": 5}],
            report)
        assert stored == 0


class TestSeedUnderstat:
    def test_groups_payload_parses(self, db):
        report = ingest_history.SeedReport()
        ingest_history.seed_understat(db, 1, {"season": [
            {"season": "2025", "time": 2700, "npxG": 15.0, "xA": 6.0},
        ]}, report)
        row = db.execute(
            """SELECT * FROM historical_player_baselines
               WHERE player_id=1 AND source='understat'""").fetchone()
        assert row["season_name"] == "2025/26"
        assert row["npxg90_prior"] == pytest.approx(0.5)
        assert row["xa90_prior"] == pytest.approx(0.2)

    def test_empty_payload_is_a_noop(self, db):
        report = ingest_history.SeedReport()
        assert ingest_history.seed_understat(db, 1, {}, report) == 0


class TestSeedImputed:
    def _player(self, db, pid, etype, cost):
        db.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, team_id, element_type, now_cost, status)
               VALUES (?, ?, 1, ?, ?, 'a')""", (pid, f"P{pid}", etype, cost))

    def test_covers_exactly_the_uncovered(self, db):
        self._player(db, 1, 2, 4.5)   # zero history -> imputed
        self._player(db, 2, 3, 8.0)   # real history -> left alone
        _baseline(db, 2, "2025/26", "fpl_history", 2500)
        report = ingest_history.SeedReport()
        ingest_history.seed_imputed(db, report)
        assert report.imputed_rows == 1
        prior = priors_mod.player_prior(db, 1)
        assert prior.source == "imputed"
        assert (prior.npxg90, prior.xa90) == (0.02, 0.04)

    def test_offline_seed_is_idempotent(self, db):
        self._player(db, 1, 4, 5.8)
        first = ingest_history.seed(db, network=False)
        second = ingest_history.seed(db, network=False)
        assert first.imputed_rows == second.imputed_rows == 1
        n = db.execute(
            "SELECT COUNT(*) c FROM historical_player_baselines").fetchone()["c"]
        assert n == 1


# ==========================================================================
# xP wiring: the blend must actually move projections
# ==========================================================================
def _world(conn, *, gws=(), minutes=90, xg_per_gw=0.0):
    """Two clubs, one fixture next gameweek, one player under test (id 1)."""
    for tid, name in ((1, "Home"), (2, "Away")):
        conn.execute(
            """INSERT OR REPLACE INTO teams
                 (id, name, short_name, strength_attack_home,
                  strength_attack_away, strength_defence_home,
                  strength_defence_away)
               VALUES (?, ?, ?, 1200, 1200, 1200, 1200)""", (tid, name, name[:3]))
    conn.execute(
        """INSERT OR REPLACE INTO players
             (id, web_name, team_id, element_type, position, now_cost, status)
           VALUES (1, 'Subject', 1, 4, 'FWD', 5.8, 'a')""")
    next_gw = (max(gws) if gws else 0) + 1
    conn.execute(
        """INSERT OR REPLACE INTO fixtures
             (id, event, team_h, team_a, team_h_difficulty, team_a_difficulty,
              kickoff_time, finished)
           VALUES (1, ?, 1, 2, 3, 3, '2026-09-12T14:00:00Z', 0)""", (next_gw,))
    for gw in gws:
        conn.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points, goals_scored,
                  assists, clean_sheets, expected_goals, expected_assists,
                  defensive_contribution, saves, bonus, bps, yellow_cards,
                  red_cards)
               VALUES (1, ?, ?, 1, 2, 0, 0, 0, ?, 0.1, 2, 0, 0, 10, 0, 0)""",
            (gw, minutes, xg_per_gw))
    conn.commit()
    return next_gw


class TestXPIntegration:
    def test_zero_history_player_projects_on_pure_prior(self, db):
        """The imputed matrix, not a league average, drives a new signing."""
        gw = _world(db, gws=())
        ingest_history.seed(db, network=False)
        result = xp_mod.project(db, [gw], player_ids=[1], persist=False)
        bd = result[(1, gw)]
        assert bd.goals > 0          # 0.28 npxG90 prior flows through
        assert any("prior" in n for n in bd.notes)

    def test_unseeded_database_keeps_v2_behaviour(self, db):
        gw = _world(db, gws=())
        result = xp_mod.project(db, [gw], player_ids=[1], persist=False)
        assert not any("prior" in n for n in result[(1, gw)].notes)

    def test_hot_start_is_damped_by_a_modest_prior(self, db):
        """N=2 at a 1.5 xG/GW pace must not project as a 1.5 xG/90 player."""
        gw = _world(db, gws=(1, 2), xg_per_gw=1.5)
        _baseline(db, 1, "2025/26", "understat", 2500, npxg=0.30, xa=0.10)
        with_prior = xp_mod.project(db, [gw], player_ids=[1],
                                    persist=False)[(1, gw)]
        db.execute("DELETE FROM historical_player_baselines")
        db.commit()
        without = xp_mod.project(db, [gw], player_ids=[1],
                                 persist=False)[(1, gw)]
        assert with_prior.goals < without.goals

    def test_prior_expires_at_720_minutes(self, db):
        """Two identical current seasons, wildly different priors, no gap."""
        gw = _world(db, gws=tuple(range(1, 9)), xg_per_gw=0.5)  # 8 x 90 = 720
        _baseline(db, 1, "2025/26", "understat", 2500, npxg=5.0, xa=3.0)
        inflated = xp_mod.project(db, [gw], player_ids=[1],
                                  persist=False)[(1, gw)]
        _baseline(db, 1, "2025/26", "understat", 2500, npxg=0.0, xa=0.0)
        deflated = xp_mod.project(db, [gw], player_ids=[1],
                                  persist=False)[(1, gw)]
        assert inflated.goals == pytest.approx(deflated.goals, abs=1e-9)
        assert inflated.assists == pytest.approx(deflated.assists, abs=1e-9)
