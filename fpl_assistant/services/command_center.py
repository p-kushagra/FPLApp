"""Page 2 view-model: strategic command center.

Assembles the solver context, runs the three routes, ranks prescriptive moves
and builds the captaincy matrix and chip horizon. No Streamlit imports.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import temporal
from ..rules import load_rules
from ..strategy import captaincy as cap_mod
from ..strategy import eo as eo_mod
from ..strategy import solver as solver_mod
from ..strategy.captaincy import CaptainOption, RegimeCall
from ..strategy.solver import SolverPath
from .degrade import DataQuality


@dataclass
class MoveSuggestion:
    player_out: int
    out_name: str
    out_team: str
    out_cost: float
    player_in: int
    in_name: str
    in_team: str
    in_cost: float
    position: str
    xp_delta: float
    cost_delta: float
    ileo: float = 0.0
    rationale: str = ""

    @property
    def affordable_note(self) -> str:
        if self.cost_delta <= 0:
            return f"frees {abs(self.cost_delta):.1f}m"
        return f"costs {self.cost_delta:.1f}m"


@dataclass
class ChipWindow:
    gw: int
    kind: str                    # normal | double | blank | mixed
    fixtures: int
    squad_playing: int
    squad_total: int
    blank_teams: list[str] = field(default_factory=list)
    double_teams: list[str] = field(default_factory=list)
    projected: bool = False

    @property
    def coverage(self) -> str:
        return f"{self.squad_playing}/{self.squad_total}"


@dataclass
class ChipRecommendation:
    chip: str
    target_gw: int | None
    action: str                  # play | hold
    reason: str
    confidence: str


@dataclass
class CommandCenterVM:
    state: temporal.GWState
    quality: DataQuality
    window: list[int]
    free_transfers: int
    bank: float
    team_value: float
    squad_size: int
    routes: list[SolverPath] = field(default_factory=list)
    moves: list[MoveSuggestion] = field(default_factory=list)
    captains: list[CaptainOption] = field(default_factory=list)
    regime: RegimeCall | None = None
    captain_pick: CaptainOption | None = None
    captain_reason: str = ""
    horizon: list[ChipWindow] = field(default_factory=list)
    chips: list[ChipRecommendation] = field(default_factory=list)
    chips_available: list[str] = field(default_factory=list)
    projected_disruption: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def recommended_route(self) -> SolverPath | None:
        """Highest net xP among routes that actually solved."""
        solved = [r for r in self.routes if r.status == "Optimal"]
        return max(solved, key=lambda r: r.net_xp) if solved else None

    @property
    def has_squad(self) -> bool:
        return self.squad_size > 0


# --------------------------------------------------------------------------
def load_xp(conn: sqlite3.Connection, gws: list[int]
            ) -> dict[tuple[int, int], float]:
    """Projections for the window, latest run per gameweek."""
    if not gws:
        return {}
    marks = ",".join("?" * len(gws))
    rows = conn.execute(
        f"""SELECT xp.player_id, xp.gw, xp.xp_total FROM xp_projection xp
            JOIN (SELECT gw, run_id FROM xp_projection
                  WHERE gw IN ({marks})
                  GROUP BY gw
                  HAVING computed_at = MAX(computed_at)) latest
              ON latest.gw = xp.gw AND latest.run_id = xp.run_id""",
        gws,
    ).fetchall()
    return {(r["player_id"], r["gw"]): float(r["xp_total"] or 0.0) for r in rows}


def current_squad(conn: sqlite3.Connection) -> tuple[list[int], int | None]:
    row = conn.execute("SELECT MAX(gw) g FROM my_picks").fetchone()
    if not row or row["g"] is None:
        return [], None
    gw = int(row["g"])
    ids = [r["player_id"] for r in conn.execute(
        "SELECT player_id FROM my_picks WHERE gw = ?", (gw,))]
    return ids, gw


def _team_value(conn: sqlite3.Connection, squad: list[int]) -> float:
    if not squad:
        return 0.0
    marks = ",".join("?" * len(squad))
    row = conn.execute(
        f"SELECT COALESCE(SUM(now_cost), 0) v FROM players WHERE id IN ({marks})",
        squad,
    ).fetchone()
    return round(float(row["v"] or 0.0), 1)


def top_moves(conn: sqlite3.Connection, ctx: solver_mod.SolverContext,
              ileo: dict[int, float] | None = None,
              limit: int = 10) -> list[MoveSuggestion]:
    """Rank every affordable like-for-like swap by horizon xP gain.

    Independent of the solver: this is the "what single move helps most" view,
    which is a different question from "what sequence is optimal". Both are
    shown, because they disagree often and the disagreement is informative.
    """
    ileo = ileo or {}
    meta = {
        r["id"]: dict(r) for r in conn.execute(
            """SELECT p.id, p.web_name, p.position, p.element_type, p.now_cost,
                      t.short_name AS team_short
               FROM players p LEFT JOIN teams t ON t.id = p.team_id""")
    }

    suggestions: list[MoveSuggestion] = []
    squad = set(ctx.initial_squad)

    for out_id in squad:
        out = meta.get(out_id)
        if not out:
            continue
        out_pos = ctx.position(out_id)
        out_xp = sum(ctx.points(out_id, gw) for gw in ctx.gws)
        budget = ctx.sale_value(out_id) + ctx.initial_bank

        for in_id, cand in meta.items():
            if in_id in squad or ctx.position(in_id) != out_pos:
                continue
            price = float(cand.get("now_cost") or 0.0)
            if price > budget + 1e-6:
                continue
            gain = sum(ctx.points(in_id, gw) for gw in ctx.gws) - out_xp
            if gain <= 0:
                continue
            suggestions.append(MoveSuggestion(
                player_out=out_id, out_name=out.get("web_name") or "?",
                out_team=out.get("team_short") or "?",
                out_cost=float(out.get("now_cost") or 0.0),
                player_in=in_id, in_name=cand.get("web_name") or "?",
                in_team=cand.get("team_short") or "?", in_cost=price,
                position=out_pos,
                xp_delta=round(gain, 2),
                cost_delta=round(price - ctx.sale_value(out_id), 1),
                ileo=round(ileo.get(in_id, 0.0), 3),
                rationale=f"+{gain:.1f} xP over {len(ctx.gws)} GW",
            ))

    suggestions.sort(key=lambda m: (-m.xp_delta, m.cost_delta, m.player_in))

    # One suggestion per outgoing player: ten variations on selling the same
    # defender is a list of one idea, not ten.
    seen: set[int] = set()
    unique: list[MoveSuggestion] = []
    for s in suggestions:
        if s.player_out in seen:
            continue
        seen.add(s.player_out)
        unique.append(s)
        if len(unique) >= limit:
            break
    return unique


def chip_horizon(conn: sqlite3.Connection, cfg, from_gw: int,
                 horizon: int = 12) -> list[ChipWindow]:
    """Gameweek shape plus this squad's coverage of it."""
    from ..planner import gameweek_shape

    squad, _ = current_squad(conn)
    squad_teams: dict[int, int] = {}
    if squad:
        marks = ",".join("?" * len(squad))
        for r in conn.execute(
            f"SELECT team_id, COUNT(*) n FROM players WHERE id IN ({marks}) "
            "GROUP BY team_id", squad):
            squad_teams[r["team_id"]] = int(r["n"])

    out: list[ChipWindow] = []
    for shape in gameweek_shape(conn, from_gw=from_gw, horizon=horizon):
        counts = shape.get("counts") or {}
        playing = sum(n for tid, n in squad_teams.items() if counts.get(tid, 0) >= 1)
        out.append(ChipWindow(
            gw=shape["gw"], kind=shape["kind"], fixtures=shape["fixtures"],
            squad_playing=playing, squad_total=len(squad),
            blank_teams=shape.get("blank_teams") or [],
            double_teams=shape.get("double_teams") or [],
        ))
    return out


def chip_recommendations(conn: sqlite3.Connection, cfg
                         ) -> tuple[list[ChipRecommendation], list[dict]]:
    """Delegate to the v1 chip planner, which is deliberately conservative.

    `chip_plan` returns a WRAPPER -- the per-chip advice is under "plan", and
    the siblings carry confirmed and projected disruption. Returns the advice
    plus the projected (not yet confirmed) blanks, which the UI must label as
    calendar-derived rather than fixture-confirmed.
    """
    from ..planner import chip_plan

    payload = chip_plan(conn, cfg=cfg, horizon=12) or {}
    advice = payload.get("plan") or {}
    projected = payload.get("projected") or []

    recs = [
        ChipRecommendation(
            chip=name,
            target_gw=detail.get("target_gw"),
            action=detail.get("action", "hold"),
            reason=detail.get("reason", ""),
            confidence=detail.get("confidence", "low"),
        )
        for name, detail in advice.items()
        if isinstance(detail, dict)
    ]
    return recs, projected


def build(conn: sqlite3.Connection, cfg, quality: DataQuality,
          horizon: int = 5, rival_ids: list[int] | None = None,
          deficit: int = 0, gameweeks_left: int = 20,
          run_solver: bool = False, time_limit: int = 30,
          candidates_k: int = 25) -> CommandCenterVM:
    """Assemble the Page 2 view-model.

    `run_solver` is opt-in: the ILP takes seconds, so the page renders
    everything else first and solves only when asked. Each panel fails soft.
    """
    state = temporal.gw_state(conn)
    window = state.planning_window(horizon)
    squad, _squad_gw = current_squad(conn)
    rules = load_rules()

    # An explicit rival set wins; otherwise fall back to the one saved on the
    # Leagues & Rivals page. Without this the captaincy matrix silently showed
    # a rival captain EO of 0% for everyone -- a Shield/Sword call computed
    # against an empty field, which looks like data but is the absence of it.
    if rival_ids is None:
        try:
            from .. import leagues as leagues_mod
            rival_ids = leagues_mod.rival_ids(conn) or None
        except sqlite3.Error:
            rival_ids = None

    bank_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'bank'").fetchone()
    bank = float(bank_row["value"]) if bank_row and bank_row["value"] else 0.0

    vm = CommandCenterVM(
        state=state, quality=quality, window=window,
        free_transfers=temporal.read_ft_bank(conn, window[0]).available,
        bank=bank,
        team_value=_team_value(conn, squad),
        squad_size=len(squad),
        chips_available=[r["chip"] for r in conn.execute(
            "SELECT chip FROM chip_state WHERE available = 1")]
        or list(rules["chips"]["available"]),
    )

    xp = load_xp(conn, window)
    ileo = {}
    if rival_ids:
        try:
            ileo = eo_mod.ileo(conn, state.scoring_gw, rival_ids)
        except sqlite3.Error as exc:
            vm.errors.append(f"ILEO unavailable: {exc}")

    ctx = None
    if squad and xp:
        try:
            ctx = solver_mod.build_context(
                conn, window, xp, initial_squad=squad, bank=bank,
                free_transfers=vm.free_transfers, ileo=ileo)
        except (sqlite3.Error, KeyError) as exc:
            vm.errors.append(f"Solver context unavailable: {exc}")

    if ctx is not None:
        try:
            vm.moves = top_moves(conn, ctx, ileo=ileo, limit=10)
        except (sqlite3.Error, KeyError) as exc:
            vm.errors.append(f"Transfer ranking unavailable: {exc}")

        if run_solver:
            try:
                vm.routes = solver_mod.three_routes(
                    ctx, time_limit=time_limit, k=candidates_k)
            except Exception as exc:  # noqa: BLE001 - a solver crash must not kill the page
                vm.errors.append(f"Solver failed: {type(exc).__name__}: {exc}")

    # -- captaincy --------------------------------------------------------
    try:
        cap_ileo = (eo_mod.captain_ileo(conn, state.scoring_gw, rival_ids)
                    if rival_ids else {})
        pool = squad if squad else None
        vm.captains = cap_mod.matrix(conn, window[0], candidate_ids=pool,
                                     ileo_cap=cap_ileo, limit=20)
        vm.regime = cap_mod.regime(deficit, gameweeks_left)
        vm.captain_pick, vm.captain_reason = cap_mod.recommend(vm.captains, vm.regime)
    except sqlite3.Error as exc:
        vm.errors.append(f"Captaincy matrix unavailable: {exc}")

    # -- chips ------------------------------------------------------------
    try:
        vm.horizon = chip_horizon(conn, cfg, from_gw=window[0], horizon=12)
    except Exception as exc:  # noqa: BLE001 - planner touches YAML and dates
        vm.errors.append(f"Chip horizon unavailable: {type(exc).__name__}: {exc}")

    try:
        vm.chips, vm.projected_disruption = chip_recommendations(conn, cfg)
    except Exception as exc:  # noqa: BLE001
        vm.errors.append(f"Chip plan unavailable: {type(exc).__name__}: {exc}")

    return vm
