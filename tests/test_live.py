"""Live matchday: multipliers, provisional bonus and auto-substitution legality.

The auto-sub tests carry the weight here. A substitution engine that produces an
illegal XI is not a cosmetic bug -- it reports points the manager will never
receive, which is worse than reporting nothing. Every edge case FPL's own rules
name is pinned: the goalkeeper exchange, the formation floor, bench order
priority, and the bench player who did not play either.
"""
from __future__ import annotations

from fpl_assistant import live


def _p(pid, name, position, minutes=90, points=2, order=0):
    return {"player_id": pid, "name": name, "position": position,
            "minutes": minutes, "points": points, "order": order}


def _xi(defs=4, mids=4, fwds=2, overrides=None):
    """A legal starting XI: 1 GKP + the requested outfield shape.

    Ids are stable by band so a test can name one: keeper 1, defenders from 10,
    midfielders from 20, forwards from 30.
    """
    overrides = overrides or {}
    squad = [_p(1, "Keeper", "GKP")]
    for position, count, base in (("DEF", defs, 10), ("MID", mids, 20),
                                  ("FWD", fwds, 30)):
        for index in range(count):
            pid = base + index
            squad.append(_p(pid, f"{position}{pid}", position))
    for player in squad:
        player.update(overrides.get(player["player_id"], {}))
    return squad


# ==========================================================================
# Formation legality
# ==========================================================================
class TestFormationLegality:
    def test_canonical_formations_are_legal(self):
        for defs, mids, fwds in ((3, 4, 3), (3, 5, 2), (4, 4, 2),
                                 (4, 3, 3), (5, 3, 2), (5, 4, 1), (4, 5, 1)):
            positions = (["GKP"] + ["DEF"] * defs + ["MID"] * mids
                         + ["FWD"] * fwds)
            assert live.formation_legal(positions), f"{defs}-{mids}-{fwds}"

    def test_two_defenders_is_illegal(self):
        assert not live.formation_legal(
            ["GKP"] + ["DEF"] * 2 + ["MID"] * 5 + ["FWD"] * 3)

    def test_no_forward_is_illegal(self):
        assert not live.formation_legal(
            ["GKP"] + ["DEF"] * 5 + ["MID"] * 5)

    def test_one_midfielder_is_illegal(self):
        assert not live.formation_legal(
            ["GKP"] + ["DEF"] * 5 + ["MID"] * 1 + ["FWD"] * 4)

    def test_two_keepers_is_illegal(self):
        assert not live.formation_legal(
            ["GKP"] * 2 + ["DEF"] * 4 + ["MID"] * 3 + ["FWD"] * 2)

    def test_wrong_size_is_illegal(self):
        assert not live.formation_legal(["GKP"] + ["DEF"] * 3 + ["MID"] * 4)
        assert not live.formation_legal(
            ["GKP"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3)


# ==========================================================================
# Auto-substitutions
# ==========================================================================
class TestAutoSubs:
    def test_no_subs_when_everyone_played(self):
        assert live.auto_subs(_xi(), [_p(90, "Bench", "MID", minutes=90)]) == []

    def test_blank_starter_is_replaced(self):
        starters = _xi(overrides={20: {"minutes": 0, "points": 0}})
        bench = [_p(90, "SubMid", "MID", minutes=70, points=6, order=12)]
        subs = live.auto_subs(starters, bench)
        assert len(subs) == 1
        assert subs[0].in_name == "SubMid"
        assert subs[0].points_gained == 6

    def test_bench_player_who_also_blanked_is_not_used(self):
        starters = _xi(overrides={20: {"minutes": 0, "points": 0}})
        bench = [_p(90, "AlsoBlank", "MID", minutes=0, points=0, order=12)]
        assert live.auto_subs(starters, bench) == []

    def test_bench_order_is_the_priority(self):
        """The manager's order decides, not the points scored."""
        starters = _xi(overrides={20: {"minutes": 0, "points": 0}})
        bench = [_p(90, "First", "MID", minutes=90, points=2, order=12),
                 _p(91, "Second", "MID", minutes=90, points=15, order=13)]
        subs = live.auto_subs(starters, bench)
        assert subs[0].in_name == "First"

    def test_keeper_is_replaced_only_by_the_bench_keeper(self):
        starters = _xi(overrides={1: {"minutes": 0, "points": 0}})
        bench = [_p(90, "OutfieldFirst", "DEF", minutes=90, points=9, order=12),
                 _p(91, "SubKeeper", "GKP", minutes=90, points=3, order=13)]
        subs = live.auto_subs(starters, bench)
        assert len(subs) == 1
        assert subs[0].in_name == "SubKeeper"
        assert subs[0].in_position == "GKP"

    def test_outfielder_never_replaced_by_a_keeper(self):
        starters = _xi(overrides={20: {"minutes": 0, "points": 0}})
        bench = [_p(90, "SubKeeper", "GKP", minutes=90, points=8, order=12)]
        assert live.auto_subs(starters, bench) == []

    def test_sub_that_would_break_the_formation_is_skipped(self):
        """A 3-4-3 losing a defender cannot take a forward: 2 DEF is illegal."""
        starters = _xi(defs=3, mids=4, fwds=3,
                       overrides={10: {"minutes": 0, "points": 0}})
        bench = [_p(90, "ExtraFwd", "FWD", minutes=90, points=12, order=12),
                 _p(91, "SubDef", "DEF", minutes=90, points=2, order=13)]
        subs = live.auto_subs(starters, bench)
        assert len(subs) == 1
        assert subs[0].in_name == "SubDef", "must skip to the legal option"

    def test_a_bench_player_comes_on_only_once(self):
        starters = _xi(overrides={20: {"minutes": 0, "points": 0},
                                  21: {"minutes": 0, "points": 0}})
        bench = [_p(90, "OnlyOne", "MID", minutes=90, points=5, order=12)]
        subs = live.auto_subs(starters, bench)
        assert len(subs) == 1

    def test_two_blanks_take_two_different_subs(self):
        starters = _xi(overrides={20: {"minutes": 0, "points": 0},
                                  21: {"minutes": 0, "points": 0}})
        bench = [_p(90, "SubA", "MID", minutes=90, points=5, order=12),
                 _p(91, "SubB", "MID", minutes=90, points=4, order=13)]
        subs = live.auto_subs(starters, bench)
        assert {s.in_name for s in subs} == {"SubA", "SubB"}

    def test_resulting_xi_is_always_legal(self):
        """Property: whatever the engine returns must leave a legal team."""
        starters = _xi(defs=5, mids=3, fwds=2,
                       overrides={10: {"minutes": 0, "points": 0},
                                  20: {"minutes": 0, "points": 0}})
        bench = [_p(90, "B1", "FWD", minutes=90, points=3, order=12),
                 _p(91, "B2", "DEF", minutes=90, points=3, order=13),
                 _p(92, "B3", "MID", minutes=90, points=3, order=14)]
        subs = live.auto_subs(starters, bench)

        positions = [p["position"] for p in starters]
        for sub in subs:
            positions.remove(sub.out_position)
            positions.append(sub.in_position)
        assert live.formation_legal(positions)


class TestViceCaptain:
    def test_vice_takes_over_when_captain_blanks(self):
        assert live.vice_takes_over({"minutes": 0}, {"minutes": 90})

    def test_vice_does_not_take_over_when_captain_played(self):
        assert not live.vice_takes_over({"minutes": 12}, {"minutes": 90})

    def test_vice_who_also_blanked_does_not_take_over(self):
        assert not live.vice_takes_over({"minutes": 0}, {"minutes": 0})

    def test_missing_players_are_handled(self):
        assert not live.vice_takes_over(None, {"minutes": 90})
        assert not live.vice_takes_over({"minutes": 0}, None)


# ==========================================================================
# Provisional bonus
# ==========================================================================
class TestProvisionalBonus:
    def test_top_three_take_three_two_one(self):
        awards = live.provisional_bonus({1: [(10, 40), (11, 35), (12, 30),
                                             (13, 25)]})
        assert awards == {10: 3, 11: 2, 12: 1}

    def test_tie_on_top_shares_the_higher_award(self):
        """Two tied on top both take 3, and the next takes 1 -- FPL's rule."""
        awards = live.provisional_bonus({1: [(10, 40), (11, 40), (12, 30)]})
        assert awards == {10: 3, 11: 3, 12: 1}

    def test_triple_tie_on_top_consumes_every_award(self):
        awards = live.provisional_bonus(
            {1: [(10, 40), (11, 40), (12, 40), (13, 30)]})
        assert awards == {10: 3, 11: 3, 12: 3}
        assert 13 not in awards

    def test_tie_for_second(self):
        awards = live.provisional_bonus(
            {1: [(10, 40), (11, 30), (12, 30), (13, 20)]})
        assert awards == {10: 3, 11: 2, 12: 2}

    def test_zero_bps_earns_nothing(self):
        assert live.provisional_bonus({1: [(10, 0), (11, 0)]}) == {}

    def test_each_fixture_is_scored_independently(self):
        awards = live.provisional_bonus({
            1: [(10, 40), (11, 35), (12, 30)],
            2: [(20, 90), (21, 80), (22, 70)]})
        assert awards[10] == 3 and awards[20] == 3

    def test_empty_input(self):
        assert live.provisional_bonus({}) == {}


# ==========================================================================
# Multipliers, from a real database
# ==========================================================================
def _seed_squad(db, gw=2, chip=None, tc_multiplier=2):
    db.execute("INSERT OR REPLACE INTO teams(id, name, short_name)"
               " VALUES (1, 'Club', 'CLB')")
    for pid in range(1, 16):
        etype = 1 if pid == 1 else (2 if pid <= 6 else (3 if pid <= 11 else 4))
        db.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, team_id, element_type, position, now_cost, status)
               VALUES (?, ?, 1, ?, ?, 5.0, 'a')""",
            (pid, f"P{pid}", etype,
             {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}[etype]))
        db.execute(
            """INSERT OR REPLACE INTO player_gw
                 (player_id, gw, minutes, starts, total_points, bps, bonus)
               VALUES (?, ?, 90, 1, ?, 20, 0)""",
            (pid, gw, 13 if pid == 11 else 2))
        multiplier = 0 if pid > 11 else (tc_multiplier if pid == 11 else 1)
        db.execute(
            """INSERT OR REPLACE INTO my_picks
                 (gw, player_id, position, multiplier, is_captain, is_vice, chip)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (gw, pid, pid, multiplier, 1 if pid == 11 else 0, chip))
    db.commit()


class TestMultipliers:
    def test_triple_captain_scores_three_times(self, db):
        """The regression this whole fix exists for."""
        _seed_squad(db, chip="3xc", tc_multiplier=3)
        state = live.build(db, 2, fetch=False)
        # 10 starters at 2 pts + captain 13 x 3 = 20 + 39 = 59
        assert state.provisional_points == 59
        assert state.active_chip == "3xc"

    def test_normal_captain_scores_twice(self, db):
        _seed_squad(db, chip=None, tc_multiplier=2)
        state = live.build(db, 2, fetch=False)
        assert state.provisional_points == 46      # 20 + 13*2

    def test_triple_captain_beats_normal_by_one_captain_haul(self, db):
        _seed_squad(db, chip=None, tc_multiplier=2)
        normal = live.build(db, 2, fetch=False).provisional_points
        _seed_squad(db, chip="3xc", tc_multiplier=3)
        tripled = live.build(db, 2, fetch=False).provisional_points
        assert tripled - normal == 13

    def test_bench_scores_nothing(self, db):
        _seed_squad(db, tc_multiplier=2)
        state = live.build(db, 2, fetch=False)
        bench_points = sum(
            p.total_points for p in state.squad if p.player_id > 11)
        assert bench_points > 0                     # they did play
        assert state.provisional_points == 46       # but contribute nothing

    def test_bench_boost_disables_auto_subs(self, db):
        _seed_squad(db, chip="bboost", tc_multiplier=2)
        db.execute("UPDATE my_picks SET multiplier = 1 WHERE gw = 2"
                   " AND multiplier = 0")
        db.commit()
        state = live.build(db, 2, fetch=False)
        assert state.active_chip == "bboost"
        assert state.subs == []


class TestLiveBuild:
    def test_empty_gameweek_does_not_raise(self, db):
        state = live.build(db, 7, fetch=False)
        assert state.provisional_points == 0
        assert state.notes

    def test_live_payload_is_parsed(self, db):
        _seed_squad(db, tc_multiplier=2)
        elements = [{"id": 1, "stats": {"minutes": 90, "total_points": 6,
                                        "bps": 33, "goals_scored": 0,
                                        "assists": 0, "clean_sheets": 1,
                                        "bonus": 0}}]
        state = live.build(db, 2, elements=elements, fetch=False)
        assert state.players[1].total_points == 6
        assert state.players[1].bps == 33

    def test_provisional_bonus_is_added_only_while_unfinished(self, db):
        _seed_squad(db, tc_multiplier=2)
        player = live.LivePlayer(player_id=1, total_points=6,
                                 provisional_bonus=3, fixture_finished=False)
        assert player.live_points == 9
        player.fixture_finished = True
        assert player.live_points == 6
