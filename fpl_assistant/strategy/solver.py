"""Rolling-horizon Integer Linear Program for transfer planning.

Greedy "best xP delta" ranking cannot express "take a -4 now so the squad is
shaped for a Bench Boost in three weeks". That is a multi-period problem with
coupled budget, club-limit, formation and free-transfer constraints, so it is
modelled as an ILP and solved exactly.

Tractability comes from pruning (ADR-003): the full universe is ~700 players x
5 weeks x 5 variable families, about 17.5k binaries, which CBC will not close in
interactive time. Restricted to the incumbent 15 plus the top-K per position it
is ~200 players and ~5k binaries, which solves to optimality in seconds.

The free-transfer block (C11-C13) is the subtlest part and is written out
explicitly below, because getting it wrong produces plans that are illegal in a
way nothing else would catch.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import pulp

from ..rules import ELEMENT_TYPE_TO_POS, load_rules
from ..temporal import FTBank, project_ft

log = logging.getLogger(__name__)

BIG_M = 15  # a whole squad; safe upper bound on transfers in one gameweek

DEFAULT_HORIZON = 5
DEFAULT_CANDIDATES = 40
DEFAULT_TIME_LIMIT = 30
DEFAULT_GAP = 0.01


# --------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    """A parameterisation of the same model. Three of these = three routes."""

    key: str
    label: str
    max_hits: int = 0
    gamma: float = 0.90          # per-GW discount
    bench: float = 0.10          # bench weight
    terminal_ft: float = 1.5     # value of each banked FT at the horizon end
    differential: float = 0.0    # bonus on low-ILEO starters
    chip: str | None = None      # chip this route is routing toward
    chip_gw: int | None = None


CONSERVATIVE = Profile("conservative", "Conservative - FT building",
                       max_hits=0, gamma=0.95, terminal_ft=3.0)
AGGRESSIVE = Profile("aggressive", "Aggressive - form chasing",
                     max_hits=1, gamma=0.75, terminal_ft=0.5, differential=1.2)
CHIP_SETUP = Profile("chip_setup", "Chip enabler",
                     max_hits=1, gamma=0.90, terminal_ft=1.0)


@dataclass
class SolverContext:
    """Everything the model needs, already resolved to plain data."""

    players: dict[int, dict]              # id -> {position, team_id, now_cost, ...}
    xp: dict[tuple[int, int], float]      # (player_id, gw) -> projected points
    gws: list[int]
    initial_squad: list[int]
    initial_bank: float
    initial_ft: int
    sell_price: dict[int, float] = field(default_factory=dict)
    ileo: dict[int, float] = field(default_factory=dict)
    chips_available: set[str] = field(default_factory=set)
    rules: dict = field(default_factory=load_rules)

    def points(self, pid: int, gw: int) -> float:
        return float(self.xp.get((pid, gw), 0.0))

    def price(self, pid: int) -> float:
        return float(self.players[pid].get("now_cost") or 0.0)

    def sale_value(self, pid: int) -> float:
        return float(self.sell_price.get(pid, self.price(pid)))

    def position(self, pid: int) -> str:
        p = self.players[pid]
        etype = p.get("element_type")
        return (p.get("position")
                or (ELEMENT_TYPE_TO_POS.get(etype, "MID") if etype is not None
                    else "MID"))


@dataclass
class Move:
    player_out: int
    player_in: int
    cost_delta: float = 0.0
    xp_delta: float = 0.0
    rationale: str = ""


@dataclass
class Step:
    gw: int
    moves: list[Move] = field(default_factory=list)
    hits: int = 0
    chip: str | None = None
    ft_before: int = 0
    ft_after: int = 0
    bank_after: float = 0.0
    xi: list[int] = field(default_factory=list)
    captain: int | None = None
    gw_xp: float = 0.0

    @property
    def is_roll(self) -> bool:
        return not self.moves


@dataclass
class SolverPath:
    profile: str
    label: str
    steps: list[Step] = field(default_factory=list)
    initial_squad: list[int] = field(default_factory=list)
    initial_bank: float = 0.0
    initial_ft: int = 1
    total_xp: float = 0.0
    total_hits: int = 0
    status: str = "Unknown"
    objective: float = 0.0
    mip_gap: float = 0.0
    wall_seconds: float = 0.0
    relaxations: list[str] = field(default_factory=list)
    candidate_count: int = 0
    variable_count: int = 0
    constraint_count: int = 0
    chip_used: str | None = None
    chip_gw: int | None = None

    @property
    def net_xp(self) -> float:
        return round(self.total_xp - 4.0 * self.total_hits, 2)

    @property
    def end_ft(self) -> int:
        return self.steps[-1].ft_after if self.steps else self.initial_ft

    @property
    def end_bank(self) -> float:
        return self.steps[-1].bank_after if self.steps else self.initial_bank

    def summary(self) -> str:
        parts = []
        for s in self.steps:
            if s.chip:
                parts.append(f"GW{s.gw}: {s.chip.upper()}")
            elif s.is_roll:
                parts.append(f"GW{s.gw}: roll (FT->{s.ft_after})")
            else:
                moves = ", ".join(f"{m.player_out}->{m.player_in}" for m in s.moves)
                hit = f" -{4 * s.hits}" if s.hits else ""
                parts.append(f"GW{s.gw}: {moves}{hit}")
        return " | ".join(parts)


# --------------------------------------------------------------------------
# Candidate pruning
# --------------------------------------------------------------------------
def candidate_set(ctx: SolverContext, k: int = DEFAULT_CANDIDATES) -> list[int]:
    """Incumbent squad plus the top-k per position by horizon xP.

    The solver is optimal over this set, not over the league, so the pruning is
    part of the model's correctness -- which is why the count is surfaced in the
    UI as "considering N candidates".
    """
    incumbent = set(ctx.initial_squad)
    by_pos: dict[str, list[tuple[float, int]]] = {}

    for pid in ctx.players:
        total = sum(ctx.points(pid, gw) for gw in ctx.gws)
        by_pos.setdefault(ctx.position(pid), []).append((total, pid))

    chosen = set(incumbent)
    for rows in by_pos.values():
        # Deterministic ordering: xP desc, then id asc. Two runs on identical
        # data must produce identical paths or the audit trail is worthless.
        rows.sort(key=lambda r: (-r[0], r[1]))
        chosen.update(pid for _, pid in rows[:k])

    return sorted(chosen)


# --------------------------------------------------------------------------
# Model construction
# --------------------------------------------------------------------------
def build_model(ctx: SolverContext, profile: Profile,
                candidates: list[int]) -> tuple[pulp.LpProblem, dict]:
    """Build the ILP. Returns (problem, variable registry)."""
    r = ctx.rules
    sq = r["squad"]
    tr = r["transfers"]

    f_max = int(tr["max_banked"])
    per_gw = int(tr["free_per_gw"])
    hit_cost = float(tr["hit_cost"])
    chip_retains = bool(tr["chip_retains_ft"])
    chip_accrues = bool(tr["chip_accrues_ft"])

    P = candidates
    T = list(range(len(ctx.gws)))          # 0-based period index
    gw_of = {t: ctx.gws[t] for t in T}

    prob = pulp.LpProblem(f"fpl_{profile.key}", pulp.LpMaximize)

    # -- decision variables ------------------------------------------------
    x = pulp.LpVariable.dicts("x", (P, T), cat="Binary")   # in squad
    y = pulp.LpVariable.dicts("y", (P, T), cat="Binary")   # in XI
    c = pulp.LpVariable.dicts("c", (P, T), cat="Binary")   # captain
    b = pulp.LpVariable.dicts("b", (P, T), cat="Binary")   # transferred in
    s = pulp.LpVariable.dicts("s", (P, T), cat="Binary")   # transferred out

    f = pulp.LpVariable.dicts("f", range(len(T) + 1), lowBound=0, upBound=f_max,
                              cat="Integer")               # FT bank entering t
    q = pulp.LpVariable.dicts("q", T, lowBound=0, upBound=f_max, cat="Integer")
    h = pulp.LpVariable.dicts("h", T, lowBound=0, upBound=BIG_M, cat="Integer")
    z = pulp.LpVariable.dicts("z", T, cat="Binary")         # linearises min()
    money = pulp.LpVariable.dicts("money", range(len(T) + 1), lowBound=0, cat="Continuous")

    # Chip activation. Only a squad chip (wildcard) participates in the chain;
    # Free Hit does not persist, so it is solved separately (see free_hit_value).
    use_chip = bool(profile.chip == "wildcard" and "wildcard" in ctx.chips_available)
    u = pulp.LpVariable.dicts("u", T, cat="Binary")
    if not use_chip:
        for t in T:
            prob += u[t] == 0, f"no_chip_{t}"

    # -- objective ---------------------------------------------------------
    obj = []
    for t in T:
        discount = profile.gamma ** t
        gw = gw_of[t]
        for p in P:
            xp = ctx.points(p, gw)
            if xp == 0:
                continue
            # A captain adds a SECOND copy of xP: exactly the doubling rule,
            # and it keeps the objective linear.
            obj.append(discount * xp * (y[p][t] + c[p][t]))
            # Bench players carry option value via auto-subs, not full value.
            obj.append(discount * profile.bench * xp * (x[p][t] - y[p][t]))
            if profile.differential:
                edge = profile.differential * xp * (1.0 - ctx.ileo.get(p, 0.0))
                obj.append(discount * edge * y[p][t] * 0.1)

    obj.append(-hit_cost * pulp.lpSum(h[t] for t in T))
    obj.append(profile.terminal_ft * f[len(T)])
    prob += pulp.lpSum(obj)

    # -- C1/C2 squad size and positional quota ----------------------------
    for t in T:
        prob += pulp.lpSum(x[p][t] for p in P) == int(sq["size"]), f"squad_size_{t}"
        for pos, want in sq["quota"].items():
            members = [p for p in P if ctx.position(p) == pos]
            prob += pulp.lpSum(x[p][t] for p in members) == int(want), \
                f"quota_{pos}_{t}"

    # -- C3/C4/C5 starting XI and formation -------------------------------
    for t in T:
        prob += pulp.lpSum(y[p][t] for p in P) == 11, f"xi_size_{t}"
        for p in P:
            prob += y[p][t] <= x[p][t], f"xi_subset_{p}_{t}"
        for pos, (lo, hi) in sq["formation"].items():
            members = [p for p in P if ctx.position(p) == pos]
            prob += pulp.lpSum(y[p][t] for p in members) >= int(lo), f"form_lo_{pos}_{t}"
            prob += pulp.lpSum(y[p][t] for p in members) <= int(hi), f"form_hi_{pos}_{t}"

    # -- C6 captaincy ------------------------------------------------------
    for t in T:
        prob += pulp.lpSum(c[p][t] for p in P) == 1, f"one_captain_{t}"
        for p in P:
            prob += c[p][t] <= y[p][t], f"captain_starts_{p}_{t}"

    # -- C7 club limit -----------------------------------------------------
    clubs = {ctx.players[p].get("team_id") for p in P}
    for t in T:
        for club in clubs:
            members = [p for p in P if ctx.players[p].get("team_id") == club]
            prob += pulp.lpSum(x[p][t] for p in members) <= int(sq["max_per_club"]), \
                f"club_{club}_{t}"

    # -- C8/C9 continuity from the incumbent squad -------------------------
    incumbent = set(ctx.initial_squad)
    for p in P:
        for t in T:
            prev = x[p][t - 1] if t > 0 else (1 if p in incumbent else 0)
            prob += x[p][t] == prev + b[p][t] - s[p][t], f"cont_{p}_{t}"
            prob += b[p][t] + s[p][t] <= 1, f"nochurn_{p}_{t}"

    # -- C10 budget --------------------------------------------------------
    prob += money[0] == ctx.initial_bank, "bank_init"
    for t in T:
        proceeds = pulp.lpSum(ctx.sale_value(p) * s[p][t] for p in P)
        spend = pulp.lpSum(ctx.price(p) * b[p][t] for p in P)
        prob += money[t + 1] == money[t] + proceeds - spend, f"bank_{t}"

    # -- C11/C12/C13 the free-transfer block -------------------------------
    #
    # This is the block that must be right. With chip_retains_ft and
    # chip_accrues_ft read from config:
    #
    #   normal week:  q = T - h,  q <= f,  f' = min(F, f - q + 1)
    #   chip week:    q = 0, h = 0,        f' = min(F, f + accrual)
    #
    # accrual is 0 this season (chip_accrues_ft: false), so a chip week freezes
    # the bank exactly rather than banking a transfer.
    prob += f[0] == ctx.initial_ft, "ft_init"
    for t in T:
        transfers_in = pulp.lpSum(b[p][t] for p in P)

        # C11 - hits are the overflow past the bank; a chip suppresses them.
        prob += h[t] >= transfers_in - f[t] - BIG_M * u[t], f"hits_lb_{t}"
        if chip_retains:
            prob += h[t] <= BIG_M * (1 - u[t]), f"hits_chip_{t}"

        # C12 - FTs consumed are the transfers not paid for by hits.
        #
        # This MUST be a big-M pair, not the equality `q == transfers_in - h`.
        # Under a chip both q and h are forced to 0, and an equality would then
        # read 0 == transfers_in, silently forbidding every transfer in the one
        # gameweek whose entire purpose is unlimited transfers. The wildcard
        # would solve to "Optimal" having made no moves at all.
        prob += q[t] >= transfers_in - h[t] - BIG_M * u[t], f"ft_used_lb_{t}"
        prob += q[t] <= transfers_in - h[t] + BIG_M * u[t], f"ft_used_ub_{t}"
        prob += q[t] <= f[t], f"ft_cap_{t}"
        if chip_retains:
            prob += q[t] <= BIG_M * (1 - u[t]), f"ft_chip_{t}"

        # C13 - f[t+1] = min(F_MAX, f[t] - q[t] + accrual_t), linearised.
        # accrual_t = per_gw normally; 0 in a chip week when chips do not accrue.
        accrual = per_gw if chip_accrues else per_gw * (1 - u[t])
        prob += f[t + 1] <= f_max, f"ftcap_hi_{t}"
        prob += f[t + 1] <= f[t] - q[t] + accrual, f"ftrec_hi_{t}"
        prob += f[t + 1] >= f[t] - q[t] + accrual - f_max * z[t], f"ftrec_lo_{t}"
        prob += f[t + 1] >= f_max - f_max * (1 - z[t]), f"ftcap_lo_{t}"

    # -- profile-level transfer budget ------------------------------------
    prob += pulp.lpSum(h[t] for t in T) <= profile.max_hits, "max_hits"

    # -- C14 chip budget ---------------------------------------------------
    if use_chip:
        prob += pulp.lpSum(u[t] for t in T) <= 1, "one_chip"
        if profile.chip_gw is not None and profile.chip_gw in ctx.gws:
            target = ctx.gws.index(profile.chip_gw)
            prob += u[target] == 1, "chip_target"

    # -- C15 availability --------------------------------------------------
    # A player with zero projected points across the whole horizon is either
    # injured or has no fixtures; never force one into the squad.
    for p in P:
        if all(ctx.points(p, gw) <= 0 for gw in ctx.gws) and p not in incumbent:
            for t in T:
                prob += x[p][t] == 0, f"unavailable_{p}_{t}"

    registry = {"x": x, "y": y, "c": c, "b": b, "s": s,
                "f": f, "q": q, "h": h, "u": u, "money": money,
                "P": P, "T": T, "gw_of": gw_of}
    return prob, registry


# --------------------------------------------------------------------------
# Solving
# --------------------------------------------------------------------------
def _extract(ctx: SolverContext, profile: Profile, prob: pulp.LpProblem,
             reg: dict, wall: float) -> SolverPath:
    P, T, gw_of = reg["P"], reg["T"], reg["gw_of"]
    y, c, b, s = reg["y"], reg["c"], reg["b"], reg["s"]
    f, h, u, money = reg["f"], reg["h"], reg["u"], reg["money"]

    def on(var) -> bool:
        return var.value() is not None and var.value() > 0.5

    path = SolverPath(
        profile=profile.key, label=profile.label,
        initial_squad=list(ctx.initial_squad),
        initial_bank=ctx.initial_bank, initial_ft=ctx.initial_ft,
        status=pulp.LpStatus[prob.status],
        objective=round(pulp.value(prob.objective) or 0.0, 3),
        wall_seconds=round(wall, 2),
        candidate_count=len(P),
        variable_count=len(prob.variables()),
        constraint_count=len(prob.constraints),
    )

    for t in T:
        gw = gw_of[t]
        outs = [p for p in P if on(s[p][t])]
        ins = [p for p in P if on(b[p][t])]
        xi = [p for p in P if on(y[p][t])]
        captain = next((p for p in P if on(c[p][t])), None)

        moves = []
        for i, pid_in in enumerate(ins):
            pid_out = outs[i] if i < len(outs) else None
            if pid_out is None:
                continue
            moves.append(Move(
                player_out=pid_out,
                player_in=pid_in,
                cost_delta=round(ctx.price(pid_in) - ctx.sale_value(pid_out), 2),
                xp_delta=round(
                    sum(ctx.points(pid_in, g) - ctx.points(pid_out, g)
                        for g in ctx.gws), 2),
                rationale=_rationale(ctx, pid_in, pid_out),
            ))

        gw_xp = sum(ctx.points(p, gw) for p in xi)
        if captain is not None:
            gw_xp += ctx.points(captain, gw)

        step = Step(
            gw=gw, moves=moves,
            hits=round(h[t].value() or 0),
            chip="wildcard" if on(u[t]) else None,
            ft_before=round(f[t].value() or 0),
            ft_after=round(f[t + 1].value() or 0),
            bank_after=round(money[t + 1].value() or 0.0, 2),
            xi=sorted(xi), captain=captain,
            gw_xp=round(gw_xp, 2),
        )
        path.steps.append(step)
        path.total_xp += gw_xp
        path.total_hits += step.hits
        if step.chip:
            path.chip_used, path.chip_gw = step.chip, gw

    path.total_xp = round(path.total_xp, 2)
    return path


def _rationale(ctx: SolverContext, pid_in: int, pid_out: int) -> str:
    gain = sum(ctx.points(pid_in, g) - ctx.points(pid_out, g) for g in ctx.gws)
    price = ctx.price(pid_in) - ctx.sale_value(pid_out)
    bits = [f"+{gain:.1f} xP over {len(ctx.gws)} GW"]
    if abs(price) >= 0.1:
        bits.append(f"{'costs' if price > 0 else 'frees'} {abs(price):.1f}m")
    ileo_in = ctx.ileo.get(pid_in)
    if ileo_in is not None and ileo_in < 0.25:
        bits.append("differential")
    return "; ".join(bits)


def solve(ctx: SolverContext, profile: Profile,
          time_limit: int = DEFAULT_TIME_LIMIT,
          gap: float = DEFAULT_GAP,
          candidates: list[int] | None = None,
          k: int = DEFAULT_CANDIDATES) -> SolverPath:
    """Solve one profile. Never raises: an infeasible model returns a path
    carrying its status so the UI can explain rather than crash."""
    cand = candidates if candidates is not None else candidate_set(ctx, k)
    prob, reg = build_model(ctx, profile, cand)

    started = time.monotonic()
    try:
        solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit, gapRel=gap)
        prob.solve(solver)
    except Exception as exc:  # noqa: BLE001 - a solver crash must not kill a page
        log.warning("CBC failed for profile %s: %s", profile.key, exc)
        return SolverPath(profile=profile.key, label=profile.label,
                          status="SolverError", initial_squad=list(ctx.initial_squad),
                          initial_bank=ctx.initial_bank, initial_ft=ctx.initial_ft,
                          wall_seconds=round(time.monotonic() - started, 2))

    wall = time.monotonic() - started
    status = pulp.LpStatus[prob.status]

    if status not in ("Optimal", "Not Solved") or pulp.value(prob.objective) is None:
        return SolverPath(profile=profile.key, label=profile.label, status=status,
                          initial_squad=list(ctx.initial_squad),
                          initial_bank=ctx.initial_bank, initial_ft=ctx.initial_ft,
                          wall_seconds=round(wall, 2), candidate_count=len(cand))

    return _extract(ctx, profile, prob, reg, wall)


def solve_with_relaxation(ctx: SolverContext, profile: Profile,
                          ladder: list[str] | None = None,
                          **kw) -> SolverPath:
    """Solve, walking the relaxation ladder if the model is infeasible.

    Each relaxation is recorded on the path so the UI can say what it had to
    give up -- a recommendation the operator cannot interrogate is not
    prescriptive, it is just assertive.
    """
    ladder = ladder or ["drop_chip_shape", "allow_one_hit", "horizon_to_3"]
    applied: list[str] = []

    path = solve(ctx, profile, **kw)
    if path.status == "Optimal":
        path.relaxations = applied
        return path

    current = profile
    working = ctx

    for step in ladder:
        if step == "drop_chip_shape" and current.chip:
            current = Profile(**{**current.__dict__, "chip": None, "chip_gw": None})
        elif step == "allow_one_hit":
            current = Profile(**{**current.__dict__,
                                 "max_hits": current.max_hits + 1})
        elif step == "horizon_to_3" and len(working.gws) > 3:
            working = SolverContext(**{**working.__dict__, "gws": working.gws[:3]})
        else:
            continue

        applied.append(step)
        path = solve(working, current, **kw)
        if path.status == "Optimal":
            break

    path.relaxations = applied
    return path


# --------------------------------------------------------------------------
# The three routes
# --------------------------------------------------------------------------
def three_routes(ctx: SolverContext, chip_target: str | None = None,
                 chip_gw: int | None = None,
                 time_limit: int = DEFAULT_TIME_LIMIT,
                 k: int = DEFAULT_CANDIDATES) -> list[SolverPath]:
    """Conservative, Aggressive and Chip-enabler paths over one candidate set.

    Sharing the candidate set is not just an optimisation: it makes the three
    objectives directly comparable, which is the whole point of showing them
    side by side.
    """
    cand = candidate_set(ctx, k)

    chip_profile = Profile(
        **{**CHIP_SETUP.__dict__,
           "chip": chip_target or ("wildcard" if "wildcard" in ctx.chips_available else None),
           "chip_gw": chip_gw},
    )

    routes = []
    for profile in (CONSERVATIVE, AGGRESSIVE, chip_profile):
        routes.append(solve_with_relaxation(
            ctx, profile, candidates=cand, time_limit=time_limit))
    return routes


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def persist(conn: sqlite3.Connection, paths: list[SolverPath],
            anchor_gw: int) -> str:
    """Write a solve to solver_run / solver_path / solver_move. Returns run_id."""
    run_id = uuid.uuid4().hex[:12]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    lead = paths[0] if paths else None

    conn.execute(
        """INSERT INTO solver_run
             (run_id, anchor_gw, horizon, profile, candidate_count, variable_count,
              constraint_count, status, objective, mip_gap, wall_seconds,
              relaxations, ft_start, bank_start, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, anchor_gw, len(lead.steps) if lead else 0, "three_routes",
         lead.candidate_count if lead else 0, lead.variable_count if lead else 0,
         lead.constraint_count if lead else 0, lead.status if lead else "None",
         lead.objective if lead else 0.0, lead.mip_gap if lead else 0.0,
         sum(p.wall_seconds for p in paths),
         ",".join(sorted({r for p in paths for r in p.relaxations})),
         lead.initial_ft if lead else 0, lead.initial_bank if lead else 0.0, now),
    )

    for rank, path in enumerate(paths, start=1):
        conn.execute(
            """INSERT INTO solver_path
                 (run_id, path_rank, profile, label, total_xp, total_hits, net_xp,
                  end_ft, end_bank, end_team_value, path_variance, chip_used, chip_gw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, rank, path.profile, path.label, path.total_xp,
             path.total_hits, path.net_xp, path.end_ft, path.end_bank,
             0.0, 0.0, path.chip_used, path.chip_gw),
        )
        for step in path.steps:
            for i, mv in enumerate(step.moves):
                conn.execute(
                    """INSERT INTO solver_move
                         (run_id, path_rank, gw, move_index, player_out, player_in,
                          cost_delta, xp_delta, is_hit, rationale)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, rank, step.gw, i, mv.player_out, mv.player_in,
                     mv.cost_delta, mv.xp_delta, 1 if step.hits else 0, mv.rationale),
                )
    conn.commit()
    return run_id


# --------------------------------------------------------------------------
# Context assembly
# --------------------------------------------------------------------------
def sell_price(purchase: float, now_cost: float,
               share: float = 0.5) -> float:
    """FPL's 50%-profit selling rule, computed in integer tenths.

        sell = purchase + floor(profit_tenths * share)   [tenths of a million]

    Float arithmetic is wrong here and quietly so: 10.2 - 10.0 evaluates to
    0.19999999999999929, and `int(0.1999.../0.2)` floors to 0, losing a tenth on
    exactly the profit that should return one. FPL prices are integer tenths
    upstream, so the whole calculation stays in tenths.

    A loss is taken in FULL -- only profit is shared. A player who has dropped
    in price sells at the current price, not at what you paid; modelling that
    the other way round would let the solver fund transfers with money it does
    not have.
    """
    purchase_t = round(purchase * 10)
    now_t = round(now_cost * 10)
    if now_t <= purchase_t:
        return now_t / 10.0
    return (purchase_t + int((now_t - purchase_t) * share)) / 10.0


def sell_prices(conn: sqlite3.Connection, gw: int) -> dict[int, float]:
    """Selling value of every player in the squad for `gw`.

    FPL reports `selling_price` directly on the picks endpoint; that is
    authoritative and is used whenever present. The rule is only re-derived when
    it is missing (an un-ingested squad, or a hypothetical).
    """
    rules = load_rules()
    share = float(rules["squad"].get("sell_price_profit_share", 0.5))
    out: dict[int, float] = {}
    rows = conn.execute(
        """SELECT mp.player_id, mp.purchase_price, mp.selling_price, p.now_cost
           FROM my_picks mp JOIN players p ON p.id = mp.player_id
           WHERE mp.gw = ?""",
        (gw,),
    ).fetchall()
    for r in rows:
        if r["selling_price"] is not None:
            out[r["player_id"]] = float(r["selling_price"])
            continue
        now_cost = float(r["now_cost"] or 0.0)
        purchase = float(r["purchase_price"] if r["purchase_price"] is not None
                         else now_cost)
        out[r["player_id"]] = sell_price(purchase, now_cost, share)
    return out


def build_context(conn: sqlite3.Connection, gws: list[int],
                  xp: dict[tuple[int, int], float],
                  initial_squad: list[int] | None = None,
                  bank: float = 0.0, free_transfers: int | None = None,
                  ileo: dict[int, float] | None = None) -> SolverContext:
    """Assemble a SolverContext from the database."""
    from .. import temporal

    players = {
        r["id"]: dict(r) for r in conn.execute(
            """SELECT id, web_name, element_type, position, team_id, now_cost,
                      status, chance_of_playing_next_round
               FROM players"""
        )
    }

    if initial_squad is None:
        rows = conn.execute(
            "SELECT player_id FROM my_picks WHERE gw = (SELECT MAX(gw) FROM my_picks)"
        ).fetchall()
        initial_squad = [r["player_id"] for r in rows]

    if free_transfers is None:
        free_transfers = temporal.read_ft_bank(conn, gws[0]).available

    chips = {
        r["chip"] for r in conn.execute(
            "SELECT chip FROM chip_state WHERE available = 1"
        )
    }

    return SolverContext(
        players=players,
        xp=xp,
        gws=list(gws),
        initial_squad=list(initial_squad),
        initial_bank=float(bank),
        initial_ft=int(free_transfers),
        sell_price=sell_prices(conn, gws[0]) if initial_squad else {},
        ileo=ileo or {},
        chips_available=chips,
    )


def project_ft_after(ctx: SolverContext, transfers: int,
                     chip: str | None = None) -> int:
    """Bank after one gameweek. Shares `temporal.project_ft` with the validator
    so the model and the checker cannot drift apart on the recurrence."""
    return project_ft(FTBank(gw=ctx.gws[0], available=ctx.initial_ft),
                      transfers, chip, ctx.rules).available
