"""Transfer sandbox: an in-memory what-if over the stored squad.

The page this serves is a scenario modeller -- swap players in and out, turn a
chip on, and read the expected-points consequence -- and none of that may touch
`my_picks`. What you actually own is the baseline every retrospective is scored
against; a sandbox that wrote to it would destroy the comparison it exists to
make. So every mutation here returns a NEW `SandboxState` and the caller (the
Streamlit page) keeps it in session state. SQLite is touched only by
`save_scenario`, and only into the `scenario` tables, which nothing in the
ingest, projection or calibration path reads.

Legality is delegated to `strategy.validator`, not reimplemented. That module
deliberately shares no code with the solver so it can catch the solver being
wrong; a third copy of the rules living here would defeat the same purpose.

Chips change the arithmetic rather than the rules:

* wildcard / free_hit -- transfer cost is zero, however many you make.
* bench_boost         -- the bench scores, so the squad's xP is all 15.
* triple_captain      -- the captain's multiplier goes from 2 to 3.

Only the hit and the scoring change. A Free Hit squad still has to be a legal
15 inside the budget, which is why chips are applied at `impact()` and never as
an exemption inside `swap()`.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field, replace

from ..rules import ELEMENT_TYPE_TO_POS, load_rules
from ..strategy import validator as validator_mod
from ..ui.pitch import XI_SIZE, PitchPlayer, formation_string

CHIPS = ("wildcard", "free_hit", "bench_boost", "triple_captain")
CHIP_LABELS = {
    None: "None",
    "wildcard": "\U0001F0CF Wildcard",
    "free_hit": "\U0001F39F Free Hit",
    "bench_boost": "\U0001F680 Bench Boost",
    "triple_captain": "\U0001F451 Triple Captain",
}
# Chips that make transfers free. Both also leave banked FTs alone, which is
# why `hits` never consults `free_transfers` when one of these is active.
FREE_TRANSFER_CHIPS = ("wildcard", "free_hit")

HIT_COST = 4          # points per transfer beyond the free allowance


@dataclass(frozen=True)
class Candidate:
    """A player who could be transferred in. Mirrors the roster-card fields."""

    player_id: int
    name: str
    position: str
    team: str
    team_id: int
    cost: float                 # what it costs to buy, i.e. `now_cost`
    xp: float = 0.0
    form: float = 0.0
    ownership: float = 0.0
    net_transfers: int = 0      # v_net: transfers_in_event - transfers_out_event
    next_fdr: int | None = None
    next_opponent: str = ""
    badges: list[str] = field(default_factory=list)
    status: str = "a"


@dataclass
class Transfer:
    out_id: int
    in_id: int
    out_name: str
    in_name: str
    sold_for: float
    bought_for: float

    @property
    def net_spend(self) -> float:
        return self.bought_for - self.sold_for


@dataclass
class SandboxState:
    """The whole what-if. Immutable-by-convention: mutators return a new one."""

    gw: int
    squad: list[PitchPlayer]
    bank: float = 0.0
    free_transfers: int = 1
    chip: str | None = None
    transfers: list[Transfer] = field(default_factory=list)
    selected_id: int | None = None          # the pitch node awaiting a swap
    # Sell prices are fixed at the moment the sandbox opens. FPL prices a sale
    # from what you PAID, not from today's list price, so recomputing them as
    # the sandbox mutates would let a player bought inside the sandbox be sold
    # back at a profit that does not exist.
    sell_prices: dict[int, float] = field(default_factory=dict)
    baseline_xi_xp: float = 0.0
    baseline_squad_xp: float = 0.0
    run_id: str | None = None

    # -- derived views -----------------------------------------------------
    @property
    def starters(self) -> list[PitchPlayer]:
        return [p for p in self.squad if p.starting]

    @property
    def bench(self) -> list[PitchPlayer]:
        return sorted((p for p in self.squad if not p.starting),
                      key=lambda p: p.bench_order)

    @property
    def captain(self) -> PitchPlayer | None:
        return next((p for p in self.squad if p.is_captain), None)

    @property
    def formation(self) -> str:
        return formation_string(self.starters)

    @property
    def squad_value(self) -> float:
        return sum(p.cost for p in self.squad)

    @property
    def dirty(self) -> bool:
        return bool(self.transfers) or self.chip is not None

    def sell_price(self, player_id: int) -> float:
        """What this player would sell for. Falls back to list price."""
        by_id = {p.player_id: p for p in self.squad}
        player = by_id.get(player_id)
        return self.sell_prices.get(player_id,
                                    player.cost if player else 0.0)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def captain_multiplier(chip: str | None) -> int:
    return 3 if chip == "triple_captain" else 2


def scoring_xp(state: SandboxState) -> float:
    """Expected points for the scenario under its active chip.

    The captain is counted at their multiplier and everyone else at 1, so this
    is the number the impact bar compares -- not a bare sum of xP, which would
    price a Triple Captain identically to no chip at all.
    """
    scorers = (state.squad if state.chip == "bench_boost" else state.starters)
    captain = state.captain
    total = 0.0
    for player in scorers:
        multiplier = 1.0
        if captain is not None and player.player_id == captain.player_id:
            multiplier = float(captain_multiplier(state.chip))
        total += player.xp * multiplier
    return total


def baseline_xp(state: SandboxState) -> float:
    """The same measure applied to the squad as stored.

    Bench Boost and Triple Captain are scenario choices, so the baseline is
    read under the SAME chip. Otherwise turning on Bench Boost would report a
    +20 "gain" that is really just the bench being counted on one side of the
    subtraction and not the other.
    """
    if state.chip == "bench_boost":
        base = state.baseline_squad_xp
    else:
        base = state.baseline_xi_xp
    # The captain's extra multiple is already inside the stored baselines at
    # 2x; a Triple Captain adds one more captain's worth on top.
    if state.chip == "triple_captain":
        captain = state.captain
        if captain is not None:
            base += captain.xp
    return base


def hits(state: SandboxState) -> int:
    """Points deducted for this scenario's transfers."""
    if state.chip in FREE_TRANSFER_CHIPS:
        return 0
    chargeable = max(0, len(state.transfers) - state.free_transfers)
    return chargeable * HIT_COST


@dataclass
class Impact:
    """Everything the metrics bar shows."""

    transfers: int
    hits: int
    free_used: int
    bank: float
    squad_value: float
    baseline_xp: float
    scenario_xp: float
    chip: str | None

    @property
    def xp_delta(self) -> float:
        return self.scenario_xp - self.baseline_xp

    @property
    def net_ev(self) -> float:
        """Net EV = (scenario xP - baseline xP) - transfer hits."""
        return self.xp_delta - self.hits


def impact(state: SandboxState) -> Impact:
    return Impact(
        transfers=len(state.transfers),
        hits=hits(state),
        free_used=min(len(state.transfers), state.free_transfers),
        bank=state.bank,
        squad_value=state.squad_value,
        baseline_xp=baseline_xp(state),
        scenario_xp=scoring_xp(state),
        chip=state.chip,
    )


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------
@dataclass
class SwapOutcome:
    ok: bool
    state: SandboxState | None = None
    reason: str = ""


def _as_validator_rows(squad: list[PitchPlayer],
                       team_ids: dict[int, int]) -> list[dict]:
    """Shape the pitch model into what `strategy.validator` expects."""
    return [{"id": p.player_id, "position": p.position,
             "team_id": team_ids.get(p.player_id, -1),
             "now_cost": p.cost} for p in squad]


def transfer_in(state: SandboxState, candidate: Candidate,
                team_ids: dict[int, int]) -> SwapOutcome:
    """Replace the currently selected player with `candidate`.

    Every structural rule is checked against the PROPOSED squad by
    `validator.validate_squad`, so a refusal names the rule it broke rather
    than failing somewhere downstream with a confusing number.
    """
    if state.selected_id is None:
        return SwapOutcome(False, reason="select a player on the pitch first")

    by_id = {p.player_id: p for p in state.squad}
    outgoing = by_id.get(state.selected_id)
    if outgoing is None:
        return SwapOutcome(False, reason="the selected player is not in the squad")
    if candidate.player_id in by_id:
        return SwapOutcome(
            False, reason=f"{candidate.name} is already in this squad")
    if candidate.position != outgoing.position:
        return SwapOutcome(
            False,
            reason=(f"{candidate.name} is a {candidate.position} and "
                    f"{outgoing.name} is a {outgoing.position} -- FPL only "
                    "allows like-for-like within a squad slot"))

    sold_for = state.sell_price(outgoing.player_id)
    new_bank = state.bank + sold_for - candidate.cost
    if new_bank < -1e-6:
        return SwapOutcome(
            False,
            reason=(f"£{abs(new_bank):.1f}m short: selling {outgoing.name} "
                    f"raises £{sold_for:.1f}m against £{candidate.cost:.1f}m "
                    f"for {candidate.name}"))

    incoming = PitchPlayer(
        player_id=candidate.player_id, name=candidate.name,
        position=candidate.position, team=candidate.team,
        cost=candidate.cost, starting=outgoing.starting,
        multiplier=outgoing.multiplier,
        is_captain=outgoing.is_captain, is_vice=outgoing.is_vice,
        bench_order=outgoing.bench_order, xp=candidate.xp,
        next_fdr=candidate.next_fdr, next_opponent=candidate.next_opponent,
        badges=list(candidate.badges), status=candidate.status)

    proposed = [incoming if p.player_id == outgoing.player_id else p
                for p in state.squad]

    team_map = {**team_ids, candidate.player_id: candidate.team_id}
    check = validator_mod.validate_squad(
        _as_validator_rows(proposed, team_map), bank=new_bank)
    if not check.legal:
        return SwapOutcome(False, reason="; ".join(
            v.detail for v in check.violations))

    # A transfer chain that returns to a player already sold this session is
    # one transfer, not two -- recording it twice would charge a second hit for
    # undoing a mistake.
    chain = [t for t in state.transfers if t.in_id != outgoing.player_id]
    if len(chain) == len(state.transfers):
        chain = list(state.transfers) + [Transfer(
            out_id=outgoing.player_id, in_id=candidate.player_id,
            out_name=outgoing.name, in_name=candidate.name,
            sold_for=sold_for, bought_for=candidate.cost)]
    else:
        original = next(t for t in state.transfers
                        if t.in_id == outgoing.player_id)
        if original.out_id != candidate.player_id:
            chain = chain + [Transfer(
                out_id=original.out_id, in_id=candidate.player_id,
                out_name=original.out_name, in_name=candidate.name,
                sold_for=original.sold_for, bought_for=candidate.cost)]

    new_sell = {**state.sell_prices, candidate.player_id: candidate.cost}
    return SwapOutcome(True, state=replace(
        state, squad=proposed, bank=round(new_bank, 2), transfers=chain,
        selected_id=None, sell_prices=new_sell))


def set_chip(state: SandboxState, chip: str | None) -> SandboxState:
    if chip is not None and chip not in CHIPS:
        raise ValueError(f"unknown chip: {chip}")
    return replace(state, chip=chip)


def select(state: SandboxState, player_id: int | None) -> SandboxState:
    """Toggle the pitch selection. Clicking the selected player deselects it."""
    if player_id is not None and player_id == state.selected_id:
        return replace(state, selected_id=None)
    return replace(state, selected_id=player_id)


def set_captain(state: SandboxState, player_id: int) -> SandboxState:
    """Move the armband, demoting the previous captain."""
    squad = []
    for player in state.squad:
        if player.player_id == player_id:
            squad.append(replace(player, is_captain=True, is_vice=False))
        else:
            squad.append(replace(player, is_captain=False))
    return replace(state, squad=squad)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _latest_xp(conn: sqlite3.Connection) -> tuple[dict[int, float], str | None]:
    row = conn.execute(
        "SELECT run_id FROM xp_projection ORDER BY computed_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {}, None
    run_id = row["run_id"] if hasattr(row, "keys") else row[0]
    gw_row = conn.execute(
        "SELECT MIN(gw) FROM xp_projection WHERE run_id = ?", (run_id,)
    ).fetchone()
    gw = gw_row[0]
    return ({int(r[0]): float(r[1] or 0.0) for r in conn.execute(
        "SELECT player_id, xp_total FROM xp_projection "
        "WHERE run_id = ? AND gw = ?", (run_id, gw))}, run_id)


def open_sandbox(conn: sqlite3.Connection, gw: int, *,
                 free_transfers: int = 1) -> SandboxState:
    """Build the starting state from the stored squad. Never writes."""
    from ..models import arbitrage as arbitrage_mod
    from ..strategy import solver as solver_mod
    from ..ui import pitch as pitch_mod

    xp_by_player, run_id = _latest_xp(conn)
    ids = [int(r[0]) for r in conn.execute(
        "SELECT player_id FROM my_picks WHERE gw = ?", (gw,))]
    badges = arbitrage_mod.badges_for(conn, ids) if ids else {}
    squad = pitch_mod.load_squad(conn, gw, xp_by_player=xp_by_player,
                                 badges=badges)

    try:
        sell = solver_mod.sell_prices(conn, gw)
    except sqlite3.Error:
        sell = {}
    sell = {int(k): float(v) for k, v in sell.items()}
    for player in squad:
        sell.setdefault(player.player_id, player.cost)

    bank = _bank(conn, gw, squad, sell)
    starters = [p for p in squad if p.starting]
    captain = next((p for p in squad if p.is_captain), None)
    captain_bonus = captain.xp if captain is not None else 0.0

    return SandboxState(
        gw=gw, squad=squad, bank=bank, free_transfers=free_transfers,
        sell_prices=sell,
        # Baselines carry the standard 2x captain, so a scenario that only
        # moves the armband is still compared like for like.
        baseline_xi_xp=sum(p.xp for p in starters) + captain_bonus,
        baseline_squad_xp=sum(p.xp for p in squad) + captain_bonus,
        run_id=run_id)


def _bank(conn: sqlite3.Connection, gw: int, squad: list[PitchPlayer],
          sell: dict[int, float]) -> float:
    """Money in the bank: budget minus what the squad is worth to sell.

    FPL reports this directly on `/entry/`, which this app does not ingest, so
    it is derived from the rulebook budget. A squad assembled before a price
    rise is worth more than the budget, which legitimately yields a small
    positive bank rather than an error.
    """
    budget = float(load_rules()["squad"]["budget"])
    spent = sum(sell.get(p.player_id, p.cost) for p in squad)
    return round(max(0.0, budget - spent), 1)


def candidates(conn: sqlite3.Connection, *, exclude: set[int] | None = None,
               limit: int = 400) -> list[Candidate]:
    """Every buyable player, with the fields the roster cards sort on."""
    from ..ui import pitch as pitch_mod

    xp_by_player, _run = _latest_xp(conn)
    exclude = exclude or set()
    gw_row = conn.execute("SELECT MAX(gw) FROM player_gw").fetchone()
    next_gw = int(gw_row[0] or 0) + 1
    fixtures = pitch_mod._next_fixtures(conn, next_gw)

    out: list[Candidate] = []
    for r in conn.execute(
        """SELECT p.id, p.web_name, p.element_type, p.team_id, p.now_cost,
                  p.form, p.selected_by_percent, p.status,
                  p.transfers_in_event, p.transfers_out_event,
                  t.short_name AS team
           FROM players p LEFT JOIN teams t ON t.id = p.team_id"""
    ):
        pid = int(r["id"])
        if pid in exclude:
            continue
        fdr, opponent = fixtures.get(r["team_id"], (None, ""))
        out.append(Candidate(
            player_id=pid, name=r["web_name"] or "",
            position=ELEMENT_TYPE_TO_POS.get(r["element_type"], "MID"),
            team=r["team"] or "", team_id=int(r["team_id"] or -1),
            cost=float(r["now_cost"] or 0.0),
            xp=float(xp_by_player.get(pid, 0.0)),
            form=float(r["form"] or 0.0),
            ownership=float(r["selected_by_percent"] or 0.0),
            net_transfers=int(r["transfers_in_event"] or 0)
                          - int(r["transfers_out_event"] or 0),
            next_fdr=fdr, next_opponent=opponent,
            status=r["status"] or "a"))
    return out[:limit] if limit else out


SORTS: dict[str, tuple[str, bool]] = {
    "Projected xP": ("xp", True),
    "Price velocity": ("net_transfers", True),
    "Fixture ease": ("next_fdr", False),
    "Ownership %": ("ownership", True),
    "Form": ("form", True),
    "Price": ("cost", True),
}


def filter_candidates(pool: list[Candidate], *, query: str = "",
                      position: str = "ALL", max_price: float | None = None,
                      sort: str = "Projected xP") -> list[Candidate]:
    """Search, filter and sort the roster pool. Pure; trivially testable."""
    needle = query.strip().lower()
    rows = [
        c for c in pool
        if (position in ("ALL", "") or c.position == position)
        and (max_price is None or c.cost <= max_price + 1e-6)
        and (not needle
             or needle in c.name.lower() or needle in c.team.lower())
    ]
    key, descending = SORTS.get(sort, SORTS["Projected xP"])
    # A missing FDR sorts last either way rather than ahead of every real one.
    def sort_key(c: Candidate):
        value = getattr(c, key)
        if value is None:
            return float("-inf") if descending else float("inf")
        return value
    return sorted(rows, key=sort_key, reverse=descending)


# --------------------------------------------------------------------------
# Persistence -- the only code here that writes
# --------------------------------------------------------------------------
def save_scenario(conn: sqlite3.Connection, state: SandboxState,
                  name: str) -> int:
    """Persist a scenario and return its id. Touches only the scenario tables."""
    metrics = impact(state)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO scenario
             (name, gw, chip, bank, free_transfers, transfers, hit_points,
              baseline_xp, scenario_xp, net_ev, run_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name.strip() or f"GW{state.gw} scenario", state.gw, state.chip,
         state.bank, state.free_transfers, metrics.transfers, metrics.hits,
         round(metrics.baseline_xp, 2), round(metrics.scenario_xp, 2),
         round(metrics.net_ev, 2), state.run_id, now))
    scenario_id = int(cur.lastrowid)

    conn.executemany(
        """INSERT INTO scenario_pick
             (scenario_id, player_id, position, starting, bench_order,
              is_captain, is_vice, cost, sell_price, xp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(scenario_id, p.player_id, p.position, 1 if p.starting else 0,
          p.bench_order, 1 if p.is_captain else 0, 1 if p.is_vice else 0,
          p.cost, state.sell_price(p.player_id), p.xp) for p in state.squad])
    conn.commit()
    return scenario_id


def list_scenarios(conn: sqlite3.Connection, gw: int | None = None,
                   limit: int = 20) -> list[dict]:
    sql = ("SELECT scenario_id, name, gw, chip, transfers, hit_points, "
           "net_ev, created_at FROM scenario")
    params: list = []
    if gw is not None:
        sql += " WHERE gw = ?"
        params.append(gw)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def delete_scenario(conn: sqlite3.Connection, scenario_id: int) -> None:
    conn.execute("DELETE FROM scenario_pick WHERE scenario_id = ?",
                 (scenario_id,))
    conn.execute("DELETE FROM scenario WHERE scenario_id = ?", (scenario_id,))
    conn.commit()


def load_scenario(conn: sqlite3.Connection, scenario_id: int,
                  base: SandboxState) -> SandboxState:
    """Rebuild a saved scenario's squad on top of a freshly opened sandbox."""
    head = conn.execute(
        "SELECT * FROM scenario WHERE scenario_id = ?", (scenario_id,)
    ).fetchone()
    if head is None:
        raise LookupError(f"no scenario {scenario_id}")

    rows = conn.execute(
        "SELECT * FROM scenario_pick WHERE scenario_id = ?", (scenario_id,)
    ).fetchall()
    known = {p.player_id: p for p in base.squad}
    lookup = {c.player_id: c for c in candidates(conn, limit=0)}

    squad: list[PitchPlayer] = []
    for r in rows:
        pid = int(r["player_id"])
        if pid in known:
            player = known[pid]
        elif pid in lookup:
            c = lookup[pid]
            player = PitchPlayer(player_id=pid, name=c.name,
                                 position=c.position, team=c.team,
                                 cost=c.cost, xp=c.xp, next_fdr=c.next_fdr,
                                 next_opponent=c.next_opponent,
                                 badges=list(c.badges), status=c.status)
        else:
            continue
        squad.append(replace(
            player, starting=bool(r["starting"]),
            bench_order=int(r["bench_order"] or 0),
            is_captain=bool(r["is_captain"]), is_vice=bool(r["is_vice"]),
            multiplier=(2.0 if r["is_captain"] else
                        (1.0 if r["starting"] else 0.0))))

    return replace(base, squad=squad, chip=head["chip"],
                   bank=float(head["bank"] or 0.0),
                   free_transfers=int(head["free_transfers"] or 1),
                   selected_id=None)


__all__ = [
    "CHIPS", "CHIP_LABELS", "HIT_COST", "XI_SIZE", "Candidate", "Impact",
    "SandboxState", "SwapOutcome", "Transfer", "baseline_xp", "candidates",
    "captain_multiplier", "delete_scenario", "filter_candidates", "hits",
    "impact", "list_scenarios", "load_scenario", "open_sandbox",
    "save_scenario", "scoring_xp", "select", "set_captain", "set_chip",
    "transfer_in",
]
