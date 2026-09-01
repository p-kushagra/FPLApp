"""Pitch swaps, injury degradation, the scheduler and the schedule service.

The swap tests matter for the same reason the auto-sub tests do: the pitch must
be incapable of proposing a team FPL would reject, and it must reuse the
auto-sub engine's rules rather than reimplementing them.
"""
from __future__ import annotations

import datetime as dt

import pytest

from fpl_assistant import scheduler
from fpl_assistant.models import minutes as minutes_mod
from fpl_assistant.services import schedule as schedule_svc
from fpl_assistant.ui import charts
from fpl_assistant.ui import pitch as pitch_mod


def _squad(defs=4, mids=4, fwds=2):
    """A legal 15: an XI in the requested shape plus a standard bench."""
    players, pid = [], 1
    players.append(pitch_mod.PitchPlayer(pid, "Keeper", "GKP", starting=True))
    pid += 1
    for position, count in (("DEF", defs), ("MID", mids), ("FWD", fwds)):
        for _ in range(count):
            players.append(pitch_mod.PitchPlayer(pid, f"{position}{pid}",
                                                 position, starting=True))
            pid += 1
    bench = [("GKP", 1), ("DEF", 2), ("MID", 3), ("FWD", 4)]
    for position, order in bench[:15 - len(players)]:
        players.append(pitch_mod.PitchPlayer(pid, f"Bench{pid}", position,
                                             starting=False, bench_order=order))
        pid += 1
    return players


# ==========================================================================
# Pitch
# ==========================================================================
class TestPitchModel:
    def test_formation_string(self):
        starters = [p for p in _squad(3, 4, 3) if p.starting]
        assert pitch_mod.formation_string(starters) == "3-4-3"

    def test_layout_places_every_player(self):
        squad = _squad()
        assert len(pitch_mod._layout(squad)) == len(squad)

    def test_starters_and_bench_are_on_different_rows(self):
        placed = pitch_mod._layout(_squad())
        start_y = {y for p, _x, y in placed if p.starting}
        bench_y = {y for p, _x, y in placed if not p.starting}
        assert max(start_y) < min(bench_y)

    def test_captain_label(self):
        player = pitch_mod.PitchPlayer(1, "Salah", "MID", is_captain=True)
        assert player.label.endswith(pitch_mod.CAPTAIN_MARK)

    def test_flagged_player_is_detected(self):
        assert pitch_mod.PitchPlayer(1, "X", "MID", status="i").flagged
        assert pitch_mod.PitchPlayer(2, "Y", "MID", availability=0.5).flagged
        assert not pitch_mod.PitchPlayer(3, "Z", "MID").flagged

    @pytest.mark.skipif(not charts.available(), reason="needs plotly")
    def test_figure_builds(self):
        figure = pitch_mod.figure(_squad())
        assert len(figure.data) == 2          # starters and bench traces

    @pytest.mark.skipif(not charts.available(), reason="needs plotly")
    def test_empty_squad_figure_explains_itself(self):
        assert pitch_mod.figure([]).layout.annotations


class TestSwapValidation:
    def test_legal_outfield_swap(self):
        squad = _squad(4, 4, 2)
        starter = next(p for p in squad if p.starting and p.position == "MID")
        sub = next(p for p in squad if not p.starting and p.position == "MID")
        assert pitch_mod.validate_swap(squad, starter.player_id,
                                       sub.player_id).ok

    def test_keeper_only_swaps_with_keeper(self):
        squad = _squad()
        keeper = next(p for p in squad if p.starting and p.position == "GKP")
        outfield = next(p for p in squad
                        if not p.starting and p.position != "GKP")
        result = pitch_mod.validate_swap(squad, keeper.player_id,
                                         outfield.player_id)
        assert not result.ok
        assert "goalkeeper" in result.reason

    def test_keeper_to_keeper_is_allowed(self):
        squad = _squad()
        starting = next(p for p in squad if p.starting and p.position == "GKP")
        benched = next(p for p in squad
                       if not p.starting and p.position == "GKP")
        assert pitch_mod.validate_swap(squad, starting.player_id,
                                       benched.player_id).ok

    def test_swap_that_breaks_the_floor_is_refused(self):
        """A back three cannot lose a defender for a forward."""
        squad = _squad(3, 4, 3)
        defender = next(p for p in squad if p.starting and p.position == "DEF")
        forward = next(p for p in squad
                       if not p.starting and p.position == "FWD")
        result = pitch_mod.validate_swap(squad, defender.player_id,
                                         forward.player_id)
        assert not result.ok
        assert "formation" in result.reason

    def test_two_starters_cannot_swap(self):
        squad = _squad()
        starters = [p for p in squad if p.starting]
        result = pitch_mod.validate_swap(squad, starters[1].player_id,
                                         starters[2].player_id)
        assert not result.ok
        assert "already starting" in result.reason

    def test_unknown_player_is_refused(self):
        squad = _squad()
        assert not pitch_mod.validate_swap(squad, 9999, squad[0].player_id).ok

    def test_apply_swap_is_non_mutating(self):
        squad = _squad()
        starter = next(p for p in squad if p.starting and p.position == "MID")
        sub = next(p for p in squad if not p.starting and p.position == "MID")
        updated = pitch_mod.apply_swap(squad, starter.player_id, sub.player_id)

        assert starter.starting is True, "original list must not change"
        assert next(p for p in updated
                    if p.player_id == starter.player_id).starting is False
        assert next(p for p in updated
                    if p.player_id == sub.player_id).starting is True

    def test_apply_swap_keeps_eleven_starters(self):
        squad = _squad()
        starter = next(p for p in squad if p.starting and p.position == "MID")
        sub = next(p for p in squad if not p.starting and p.position == "MID")
        updated = pitch_mod.apply_swap(squad, starter.player_id, sub.player_id)
        assert sum(1 for p in updated if p.starting) == 11

    def test_illegal_swap_raises_on_apply(self):
        squad = _squad(3, 4, 3)
        defender = next(p for p in squad if p.starting and p.position == "DEF")
        forward = next(p for p in squad
                       if not p.starting and p.position == "FWD")
        with pytest.raises(ValueError):
            pitch_mod.apply_swap(squad, defender.player_id, forward.player_id)


# ==========================================================================
# Injury parsing and minutes degradation
# ==========================================================================
class TestInjuryParsing:
    TODAY = dt.date(2026, 9, 1)

    def test_suspension_return_date(self):
        assert minutes_mod.parse_return_date(
            "Suspended until 19 Sep", self.TODAY) == dt.date(2026, 9, 19)

    def test_expected_back_phrasing(self):
        assert minutes_mod.parse_return_date(
            "Knee injury - Expected back 15 Oct", self.TODAY) == dt.date(2026, 10, 15)

    def test_year_rolls_forward_for_a_past_month(self):
        """Read in September, 'back 3 Jan' means next January."""
        assert minutes_mod.parse_return_date(
            "Expected back 3 Jan", self.TODAY) == dt.date(2027, 1, 3)

    def test_unknown_return_date(self):
        assert minutes_mod.parse_return_date(
            "Groin injury - Unknown return date", self.TODAY) is None

    def test_empty_news(self):
        assert minutes_mod.parse_return_date(None) is None
        assert minutes_mod.parse_return_date("") is None

    def test_departed_players_are_distinguished(self):
        assert minutes_mod.has_departed(
            {"status": "u", "news": "Has joined Rangers on loan"})
        assert minutes_mod.has_departed(
            {"status": "u", "news": "has departed the club as a free agent."})
        assert not minutes_mod.has_departed(
            {"status": "i", "news": "Knee injury - Unknown return date"})


class TestAvailability:
    TODAY = dt.date(2026, 9, 1)

    def test_available_player_is_ungated(self):
        assert minutes_mod.availability({"status": "a"}) == 1.0

    def test_percentage_is_honoured(self):
        assert minutes_mod.availability(
            {"status": "d", "chance_of_playing_next_round": 75}) == 0.75

    def test_flagged_without_a_percentage_is_a_coin_flip(self):
        assert minutes_mod.availability({"status": "d"}) == 0.5

    def test_injured_with_no_return_date_is_zero(self):
        assert minutes_mod.availability(
            {"status": "i", "news": "Knee injury - Unknown return date"},
            today=self.TODAY) == 0.0

    def test_departed_player_is_always_zero(self):
        assert minutes_mod.availability(
            {"status": "u", "news": "Expected back 20 Aug. Has joined Rangers"},
            today=self.TODAY) == 0.0

    def test_stale_flag_is_discounted_not_cleared(self):
        """FPL is often a day or two late clearing a passed return date."""
        gate = minutes_mod.availability(
            {"status": "i", "news": "Expected back 20 Aug"}, today=self.TODAY)
        assert 0.0 < gate < 1.0

    def test_rotation_risk_suppresses_availability(self):
        base = minutes_mod.availability({"status": "a"}, rotation_score=0.0)
        rotated = minutes_mod.availability({"status": "a"}, rotation_score=6.0)
        assert rotated < base

    def test_return_ramp_eases_a_long_absence(self):
        player = {"news": "Expected back 29 Aug", "news_added": "2026-07-01"}
        assert minutes_mod.return_ramp(player, self.TODAY) < 1.0

    def test_short_absence_needs_no_ramp(self):
        player = {"news": "Expected back 29 Aug", "news_added": "2026-08-25"}
        assert minutes_mod.return_ramp(player, self.TODAY) == 1.0

    def test_ramp_expires_after_the_window(self):
        player = {"news": "Expected back 1 Jun", "news_added": "2026-03-01"}
        assert minutes_mod.return_ramp(player, self.TODAY) == 1.0

    def test_future_return_has_no_ramp(self):
        player = {"news": "Expected back 20 Sep", "news_added": "2026-07-01"}
        assert minutes_mod.return_ramp(player, self.TODAY) == 1.0


class TestAvailabilityAlerts:
    def test_alerts_are_ordered_by_severity(self, db):
        db.execute("INSERT OR REPLACE INTO teams(id, short_name) VALUES (1,'CLB')")
        rows = [(1, "Fit", "a", None, 0), (2, "Doubt", "d", None, 1),
                (3, "Injured", "i", "Knee injury", 1)]
        for pid, name, status, news, starting in rows:
            db.execute(
                """INSERT OR REPLACE INTO players
                     (id, web_name, team_id, element_type, position, status,
                      news, now_cost)
                   VALUES (?, ?, 1, 3, 'MID', ?, ?, 5.0)""",
                (pid, name, status, news))
            db.execute(
                """INSERT OR REPLACE INTO my_picks
                     (gw, player_id, position, multiplier, is_captain, is_vice)
                   VALUES (2, ?, ?, ?, 0, 0)""", (pid, pid, starting))
        db.commit()

        alerts = minutes_mod.availability_alerts(db, 2)
        assert [a["player"] for a in alerts] == ["Injured", "Doubt"]
        assert alerts[0]["severity"] == "critical"

    def test_no_alerts_for_a_clean_squad(self, db):
        assert minutes_mod.availability_alerts(db, 2) == []


# ==========================================================================
# Scheduler
# ==========================================================================
class TestScheduler:
    def teardown_method(self):
        scheduler.shutdown(wait=True)

    def test_apscheduler_is_available(self):
        assert scheduler.available()

    def test_start_registers_the_freeze_job(self, db_path):
        from fpl_assistant import db as db_module
        db_module.init_db(db_path)
        status = scheduler.start(db_path, include_syncs=False)
        assert status.running
        assert any(j["id"] == "freeze_projections" for j in status.jobs)

    def test_start_is_idempotent(self, db_path):
        from fpl_assistant import db as db_module
        db_module.init_db(db_path)
        first = scheduler.start(db_path, include_syncs=False)
        second = scheduler.start(db_path, include_syncs=False)
        assert len(first.jobs) == len(second.jobs) == 1

    def test_shutdown_stops_it(self, db_path):
        from fpl_assistant import db as db_module
        db_module.init_db(db_path)
        scheduler.start(db_path, include_syncs=False)
        scheduler.shutdown(wait=True)
        assert not scheduler.status().running

    def test_unknown_job_is_recorded_not_raised(self, db_path):
        from fpl_assistant import db as db_module
        db_module.init_db(db_path)
        run = scheduler.run_now(db_path, "no_such_job")
        assert run is not None and not run.ok

    def test_failing_job_never_escapes(self, db_path, monkeypatch):
        """A background sync must not be able to kill the foreground app."""
        from fpl_assistant import db as db_module
        from fpl_assistant.jobs import tasks

        db_module.init_db(db_path)

        def explode(conn, **kwargs):
            raise RuntimeError("upstream on fire")

        monkeypatch.setitem(tasks.REGISTRY, "boom", explode)
        run = scheduler.run_now(db_path, "boom")
        assert run is not None and not run.ok
        assert "on fire" in run.detail

    def test_freeze_job_runs_against_a_real_database(self, db_path):
        from fpl_assistant import db as db_module
        db_module.init_db(db_path)
        run = scheduler.run_now(db_path, "freeze_projections")
        assert run is not None and run.ok


# ==========================================================================
# Schedule service
# ==========================================================================
def _fixture_world(db, teams=6, gws=(3, 4, 5)):
    for tid in range(1, teams + 1):
        db.execute(
            "INSERT OR REPLACE INTO teams(id, name, short_name) VALUES (?,?,?)",
            (tid, f"Club {tid}", f"T{tid}"))
    fid = 0
    for gw in gws:
        for home in range(1, teams, 2):
            fid += 1
            db.execute(
                """INSERT OR REPLACE INTO fixtures
                     (id, event, team_h, team_a, team_h_difficulty,
                      team_a_difficulty, kickoff_time, finished)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (fid, gw, home, home + 1, 2 if gw % 2 else 4,
                 4 if gw % 2 else 2,
                 f"2026-09-{10 + gw:02d}T14:00:00Z"))
    db.commit()


class TestScheduleService:
    def test_grid_covers_every_team_and_gameweek(self, db):
        _fixture_world(db)
        rows = schedule_svc.fixture_grid(db, 3, 3)
        assert len(rows) == 6
        assert all(len(r.cells) == 3 for r in rows)

    def test_blank_gameweek_is_marked(self, db):
        _fixture_world(db, gws=(3, 5))
        rows = schedule_svc.fixture_grid(db, 3, 3)
        assert any(c.blank for r in rows for c in r.cells)

    def test_blank_counts_as_hardest_in_the_mean(self, db):
        _fixture_world(db, gws=(3,))
        rows = schedule_svc.fixture_grid(db, 3, 2)
        assert all(r.mean_fdr > 2.0 for r in rows)

    def test_rows_sort_easiest_first(self, db):
        _fixture_world(db)
        rows = schedule_svc.fixture_grid(db, 3, 3)
        assert rows == sorted(rows, key=lambda r: r.mean_fdr)

    def test_rotation_pair_alternates(self, db):
        _fixture_world(db)
        for pid, team in ((1, 1), (2, 2)):
            db.execute(
                """INSERT OR REPLACE INTO players
                     (id, web_name, team_id, element_type, position, now_cost,
                      status, minutes)
                   VALUES (?, ?, ?, 2, 'DEF', 4.2, 'a', 270)""",
                (pid, f"D{pid}", team))
        db.commit()
        pairs = schedule_svc.rotation_pairs(db, 3, 3, positions=("DEF",))
        assert pairs
        assert pairs[0].covered_gws >= 1
        assert pairs[0].combined_cost == pytest.approx(8.4)

    def test_same_club_is_never_a_pair(self, db):
        _fixture_world(db)
        for pid in (1, 2):
            db.execute(
                """INSERT OR REPLACE INTO players
                     (id, web_name, team_id, element_type, position, now_cost,
                      status, minutes)
                   VALUES (?, ?, 1, 2, 'DEF', 4.2, 'a', 270)""",
                (pid, f"D{pid}"))
        db.commit()
        assert schedule_svc.rotation_pairs(db, 3, 3, positions=("DEF",)) == []

    def test_double_gameweek_raises_a_warning(self, db):
        _fixture_world(db, gws=(3,))
        db.execute(
            """INSERT OR REPLACE INTO fixtures
                 (id, event, team_h, team_a, team_h_difficulty,
                  team_a_difficulty, kickoff_time, finished)
               VALUES (900, 3, 1, 4, 3, 3, '2026-09-15T14:00:00Z', 0)""")
        db.commit()
        warnings = schedule_svc.congestion_warnings(db, None, 3, 1)
        assert any(w.matches >= 2 and w.severity == "high" for w in warnings)

    def test_tight_turnaround_is_flagged(self, db):
        _fixture_world(db, gws=(3,))
        db.execute(
            """INSERT OR REPLACE INTO fixtures
                 (id, event, team_h, team_a, team_h_difficulty,
                  team_a_difficulty, kickoff_time, finished)
               VALUES (901, 4, 1, 5, 3, 3, '2026-09-14T14:00:00Z', 0)""")
        db.commit()
        warnings = schedule_svc.congestion_warnings(db, None, 3, 2)
        assert any(w.turnaround_hours is not None
                   and w.turnaround_hours < schedule_svc.TIGHT_TURNAROUND_HOURS
                   for w in warnings)

    def test_horizon_is_clamped(self, db):
        _fixture_world(db)
        assert schedule_svc.build(db, None, horizon=99).horizon == \
            schedule_svc.MAX_HORIZON
        assert schedule_svc.build(db, None, horizon=1).horizon == \
            schedule_svc.MIN_HORIZON

    def test_empty_database_reports_rather_than_raises(self, db):
        vm = schedule_svc.build(db, None)
        assert vm.rows == []
        assert vm.notes


class TestAvailabilityWiringRegression:
    """`availability()` reads `chance_of_playing_next_round` by that exact name.

    Aliasing the column to `chance` in a SELECT made every consumer fall through
    to the generic 'flagged, no percentage' branch: a 50% player and a 75%
    player both scored 0.5, and a 75% doubt on an otherwise-available player
    scored 1.0 and raised no alert at all. Nothing errors when this regresses --
    the numbers are just quietly wrong -- so it is pinned here.
    """

    def _squad_with(self, db, status, chance, starting=1):
        db.execute("INSERT OR REPLACE INTO teams(id, short_name) VALUES (1,'CLB')")
        db.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, team_id, element_type, position, status,
                  chance_of_playing_next_round, news, now_cost)
               VALUES (1, 'Subject', 1, 3, 'MID', ?, ?, 'knock', 5.0)""",
            (status, chance))
        db.execute(
            """INSERT OR REPLACE INTO my_picks
                 (gw, player_id, position, multiplier, is_captain, is_vice)
               VALUES (2, 1, 1, ?, 0, 0)""", (starting,))
        db.commit()

    def test_each_fpl_percentage_survives_the_query(self, db):
        for chance, expected in ((0, 0.0), (25, 0.25), (50, 0.5), (75, 0.75)):
            self._squad_with(db, "d", chance)
            alerts = minutes_mod.availability_alerts(db, 2)
            assert alerts, f"{chance}% must raise an alert"
            assert alerts[0]["availability"] == pytest.approx(expected), chance
            assert alerts[0]["chance"] == chance

    def test_seventy_five_percent_is_a_doubt_not_a_coin_flip(self, db):
        self._squad_with(db, "d", 75)
        assert minutes_mod.availability_alerts(db, 2)[0]["severity"] == "doubt"

    def test_fifty_percent_is_high_severity(self, db):
        self._squad_with(db, "d", 50)
        assert minutes_mod.availability_alerts(db, 2)[0]["severity"] == "high"

    def test_pitch_loader_sees_the_percentage(self, db):
        self._squad_with(db, "d", 25)
        squad = pitch_mod.load_squad(db, 2)
        assert squad[0].availability == pytest.approx(0.25)
        assert squad[0].flagged

    def test_briefing_flag_reflects_the_percentage(self, db):
        from fpl_assistant.services import briefing as briefing_svc
        self._squad_with(db, "d", 25)
        brief = briefing_svc.build(db, None, 3, squad_gw=2)
        entry = (brief.starting_xi + brief.bench)[0]
        assert entry.flag == "25%"
