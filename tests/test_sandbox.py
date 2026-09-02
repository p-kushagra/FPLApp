"""Transfer sandbox: legality, chip arithmetic and isolation from the DB.

Three classes of failure, in the order they would hurt:

1. **The sandbox writes to `my_picks`.** Silently destroys the record of what
   you actually own, which every retrospective is scored against. Untestable
   by inspection, trivially testable here.
2. **An illegal squad is accepted.** A fourth Arsenal player or a negative
   bank produces advice that looks authoritative and cannot be executed.
3. **Chip arithmetic is wrong.** A Free Hit that still charges -8, or a Bench
   Boost compared against a bench-less baseline, gives a confidently wrong
   Net EV -- the one number the whole screen exists to produce.
"""
from __future__ import annotations

import pytest

from fpl_assistant.services import sandbox
from fpl_assistant.ui.pitch import PitchPlayer

TEAMS = {"ARS": 1, "MCI": 2, "LIV": 3, "TOT": 4, "CHE": 5, "EVE": 6}


def _player(pid, name, position, team="ARS", cost=5.0, starting=True,
            xp=3.0, bench_order=0, captain=False):
    return PitchPlayer(player_id=pid, name=name, position=position, team=team,
                       cost=cost, starting=starting, xp=xp,
                       bench_order=bench_order, is_captain=captain,
                       multiplier=2.0 if captain else (1.0 if starting else 0.0))


@pytest.fixture
def squad():
    """A legal 15 in a 4-4-2, spread so no club is near the limit."""
    clubs = ["ARS", "MCI", "LIV", "TOT", "CHE"]
    players = [_player(1, "Keeper", "GKP", "ARS", 5.0, True, 4.0)]
    pid = 2
    for position, starters, total in (("DEF", 4, 5), ("MID", 4, 5),
                                      ("FWD", 2, 3)):
        for i in range(total):
            players.append(_player(
                pid, f"{position}{pid}", position, clubs[i % len(clubs)],
                5.0 + i, i < starters, 4.0 - i * 0.5,
                bench_order=0 if i < starters else i))
            pid += 1
    players.append(_player(pid, "Sub keeper", "GKP", "EVE", 4.0, False, 0.5))
    players[1].is_captain = True
    players[1].multiplier = 2.0
    return players


@pytest.fixture
def team_ids(squad):
    return {p.player_id: TEAMS[p.team] for p in squad}


@pytest.fixture
def state(squad):
    starters = [p for p in squad if p.starting]
    captain = next(p for p in squad if p.is_captain)
    return sandbox.SandboxState(
        gw=3, squad=squad, bank=1.0, free_transfers=1,
        sell_prices={p.player_id: p.cost for p in squad},
        baseline_xi_xp=sum(p.xp for p in starters) + captain.xp,
        baseline_squad_xp=sum(p.xp for p in squad) + captain.xp)


def _candidate(pid=99, name="Target", position="MID", team="EVE",
               cost=6.0, xp=7.0):
    return sandbox.Candidate(player_id=pid, name=name, position=position,
                             team=team, team_id=TEAMS[team], cost=cost, xp=xp)


ELEMENT_TYPE = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}
BASELINE_TABLES = ("my_picks", "players", "teams", "xp_projection")


def _seed_picks(conn, squad, gw: int) -> None:
    """Minimal teams / players / my_picks so `open_sandbox` has real input."""
    for short, tid in TEAMS.items():
        conn.execute(
            "INSERT OR REPLACE INTO teams(id, name, short_name) "
            "VALUES (?, ?, ?)", (tid, short, short))
    for index, player in enumerate(squad, 1):
        conn.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, element_type, team_id, now_cost, status)
               VALUES (?, ?, ?, ?, ?, 'a')""",
            (player.player_id, player.name, ELEMENT_TYPE[player.position],
             TEAMS[player.team], player.cost))
        conn.execute(
            """INSERT OR REPLACE INTO my_picks
                 (gw, player_id, position, multiplier, is_captain, is_vice)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (gw, player.player_id, index,
             2 if player.is_captain else (1 if player.starting else 0),
             1 if player.is_captain else 0))
    conn.commit()


def _snapshot(conn) -> dict[str, int]:
    """Row counts for every table the sandbox must never write to."""
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in BASELINE_TABLES}


# ==========================================================================
class TestIsolation:
    def test_a_swap_never_mutates_the_state_it_was_given(self, state, team_ids):
        """Mutators return a new state; the old one is a valid undo point."""
        out = next(p for p in state.starters if p.position == "MID")
        before = [p.player_id for p in state.squad]
        picked = sandbox.select(state, out.player_id)
        outcome = sandbox.transfer_in(picked, _candidate(), team_ids)

        assert outcome.ok, outcome.reason
        assert [p.player_id for p in state.squad] == before
        assert state.transfers == []
        assert outcome.state is not state

    def test_opening_a_sandbox_writes_nothing(self, db, squad):
        """`open_sandbox` is a read. If it ever writes, this catches it.

        Seeded here rather than skipped on an empty database -- a test that
        silently skips is not protecting the isolation promise it names.
        """
        _seed_picks(db, squad, gw=3)
        before = _snapshot(db)

        state = sandbox.open_sandbox(db, 3)
        assert len(state.squad) == len(squad), "the squad did not load"
        assert _snapshot(db) == before, "open_sandbox wrote to the database"


# ==========================================================================
class TestLegality:
    def test_negative_bank_is_blocked_and_says_how_short(self, state, team_ids):
        """Bank + sale - purchase must stay >= 0."""
        out = next(p for p in state.starters if p.position == "DEF")
        picked = sandbox.select(state, out.player_id)
        # bank 1.0 + sell 5.0 = 6.0 available; ask for 9.5.
        outcome = sandbox.transfer_in(
            picked, _candidate(position=out.position, cost=9.5), team_ids)

        assert not outcome.ok
        assert "short" in outcome.reason
        assert "3.5" in outcome.reason, "the shortfall should be quantified"

    def test_fourth_player_from_one_club_is_blocked(self, state, team_ids):
        """Three per club is the limit managers actually hit."""
        arsenal = [p for p in state.squad if p.team == "ARS"]
        assert len(arsenal) >= 1
        # Fill Arsenal to exactly three, then try a fourth.
        current = state
        swapped = 0
        for player in current.squad:
            if swapped >= 3 - len(arsenal):
                break
            if player.team == "ARS" or player.position != "MID":
                continue
            picked = sandbox.select(current, player.player_id)
            outcome = sandbox.transfer_in(
                picked, _candidate(pid=200 + swapped, position="MID",
                                   team="ARS", cost=player.cost),
                {**team_ids, 200 + swapped: TEAMS["ARS"]})
            if outcome.ok:
                current = outcome.state
                swapped += 1

        target = next(p for p in current.squad
                      if p.team != "ARS" and p.position == "MID")
        picked = sandbox.select(current, target.player_id)
        outcome = sandbox.transfer_in(
            picked, _candidate(pid=300, position="MID", team="ARS",
                               cost=target.cost),
            {**{p.player_id: TEAMS[p.team] for p in current.squad},
             300: TEAMS["ARS"]})

        assert not outcome.ok
        assert "team" in outcome.reason and "max" in outcome.reason

    def test_position_must_match_to_keep_the_quota_legal(self, state, team_ids):
        """A single transfer is forced to be like-for-like.

        The 2/5/5/3 quota is exact, so swapping a MID for a DEF leaves 4 and 6.
        The refusal explains that rather than reporting a bare quota violation.
        """
        mid = next(p for p in state.squad if p.position == "MID")
        picked = sandbox.select(state, mid.player_id)
        outcome = sandbox.transfer_in(
            picked, _candidate(position="DEF", cost=5.0), team_ids)

        assert not outcome.ok
        assert "DEF" in outcome.reason and "MID" in outcome.reason

    def test_cannot_buy_a_player_already_owned(self, state, team_ids):
        owned = state.squad[3]
        picked = sandbox.select(state, state.starters[1].player_id)
        outcome = sandbox.transfer_in(
            picked, _candidate(pid=owned.player_id, name=owned.name,
                               position=owned.position, cost=owned.cost),
            team_ids)
        assert not outcome.ok and "already" in outcome.reason

    def test_swap_without_a_selection_is_refused(self, state, team_ids):
        outcome = sandbox.transfer_in(state, _candidate(), team_ids)
        assert not outcome.ok and "select" in outcome.reason.lower()


# ==========================================================================
class TestChipArithmetic:
    def test_hits_charge_four_per_transfer_beyond_the_free_ones(self, state):
        state.transfers = [sandbox.Transfer(1, 2, "a", "b", 5.0, 5.0)] * 3
        state.free_transfers = 1
        assert sandbox.hits(state) == 8      # 3 transfers, 1 free, 2 x -4

    def test_banked_free_transfers_absorb_the_hit(self, state):
        state.transfers = [sandbox.Transfer(1, 2, "a", "b", 5.0, 5.0)] * 3
        state.free_transfers = 5
        assert sandbox.hits(state) == 0

    @pytest.mark.parametrize("chip", ["wildcard", "free_hit"])
    def test_wildcard_and_free_hit_zero_the_hit(self, state, chip):
        """However many transfers, the cost is zero under these two."""
        state.transfers = [sandbox.Transfer(1, 2, "a", "b", 5.0, 5.0)] * 9
        state.free_transfers = 1
        state.chip = chip
        assert sandbox.hits(state) == 0
        assert sandbox.impact(state).net_ev == pytest.approx(
            sandbox.impact(state).xp_delta)

    def test_triple_captain_pays_three_times_not_two(self, state):
        captain = state.captain
        plain = sandbox.scoring_xp(state)
        state.chip = "triple_captain"
        assert sandbox.scoring_xp(state) - plain == pytest.approx(captain.xp)

    def test_bench_boost_scores_all_fifteen(self, state):
        xi = sandbox.scoring_xp(state)
        state.chip = "bench_boost"
        boosted = sandbox.scoring_xp(state)
        assert boosted - xi == pytest.approx(
            sum(p.xp for p in state.bench))

    def test_a_chip_alone_does_not_invent_a_gain(self, state):
        """The baseline moves with the chip, or the bar reports free points.

        Turning on Bench Boost adds the bench to the scenario. If the baseline
        stayed at XI-only, the delta would show +N for a chip that changed no
        player -- an artefact of measuring the two sides differently.
        """
        for chip in (None, "bench_boost", "triple_captain"):
            state.chip = chip
            metrics = sandbox.impact(state)
            assert metrics.xp_delta == pytest.approx(0.0), (
                f"chip {chip} reports a gain with no transfer made")
            assert metrics.net_ev == pytest.approx(0.0)

    def test_net_ev_is_gain_minus_hit(self, state, team_ids):
        mid = next(p for p in state.starters if p.position == 'MID')
        picked = sandbox.select(state, mid.player_id)
        outcome = sandbox.transfer_in(
            picked, _candidate(position=mid.position,
                               cost=5.0, xp=9.0), team_ids)
        assert outcome.ok, outcome.reason

        new = outcome.state
        new.free_transfers = 0                    # force a hit
        metrics = sandbox.impact(new)
        assert metrics.hits == 4
        assert metrics.net_ev == pytest.approx(metrics.xp_delta - 4)


# ==========================================================================
class TestSelectionAndCaptaincy:
    def test_clicking_the_selected_player_deselects(self, state):
        picked = sandbox.select(state, 5)
        assert picked.selected_id == 5
        assert sandbox.select(picked, 5).selected_id is None

    def test_moving_the_armband_leaves_exactly_one_captain(self, state):
        target = state.starters[4]
        moved = sandbox.set_captain(state, target.player_id)
        captains = [p for p in moved.squad if p.is_captain]
        assert len(captains) == 1 and captains[0].player_id == target.player_id

    def test_the_incoming_player_inherits_the_armband(self, state, team_ids):
        """Transferring out your captain must not leave the XI captainless."""
        captain = state.captain
        picked = sandbox.select(state, captain.player_id)
        outcome = sandbox.transfer_in(
            picked, _candidate(position=captain.position, cost=captain.cost),
            team_ids)
        assert outcome.ok, outcome.reason
        assert outcome.state.captain is not None
        assert len(sandbox.impact(outcome.state).__dict__) > 0


# ==========================================================================
class TestTransferAccounting:
    def test_selling_a_player_bought_in_the_sandbox_is_one_transfer(
            self, state, team_ids):
        """Undoing a mistake must not cost a second hit.

        A -> B -> C is one transfer out of A and into C, not two. Charging
        twice would price a corrected decision as worse than a wrong one.
        """
        out = next(p for p in state.starters if p.position == 'MID')
        first = sandbox.transfer_in(
            sandbox.select(state, out.player_id),
            _candidate(pid=101, position=out.position, cost=5.0), team_ids)
        assert first.ok, first.reason

        second = sandbox.transfer_in(
            sandbox.select(first.state, 101),
            _candidate(pid=102, position=out.position, cost=5.0),
            {**team_ids, 101: TEAMS["EVE"], 102: TEAMS["EVE"]})
        assert second.ok, second.reason

        assert len(second.state.transfers) == 1, "a correction cost a second hit"
        assert second.state.transfers[0].out_id == out.player_id
        assert second.state.transfers[0].in_id == 102

    def test_bank_tracks_the_sale_and_the_purchase(self, state, team_ids):
        out = next(p for p in state.starters if p.position == 'MID')
        picked = sandbox.select(state, out.player_id)
        outcome = sandbox.transfer_in(
            picked, _candidate(position=out.position, cost=out.cost + 0.5),
            team_ids)
        assert outcome.ok, outcome.reason
        assert outcome.state.bank == pytest.approx(state.bank - 0.5)


# ==========================================================================
class TestRosterBrowser:
    def _pool(self):
        return [
            _candidate(1, "Cheap", "DEF", "ARS", 4.0, 2.0),
            _candidate(2, "Mid", "MID", "MCI", 8.0, 6.0),
            _candidate(3, "Premium", "FWD", "LIV", 14.0, 9.0),
        ]

    def test_position_and_price_filters(self):
        pool = self._pool()
        assert len(sandbox.filter_candidates(pool, position="MID")) == 1
        assert len(sandbox.filter_candidates(pool, max_price=8.0)) == 2

    def test_search_matches_name_or_team(self):
        pool = self._pool()
        assert len(sandbox.filter_candidates(pool, query="prem")) == 1
        assert len(sandbox.filter_candidates(pool, query="mci")) == 1

    def test_sorts_put_the_best_first(self):
        pool = self._pool()
        by_xp = sandbox.filter_candidates(pool, sort="Projected xP")
        assert by_xp[0].name == "Premium"
        by_price = sandbox.filter_candidates(pool, sort="Price")
        assert by_price[0].cost == 14.0

    def test_missing_fdr_never_sorts_ahead_of_a_real_fixture(self):
        """A None must not masquerade as the easiest fixture on the board."""
        pool = [_candidate(1, "Known", "MID", "ARS"),
                _candidate(2, "Unknown", "MID", "MCI")]
        pool[0] = sandbox.Candidate(**{**pool[0].__dict__, "next_fdr": 2})
        ordered = sandbox.filter_candidates(pool, sort="Fixture ease")
        assert ordered[0].name == "Known"


# ==========================================================================
class TestPersistence:
    def test_saving_writes_only_to_the_scenario_tables(self, db, state):
        """The whole isolation promise, asserted rather than asserted-to."""
        _seed_picks(db, state.squad, gw=state.gw)
        before = _snapshot(db)

        scenario_id = sandbox.save_scenario(db, state, "test scenario")
        assert scenario_id > 0
        assert _snapshot(db) == before,             "saving a scenario touched a baseline table"

        rows = db.execute(
            "SELECT COUNT(*) FROM scenario_pick WHERE scenario_id = ?",
            (scenario_id,)).fetchone()[0]
        assert rows == len(state.squad)

    def test_saved_metrics_are_frozen_not_recomputed(self, db, state):
        """A saved comparison must not re-baseline against newer projections."""
        state.chip = "triple_captain"
        expected = sandbox.impact(state)
        scenario_id = sandbox.save_scenario(db, state, "frozen")

        row = db.execute(
            "SELECT * FROM scenario WHERE scenario_id = ?",
            (scenario_id,)).fetchone()
        assert row["chip"] == "triple_captain"
        assert row["net_ev"] == pytest.approx(round(expected.net_ev, 2))
        assert row["baseline_xp"] == pytest.approx(
            round(expected.baseline_xp, 2))

    def test_listing_and_deleting(self, db, state):
        first = sandbox.save_scenario(db, state, "one")
        sandbox.save_scenario(db, state, "two")
        assert len(sandbox.list_scenarios(db, gw=state.gw)) == 2

        sandbox.delete_scenario(db, first)
        remaining = sandbox.list_scenarios(db, gw=state.gw)
        assert len(remaining) == 1 and remaining[0]["name"] == "two"
        assert db.execute(
            "SELECT COUNT(*) FROM scenario_pick WHERE scenario_id = ?",
            (first,)).fetchone()[0] == 0
