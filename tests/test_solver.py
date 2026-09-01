"""ILP solver, FT bank and the independent legality validator.

T-SOLV-06 is the blocking test: EVERY path the solver returns is checked by
`strategy.validator`, which shares no code with the solver and re-derives every
rule from config/rules.yaml. A validator built from the solver's own helpers
could only prove self-consistency.
"""
from __future__ import annotations

import pytest

from fpl_assistant.rules import load_rules
from fpl_assistant.strategy import solver, validator
from fpl_assistant.strategy.solver import (
    AGGRESSIVE,
    CONSERVATIVE,
    Profile,
    SolverContext,
)
from fpl_assistant.temporal import FTBank, project_ft

RULES = load_rules()


# --------------------------------------------------------------------------
# A synthetic universe: enough players to build a legal squad with choices
# --------------------------------------------------------------------------
def make_universe(n_per_pos=(6, 14, 14, 8), clubs=8, seed=7):
    """Deterministic player pool. ids are 1..N, prices and xP vary smoothly."""
    players: dict[int, dict] = {}
    pid = 1
    for pos, count in zip(("GKP", "DEF", "MID", "FWD"), n_per_pos):
        for i in range(count):
            players[pid] = {
                "id": pid,
                "position": pos,
                "element_type": {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}[pos],
                "team_id": (pid * seed) % clubs + 1,
                "now_cost": round(4.0 + (i % 7) * 0.8, 1),
                "web_name": f"{pos}{i}",
            }
            pid += 1
    return players


def make_xp(players, gws, spread=1.0):
    """xP that rewards more expensive players, so upgrades are attractive."""
    return {
        (pid, gw): round((p["now_cost"] - 3.5) * spread + (gw % 3) * 0.1, 3)
        for pid, p in players.items()
        for gw in gws
    }


def legal_starting_squad(players, budget=100.0):
    """Cheapest legal 15 respecting quota and the 3-per-club cap."""
    quota = RULES["squad"]["quota"]
    max_club = RULES["squad"]["max_per_club"]
    squad, per_club = [], {}

    for pos, want in quota.items():
        pool = sorted((p for p in players.values() if p["position"] == pos),
                      key=lambda p: p["now_cost"])
        taken = 0
        for p in pool:
            if taken >= want:
                break
            if per_club.get(p["team_id"], 0) >= max_club:
                continue
            squad.append(p["id"])
            per_club[p["team_id"]] = per_club.get(p["team_id"], 0) + 1
            taken += 1
        assert taken == want, f"could not fill {pos}"
    return squad


@pytest.fixture
def universe():
    return make_universe()


@pytest.fixture
def ctx(universe):
    gws = [10, 11, 12]
    squad = legal_starting_squad(universe)
    return SolverContext(
        players=universe,
        xp=make_xp(universe, gws),
        gws=gws,
        initial_squad=squad,
        initial_bank=8.0,
        initial_ft=2,
        rules=RULES,
    )


# ==========================================================================
# T-TEMP: the free-transfer recurrence
# ==========================================================================
class TestFreeTransferBank:
    """chip_retains_ft: true, chip_accrues_ft: false (verified 2025-26)."""

    def test_rolling_banks_one(self):
        nxt = project_ft(FTBank(gw=1, available=1), transfers=0)
        assert nxt.available == 2
        assert nxt.hits == 0

    def test_bank_caps_at_five(self):
        bank = FTBank(gw=1, available=5)
        assert project_ft(bank, transfers=0).available == 5

    def test_using_one_of_two_keeps_two(self):
        nxt = project_ft(FTBank(gw=1, available=2), transfers=1)
        assert nxt.consumed == 1
        assert nxt.hits == 0
        assert nxt.available == 2      # 2 - 1 + 1

    def test_overspending_charges_hits(self):
        nxt = project_ft(FTBank(gw=1, available=1), transfers=3)
        assert nxt.consumed == 1
        assert nxt.hits == 2
        assert nxt.points_cost == 8
        assert nxt.available == 1      # 0 + 1

    def test_chip_retains_the_bank(self):
        """A Wildcard consumes no free transfers, however many are made."""
        nxt = project_ft(FTBank(gw=1, available=3), transfers=12, chip="wildcard")
        assert nxt.consumed == 0
        assert nxt.hits == 0

    def test_chip_does_not_accrue_this_season(self):
        """chip_accrues_ft: false -> the bank freezes, it does not grow."""
        nxt = project_ft(FTBank(gw=1, available=3), transfers=8, chip="wildcard")
        assert nxt.available == 3, "chip week must freeze the bank, not bank +1"

    def test_free_hit_behaves_like_wildcard_for_the_bank(self):
        nxt = project_ft(FTBank(gw=1, available=2), transfers=11, chip="freehit")
        assert (nxt.consumed, nxt.hits, nxt.available) == (0, 0, 2)

    def test_bench_boost_is_not_a_squad_chip(self):
        """Bench Boost does not make transfers free."""
        nxt = project_ft(FTBank(gw=1, available=1), transfers=2, chip="benchboost")
        assert nxt.hits == 1

    def test_accrual_flag_is_config_driven(self):
        """Flip the rule and the recurrence follows, with no code change."""
        variant = {**RULES, "transfers": {**RULES["transfers"],
                                          "chip_accrues_ft": True}}
        nxt = project_ft(FTBank(gw=1, available=3), 8, "wildcard", variant)
        assert nxt.available == 4

    @pytest.mark.parametrize("start,moves", [
        (1, [0, 0, 0, 0, 0, 0]),
        (1, [1, 1, 1, 1, 1, 1]),
        (5, [3, 0, 2, 0, 1, 4]),
        (2, [7, 0, 0, 3, 1, 0]),
    ])
    def test_bank_never_leaves_bounds(self, start, moves):
        bank = FTBank(gw=1, available=start)
        for t in moves:
            bank = project_ft(bank, t)
            assert 0 <= bank.available <= RULES["transfers"]["max_banked"]
            assert bank.hits >= 0

    def test_ten_gameweek_ledger_matches_hand_calculation(self):
        """Hand-computed: roll, roll, 1 transfer, WC, roll, 3 transfers, ..."""
        plan = [
            (0, None, 2), (0, None, 3), (1, None, 3), (0, "wildcard", 3),
            (0, None, 4), (3, None, 2), (0, None, 3), (5, None, 1),
            (0, None, 2), (1, None, 2),
        ]
        bank = FTBank(gw=1, available=1)
        for gw, (transfers, chip, expected) in enumerate(plan, start=1):
            bank = project_ft(bank, transfers, chip)
            assert bank.available == expected, (
                f"GW{gw}: {transfers} transfers, chip={chip} -> "
                f"expected {expected}, got {bank.available}"
            )


# ==========================================================================
# T-SOLV-01..05: model structure
# ==========================================================================
class TestSolverModel:
    def test_solves_to_optimality(self, ctx):
        path = solver.solve(ctx, CONSERVATIVE, time_limit=20)
        assert path.status == "Optimal", path.status
        assert len(path.steps) == len(ctx.gws)

    def test_candidate_pruning_includes_incumbents(self, ctx):
        cand = solver.candidate_set(ctx, k=5)
        assert set(ctx.initial_squad) <= set(cand), "must never drop the current squad"

    def test_candidate_set_is_deterministic(self, ctx):
        assert solver.candidate_set(ctx, k=8) == solver.candidate_set(ctx, k=8)

    def test_conservative_takes_no_hits(self, ctx):
        path = solver.solve(ctx, CONSERVATIVE, time_limit=20)
        assert path.total_hits == 0

    def test_aggressive_respects_its_hit_budget(self, ctx):
        path = solver.solve(ctx, AGGRESSIVE, time_limit=20)
        assert path.total_hits <= AGGRESSIVE.max_hits

    def test_aggressive_DOES_take_a_hit_when_it_pays(self, universe):
        """The C11 guard: a hit must be reachable, not merely permitted.

        Found by mutation testing. Weakening the hits lower bound to `h >= 0`
        does not make the model illegal -- the objective drives h to 0 and the
        FT cap then forbids the extra transfer. The model stays legal and the
        validator passes, but the Aggressive route silently collapses into the
        Conservative one and the -4 feature quietly stops existing.

        So: construct a case where one transfer is worth far more than 4 points
        with zero free transfers, and assert the hit is actually taken.
        """
        gws = [10, 11, 12]
        squad = legal_starting_squad(universe)
        xp = {(pid, gw): 1.0 for pid in universe for gw in gws}

        # One un-owned forward is worth ~10 pts/GW more than anything owned.
        star = next(p["id"] for p in universe.values()
                    if p["position"] == "FWD" and p["id"] not in squad)
        for gw in gws:
            xp[(star, gw)] = 11.0

        ctx = SolverContext(
            players=universe, xp=xp, gws=gws, initial_squad=squad,
            initial_bank=50.0,     # affordable
            initial_ft=0,          # so ANY transfer costs a hit
            rules=RULES,
        )

        path = solver.solve(ctx, AGGRESSIVE, time_limit=25)
        assert path.status == "Optimal", path.status
        assert path.total_hits >= 1, (
            "a +30 xP upgrade for -4 must be taken; the solver cannot reach a hit"
        )
        assert star in {m.player_in for st in path.steps for m in st.moves}
        assert validator.validate_path(path, ctx.players).legal

    def test_conservative_refuses_the_same_hit(self, universe):
        """The mirror: max_hits=0 must forgo even a hugely profitable hit."""
        gws = [10, 11, 12]
        squad = legal_starting_squad(universe)
        xp = {(pid, gw): 1.0 for pid in universe for gw in gws}
        star = next(p["id"] for p in universe.values()
                    if p["position"] == "FWD" and p["id"] not in squad)
        for gw in gws:
            xp[(star, gw)] = 11.0

        ctx = SolverContext(players=universe, xp=xp, gws=gws,
                            initial_squad=squad, initial_bank=50.0,
                            initial_ft=0, rules=RULES)
        path = solver.solve(ctx, CONSERVATIVE, time_limit=25)
        assert path.status == "Optimal"
        assert path.total_hits == 0

    def test_captain_is_a_starter_every_week(self, ctx):
        path = solver.solve(ctx, CONSERVATIVE, time_limit=20)
        for step in path.steps:
            assert step.captain is not None
            assert step.captain in step.xi

    def test_bank_never_goes_negative(self, ctx):
        path = solver.solve(ctx, AGGRESSIVE, time_limit=20)
        for step in path.steps:
            assert step.bank_after >= -1e-6, f"GW{step.gw}: {step.bank_after}"

    def test_ft_bank_respects_the_cap(self, ctx):
        path = solver.solve(ctx, CONSERVATIVE, time_limit=20)
        for step in path.steps:
            assert 0 <= step.ft_after <= RULES["transfers"]["max_banked"]

    def test_solver_is_deterministic(self, ctx):
        a = solver.solve(ctx, CONSERVATIVE, time_limit=20)
        b = solver.solve(ctx, CONSERVATIVE, time_limit=20)
        assert a.summary() == b.summary()
        assert a.objective == pytest.approx(b.objective, abs=1e-4)

    def test_three_routes_are_produced(self, ctx):
        routes = solver.three_routes(ctx, time_limit=20, k=12)
        assert len(routes) == 3
        assert [r.profile for r in routes] == [
            "conservative", "aggressive", "chip_setup"]

    def test_aggressive_gross_xp_is_not_worse(self, ctx):
        """More freedom cannot produce a lower gross optimum."""
        routes = solver.three_routes(ctx, time_limit=20, k=12)
        cons = next(r for r in routes if r.profile == "conservative")
        aggr = next(r for r in routes if r.profile == "aggressive")
        if cons.status == aggr.status == "Optimal":
            assert aggr.total_xp >= cons.total_xp - 1e-6

    def test_infeasible_model_returns_a_path_not_an_exception(self, ctx):
        """No budget and an expensive-only pool: must degrade, not raise."""
        broke = SolverContext(**{**ctx.__dict__, "initial_bank": -50.0})
        path = solver.solve(broke, CONSERVATIVE, time_limit=10)
        assert path.status != "Optimal"
        assert path.steps == []

    def test_relaxation_ladder_is_recorded(self, ctx):
        impossible = Profile(**{**CONSERVATIVE.__dict__, "chip": "wildcard",
                                "chip_gw": 999})
        path = solver.solve_with_relaxation(impossible, profile=impossible) \
            if False else solver.solve_with_relaxation(ctx, impossible, time_limit=10)
        assert isinstance(path.relaxations, list)




# ==========================================================================
# Chip enabler route
# ==========================================================================
class TestChipRoute:
    """The wildcard week must actually be usable.

    Found by mutation testing: with `q == transfers_in - h` as an equality, a
    chip week forced q=0 and h=0, which read 0 == transfers_in and silently
    banned every transfer. The model still solved to Optimal, so nothing looked
    wrong -- the wildcard just did nothing.
    """

    @pytest.fixture
    def chip_ctx(self, universe):
        gws = [10, 11, 12]
        squad = legal_starting_squad(universe)
        return SolverContext(
            players=universe, xp=make_xp(universe, gws), gws=gws,
            initial_squad=squad, initial_bank=30.0, initial_ft=1,
            rules=RULES, chips_available={"wildcard"},
        )

    def _wildcard(self, gw=10):
        return Profile("wc", "Wildcard", max_hits=0, chip="wildcard", chip_gw=gw)

    def test_wildcard_week_makes_transfers(self, chip_ctx):
        path = solver.solve(chip_ctx, self._wildcard(), time_limit=30)
        assert path.status == "Optimal", path.status
        wc_step = next(s for s in path.steps if s.chip == "wildcard")
        assert len(wc_step.moves) > 0, (
            "a wildcard that makes zero transfers is not a wildcard"
        )

    def test_wildcard_makes_many_transfers_for_free(self, chip_ctx):
        path = solver.solve(chip_ctx, self._wildcard(), time_limit=30)
        wc_step = next(s for s in path.steps if s.chip == "wildcard")
        assert len(wc_step.moves) >= 2, "unlimited transfers should mean several"
        assert wc_step.hits == 0, "a wildcard never charges a hit"

    def test_wildcard_retains_and_freezes_the_bank(self, chip_ctx):
        """chip_retains_ft: true + chip_accrues_ft: false -> bank unchanged."""
        path = solver.solve(chip_ctx, self._wildcard(), time_limit=30)
        wc_step = next(s for s in path.steps if s.chip == "wildcard")
        assert wc_step.ft_after == wc_step.ft_before, (
            f"bank moved {wc_step.ft_before} -> {wc_step.ft_after} across a chip"
        )

    def test_chip_is_played_at_most_once(self, chip_ctx):
        path = solver.solve(chip_ctx, self._wildcard(gw=None), time_limit=30)
        assert sum(1 for s in path.steps if s.chip) <= 1

    def test_chip_targets_the_requested_gameweek(self, chip_ctx):
        path = solver.solve(chip_ctx, self._wildcard(gw=11), time_limit=30)
        if path.status == "Optimal":
            assert path.chip_gw == 11

    def test_wildcard_path_is_legal(self, chip_ctx):
        path = solver.solve(chip_ctx, self._wildcard(), time_limit=30)
        assert validator.validate_path(path, chip_ctx.players).legal

    def test_no_chip_when_none_available(self, ctx):
        """chips_available is empty -> the chip route must not invent one."""
        path = solver.solve(ctx, self._wildcard(), time_limit=25)
        assert all(s.chip is None for s in path.steps)

# ==========================================================================
# T-SOLV-06 (BLOCKING): independent legality validation
# ==========================================================================
class TestSquadLegalityValidator:
    """The validator must be able to FAIL. Each rule gets a positive and a
    negative case, otherwise a validator that returns 'legal' unconditionally
    would pass the whole suite."""

    def test_accepts_a_legal_squad(self, universe):
        squad = [universe[i] for i in legal_starting_squad(universe)]
        assert validator.validate_squad(squad, bank=20.0).legal

    def test_rejects_wrong_size(self, universe):
        squad = [universe[i] for i in legal_starting_squad(universe)][:14]
        result = validator.validate_squad(squad, bank=20.0)
        assert not result.legal
        assert any(v.rule == "squad_size" for v in result.violations)

    def test_rejects_broken_quota(self, universe):
        ids = legal_starting_squad(universe)
        squad = [universe[i] for i in ids]
        extra_def = next(p for p in universe.values()
                         if p["position"] == "DEF" and p["id"] not in ids)
        squad = [p for p in squad if p["position"] != "FWD"][:14] + [extra_def]
        result = validator.validate_squad(squad, bank=20.0)
        assert any(v.rule == "position_quota" for v in result.violations)

    def test_rejects_four_from_one_club(self, universe):
        for p in list(universe.values())[:4]:
            p["team_id"] = 99
        squad = [universe[i] for i in legal_starting_squad(universe)]
        club_counts: dict[int, int] = {}
        for p in squad:
            club_counts[p["team_id"]] = club_counts.get(p["team_id"], 0) + 1
        if max(club_counts.values()) <= 3:
            for p in squad[:4]:
                p["team_id"] = 42
        result = validator.validate_squad(squad, bank=20.0)
        assert any(v.rule == "club_limit" for v in result.violations)

    def test_rejects_over_budget(self, universe):
        squad = [universe[i] for i in legal_starting_squad(universe)]
        for p in squad:
            p["now_cost"] = 15.0
        result = validator.validate_squad(squad, bank=0.0)
        assert any(v.rule == "budget" for v in result.violations)

    def test_rejects_negative_bank(self, universe):
        squad = [universe[i] for i in legal_starting_squad(universe)]
        assert not validator.validate_squad(squad, bank=-1.0).legal

    def test_rejects_duplicate_players(self, universe):
        ids = legal_starting_squad(universe)
        squad = [universe[i] for i in ids[:-1]] + [universe[ids[0]]]
        result = validator.validate_squad(squad, bank=20.0)
        assert any(v.rule == "duplicate_players" for v in result.violations)

    # -- formation ---------------------------------------------------------
    def _xi(self, universe, gk, df, md, fw):
        pick = []
        for pos, n in (("GKP", gk), ("DEF", df), ("MID", md), ("FWD", fw)):
            pool = [p for p in universe.values() if p["position"] == pos]
            pick.extend(pool[:n])
        return pick

    @pytest.mark.parametrize("shape,legal", [
        ((1, 3, 5, 2), True),
        ((1, 4, 4, 2), True),
        ((1, 5, 3, 2), True),
        ((1, 3, 4, 3), True),
        ((1, 5, 4, 1), True),
        ((1, 2, 6, 2), False),   # only 2 DEF
        ((1, 6, 3, 1), False),   # 6 DEF
        ((1, 4, 1, 5), False),   # 1 MID, 5 FWD
        ((1, 4, 6, 0), False),   # no forward
        ((2, 3, 4, 2), False),   # two keepers
    ])
    def test_formation_legality(self, universe, shape, legal):
        xi = self._xi(universe, *shape)
        squad = xi + [p for p in universe.values() if p not in xi][:15 - len(xi)]
        result = validator.validate_lineup(xi, squad)
        formation_ok = not any(v.rule == "formation" for v in result.violations)
        assert formation_ok is legal, f"{shape} -> {result.report()}"

    def test_rejects_starter_outside_the_squad(self, universe):
        ids = legal_starting_squad(universe)
        squad = [universe[i] for i in ids]
        xi = self._xi(universe, 1, 4, 4, 2)
        outsider = next(p for p in universe.values() if p["id"] not in ids)
        xi = xi[:-1] + [outsider]
        result = validator.validate_lineup(xi, squad)
        assert any(v.rule == "xi_subset" for v in result.violations)

    def test_rejects_captain_not_in_xi(self, universe):
        squad = [universe[i] for i in legal_starting_squad(universe)]
        xi = self._xi(universe, 1, 4, 4, 2)
        result = validator.validate_lineup(xi, squad, captain_id=99999)
        assert any(v.rule == "captain" for v in result.violations)

    # -- transfer economics -----------------------------------------------
    @pytest.mark.parametrize("made,ft,chip,expect_hits", [
        (0, 1, None, 0),
        (1, 1, None, 0),
        (2, 1, None, 1),
        (3, 1, None, 2),
        (5, 5, None, 0),
        (6, 5, None, 1),
        (12, 1, "wildcard", 0),
        (11, 2, "freehit", 0),
        (2, 1, "benchboost", 1),
    ])
    def test_transfer_cost_arithmetic(self, made, ft, chip, expect_hits):
        ok = validator.validate_transfers(
            transfers_made=made, free_transfers=ft,
            hits_charged=expect_hits, chip=chip)
        assert ok.legal, ok.report()

        wrong = validator.validate_transfers(
            transfers_made=made, free_transfers=ft,
            hits_charged=expect_hits + 1, chip=chip)
        assert not wrong.legal, "validator accepted the wrong hit count"

    def test_rejects_ft_outside_bounds(self):
        assert not validator.validate_transfers(
            transfers_made=0, free_transfers=9, hits_charged=0).legal
        assert not validator.validate_transfers(
            transfers_made=0, free_transfers=-1, hits_charged=0).legal


# ==========================================================================
# T-SOLV-06 end to end: every solver path must pass the validator
# ==========================================================================
class TestSolverOutputIsLegal:
    @pytest.mark.parametrize("profile", [CONSERVATIVE, AGGRESSIVE])
    def test_path_passes_independent_validation(self, ctx, profile):
        path = solver.solve(ctx, profile, time_limit=25)
        assert path.status == "Optimal", path.status
        result = validator.validate_path(path, ctx.players)
        assert result.legal, result.report()

    def test_all_three_routes_pass_validation(self, ctx):
        for path in solver.three_routes(ctx, time_limit=25, k=12):
            if path.status != "Optimal":
                continue
            result = validator.validate_path(path, ctx.players)
            assert result.legal, f"{path.profile}: {result.report()}"

    def test_validator_catches_a_tampered_path(self, ctx):
        """Proof the end-to-end check can fail: corrupt a legal path."""
        path = solver.solve(ctx, CONSERVATIVE, time_limit=25)
        assert validator.validate_path(path, ctx.players).legal

        # Buy a player already owned, selling nobody -> 16-man squad.
        from fpl_assistant.strategy.solver import Move
        outsider = next(p for p in ctx.players
                        if p not in path.initial_squad)
        path.steps[0].moves.append(
            Move(player_out=path.initial_squad[0], player_in=outsider))
        path.steps[0].moves.append(
            Move(player_out=path.initial_squad[0], player_in=outsider))

        assert not validator.validate_path(path, ctx.players).legal

    def test_squad_stays_legal_at_every_intermediate_gameweek(self, ctx):
        """An illegal mid-path squad must not hide behind a legal final one."""
        path = solver.solve(ctx, AGGRESSIVE, time_limit=25)
        if path.status != "Optimal":
            pytest.skip("model not optimal in this configuration")
        result = validator.validate_path(path, ctx.players)
        gws_checked = {v.gw for v in result.violations}
        assert result.legal, f"violations at {gws_checked}: {result.report()}"


# ==========================================================================
# Selling price
# ==========================================================================
class TestSellPrice:
    @pytest.mark.parametrize("purchase,now,expected", [
        (10.0, 10.0, 10.0),   # no change
        (10.0, 10.1, 10.0),   # +0.1 -> profit rounds down to 0
        (10.0, 10.2, 10.1),   # +0.2 -> keep 0.1
        (10.0, 10.5, 10.2),   # +0.5 -> keep 0.2
        (10.0, 11.0, 10.5),   # +1.0 -> keep 0.5
        (10.0, 9.5, 9.5),     # a loss is taken in full
    ])
    def test_fifty_percent_profit_rule(self, db, purchase, now, expected):
        db.execute("INSERT INTO players(id, web_name, now_cost) VALUES (1, 'X', ?)",
                   (now,))
        db.execute(
            """INSERT INTO my_picks(gw, player_id, position, multiplier,
                                    purchase_price) VALUES (5, 1, 1, 1, ?)""",
            (purchase,))
        db.commit()
        prices = solver.sell_prices(db, 5)
        assert prices[1] == pytest.approx(expected, abs=0.051)
