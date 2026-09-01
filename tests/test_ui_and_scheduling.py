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


class TestShotMarkerScale:
    """The marker scale is the whole encoding, so it is pinned, not eyeballed.

    A shot map's only quantitative channel is marker area. These tests fix the
    three properties that make it readable: area tracks xG, the scale is
    absolute rather than per-figure, and the largest mark stays small enough
    that a cluster of big chances does not merge into one blob -- which is
    exactly what a 70px-per-root-xG scale used to produce in front of goal.
    """

    def test_area_is_proportional_to_xg(self):
        """Doubling xG doubles the ink. Diameter must go as sqrt(xG)."""
        for low in (0.1, 0.2, 0.4):   # all clear of the 8px floor
            big = charts._shot_marker_px(low * 2) ** 2
            small = charts._shot_marker_px(low) ** 2
            assert big / small == pytest.approx(2.0, rel=0.02), (
                f"area ratio wrong at xG={low}: sizing by diameter instead of "
                "area overstates a big chance roughly fourfold")

    def test_the_floor_compresses_only_speculative_chances(self):
        """The >=8px floor costs gradation at the bottom. Bound that cost.

        Below the floor every shot renders identically, so the floor must sit
        low enough that it only flattens shots nobody ranks against each
        other -- a 0.02 and a 0.05 are both "hopeful". A floor reaching into
        real chances would hide the thing the chart is for.
        """
        floor_xg = (charts.SHOT_MIN_PX / charts.SHOT_MAX_PX) ** 2
        assert floor_xg < 0.12, (
            f"the 8px floor flattens everything below {floor_xg:.3f} xG, "
            "which is reaching into genuine chances")
        # Anything a manager would call a chance still ranks by size.
        assert (charts._shot_marker_px(0.25)
                < charts._shot_marker_px(0.5)
                < charts._shot_marker_px(0.75))

    def test_scale_is_absolute_not_per_figure(self):
        """The same shot is the same size on every player's map.

        Normalising to the selected player's own maximum would render a
        defender's best header at the same size as a striker's tap-in, and
        the cross-player comparison is the reason the chart exists.
        """
        penalty = charts._shot_marker_px(0.76)
        assert penalty == pytest.approx((0.76 ** 0.5) * charts.SHOT_MAX_PX)
        # No argument exists through which other shots could influence it.
        assert charts._shot_marker_px(0.76) == penalty

    def test_largest_realistic_shot_cannot_swamp_the_box(self):
        """A certain goal is the anchor and is still a small mark."""
        assert charts._shot_marker_px(1.0) == pytest.approx(charts.SHOT_MAX_PX)
        assert charts.SHOT_MAX_PX <= 30, (
            "the six-yard box is ~55px tall on this figure; a marker above "
            "~30px turns a cluster of chances into one blob")
        # An xG above the anchor is theoretically impossible, but must not
        # blow up the figure if a source ever reports one.
        assert charts._shot_marker_px(1.5) < 2 * charts.SHOT_MAX_PX

    def test_smallest_shot_stays_a_visible_hit_target(self):
        assert charts._shot_marker_px(0.0) == charts.SHOT_MIN_PX
        assert charts._shot_marker_px(0.001) >= 8.0

    @pytest.mark.skipif(not charts.available(), reason="needs plotly")
    def test_a_goal_and_a_miss_of_equal_xg_are_the_same_size(self):
        """Outcome is encoded once, in colour and symbol -- never in area."""
        fig = charts.shot_map([
            charts.Shot(x=0.9, y=0.5, xg=0.4, result="Goal"),
            charts.Shot(x=0.8, y=0.4, xg=0.4, result="MissedShots"),
        ])
        sizes = [trace.marker.size[0] for trace in fig.data]
        assert len(sizes) == 2 and sizes[0] == sizes[1]

    @pytest.mark.skipif(not charts.available(), reason="needs plotly")
    def test_own_goal_is_not_a_goal_and_is_not_mapped(self):
        """Understat files own goals under the scorer, at 0.00 xG.

        Counted as a goal it adds +1 against no xG -- the Goals - xG tile then
        reports elite finishing for putting one in your own net. It also sits
        ~100m from the goal being drawn.
        """
        own = charts.Shot(x=0.04, y=0.5, xg=0.0, result="OwnGoal")
        assert own.is_own_goal and not own.is_goal

        real = charts.Shot(x=0.93, y=0.5, xg=0.4, result="Goal")
        fig = charts.shot_map([real, own])
        plotted = sum(len(trace.x) for trace in fig.data)
        assert plotted == 1, "the own goal was drawn on the map"
        assert "1 shots" in fig.layout.title.text

    @pytest.mark.skipif(not charts.available(), reason="needs plotly")
    def test_subtitle_uses_a_real_separator_not_an_html_entity(self):
        """Plotly renders a small HTML subset and does NOT decode entities.

        `&middot;` reached the screen verbatim as "11 shots &middot; 2.25 xG".
        """
        fig = charts.shot_map([charts.Shot(x=0.9, y=0.5, xg=0.4)])
        assert "&middot;" not in fig.layout.title.text
        assert "·" in fig.layout.title.text


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


@pytest.mark.skipif(not charts.available(), reason="needs plotly")
class TestShotMapGeometry:
    """The pitch must be the right shape in any container, and stay put.

    The figure previously locked its aspect with `scaleanchor` alone. Plotly's
    default for that is `constrain="range"` -- it honours the ratio by WIDENING
    the range until the figure fills its container -- so the requested range was
    only a floor and the same code drew a tall strip in a narrow column and a
    stretched landscape in a wide one, pitch adrift in the middle of both.
    """

    def _fig(self, *shots):
        return charts.shot_map(list(shots) or [charts.Shot(0.9, 0.5, 0.3)])

    def test_aspect_is_constrained_by_domain_not_range(self):
        fig = self._fig()
        assert fig.layout.yaxis.scaleanchor == "x"
        assert fig.layout.yaxis.scaleratio == 1
        assert fig.layout.xaxis.constrain == "domain"
        assert fig.layout.yaxis.constrain == "domain", (
            'without constrain="domain" plotly widens the range to fill the '
            "container and the pitch stops being pitch-shaped")

    def test_axes_are_in_metres_so_the_1_to_1_lock_is_truthful(self):
        """A 1:1 lock is only correct if both axes are the same unit.

        Understat's raw units are anisotropic -- 1 x-unit is 105m and 1 y-unit
        is 68m -- so locking those 1:1 would squash the pitch by a third.
        """
        shot = charts.Shot(x=1.0, y=1.0, xg=0.1)
        assert shot.across_m == pytest.approx(charts.PITCH_WIDTH_M)
        assert shot.upfield_m == pytest.approx(charts.PITCH_LENGTH_M)

    def test_goal_is_a_horizontal_segment_at_the_top(self):
        fig = self._fig()
        horizontals = [s for s in fig.layout.shapes
                       if s.type == "line" and s.y0 == s.y1]
        goal = [s for s in horizontals
                if s.x1 - s.x0 == pytest.approx(charts.GOAL_WIDTH_M)]
        assert len(goal) == 1, "expected one goal-width horizontal segment"
        assert goal[0].y0 == pytest.approx(charts.PITCH_LENGTH_M)
        # ...at the TOP of the drawn range, not the bottom.
        low, high = fig.layout.yaxis.range
        assert abs(high - goal[0].y0) < abs(goal[0].y0 - low)
        # The goal is the one mark drawn at full strength.
        assert all(g.line.width >= s.line.width
                   for g in goal for s in horizontals)

    def test_the_crop_is_not_outlined_as_if_it_were_a_touchline(self):
        """Only real pitch lines get drawn.

        The view is a crop, so its left, right and back edges are not lines on
        a pitch. Outlining them invites the reader to read the nearest edge as
        a touchline and misjudge every angle measured from it.
        """
        fig = self._fig()
        rects = [s for s in fig.layout.shapes if s.type == "rect"]
        widths = {round(r.x1 - r.x0, 2) for r in rects}
        assert widths == {charts.PEN_AREA_WIDTH_M, charts.SIX_YARD_WIDTH_M}, (
            "an outline was drawn around the cropped view")

    def test_markings_match_the_laws_of_the_game(self):
        rects = [s for s in self._fig().layout.shapes if s.type == "rect"]
        boxes = {round(r.x1 - r.x0, 2): round(r.y1 - r.y0, 2) for r in rects}
        assert boxes[charts.PEN_AREA_WIDTH_M] == charts.PEN_AREA_DEPTH_M
        assert boxes[charts.SIX_YARD_WIDTH_M] == charts.SIX_YARD_DEPTH_M

    def test_frame_is_identical_for_every_player(self):
        """Comparability: one long-ranger must not rescale the goalmouth."""
        close = self._fig(charts.Shot(0.95, 0.5, 0.4))
        far = self._fig(charts.Shot(0.95, 0.5, 0.4),
                        charts.Shot(0.55, 0.5, 0.01))
        assert close.layout.yaxis.range == far.layout.yaxis.range
        assert close.layout.xaxis.range == far.layout.xaxis.range

    def test_shots_outside_the_frame_are_declared_not_dropped(self):
        fig = self._fig(charts.Shot(0.95, 0.5, 0.4),
                        charts.Shot(0.50, 0.5, 0.01))
        assert "outside the view" in fig.layout.title.text
        assert "2 shots" in fig.layout.title.text, "still counted in the total"

    def test_a_wide_shot_is_declared_too_not_just_a_distant_one(self):
        """The crop is in both directions, so the count must be too."""
        wide = charts.Shot(x=0.95, y=0.02, xg=0.05)   # near the touchline
        fig = self._fig(charts.Shot(0.95, 0.5, 0.4), wide)
        assert "outside the view" in fig.layout.title.text

    def test_zoom_and_pan_are_disabled(self):
        """A pitch has one correct framing; zoom only offers ways to break it."""
        fig = self._fig()
        assert fig.layout.xaxis.fixedrange is True
        assert fig.layout.yaxis.fixedrange is True

    def test_ordinary_shots_all_land_inside_the_frame(self):
        """The frame must cover the shots people actually take.

        Corners of the penalty area and the edge of the D included -- if a
        routine shot needs the "outside the view" note, the crop is too tight.
        """
        fig = self._fig(*[charts.Shot(x, y, 0.1)
                          for x in (0.70, 0.80, 0.90, 0.99)
                          for y in (0.22, 0.5, 0.78)])
        assert "off the map" not in fig.layout.title.text
        low, high = fig.layout.yaxis.range
        xlow, xhigh = fig.layout.xaxis.range
        for trace in fig.data:
            assert all(low <= v <= high for v in trace.y)
            assert all(xlow <= v <= xhigh for v in trace.x)
