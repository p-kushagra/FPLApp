"""Page 1 view-model: gameweek performance and mini-league benchmark.

No Streamlit imports. Returns plain dataclasses the page renders (ADR-001), so
this is unit-testable without a browser and reusable by a future HTTP adapter.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import temporal
from ..models import snapshot as snapshot_mod
from ..models import xp as xp_model
from ..strategy import eo as eo_mod
from ..strategy.eo import SwingMatrix
from .degrade import DataQuality


@dataclass
class Kpis:
    gw: int
    my_points: int = 0
    average_points: int = 0
    vs_average: int = 0
    league_rank: int | None = None
    league_size: int | None = None
    rank_delta: int | None = None
    lead_delta: float | None = None
    xp_total: float = 0.0
    luck_index: float = 0.0
    players_played: int = 0
    players_total: int = 0

    @property
    def luck_label(self) -> str:
        if self.luck_index > 3:
            return "fortunate"
        if self.luck_index < -3:
            return "unlucky"
        return "as deserved"


@dataclass
class VarianceRow:
    player_id: int
    web_name: str
    team_short: str
    position: str
    multiplier: float
    actual: float
    xp: float
    process: float
    luck: float
    attacking_xp: float = 0.0
    defensive_xp: float = 0.0
    bonus_xp: float = 0.0
    minutes: int = 0
    source: str = "fpl_baseline"

    @property
    def verdict(self) -> str:
        """The quadrant. The (+process, -luck) cell is the buy signal."""
        if self.process >= 0 and self.luck >= 0:
            return "Deserved haul"
        if self.process < 0 <= self.luck:
            return "Fortunate"
        if self.process >= 0 > self.luck:
            return "Unlucky"
        return "Genuinely poor"

    @property
    def action(self) -> str:
        return {
            "Deserved haul": "Hold",
            "Fortunate": "Sell candidate",
            "Unlucky": "BUY candidate",
            "Genuinely poor": "Sell",
        }[self.verdict]


@dataclass
class GWSummaryVM:
    state: temporal.GWState
    quality: DataQuality
    gw: int
    kpis: Kpis
    swing: SwingMatrix | None = None
    variance: list[VarianceRow] = field(default_factory=list)
    template: list[dict] = field(default_factory=list)
    bench: list[dict] = field(default_factory=list)
    rival_options: list[dict] = field(default_factory=list)
    selected_rivals: list[int] = field(default_factory=list)
    variance_mode: str = "luck_only"   # full | luck_only
    snapshot_meta: dict | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def variance_caveat(self) -> str | None:
        """Process needs a projection stored BEFORE the gameweek was played.

        Recomputing one now would read history that already contains the
        gameweek, so 'what we expected' would silently include what happened.
        When no prior projection exists the luck axis is still sound -- it is
        actual points against what the realised xG/xA deserved -- but the
        process axis is not, and the page must say so rather than plot a zero
        as though it meant something.
        """
        if self.variance_mode == "full":
            return None
        return ("No pre-deadline projection was frozen for this gameweek, so "
                "only the luck axis (actual vs underlying) is meaningful. "
                "Process compares the underlying numbers against what was "
                "forecast **before kickoff**; recomputing that forecast now "
                "would read the result it is meant to be judged against. "
                "The snapshot job freezes it at deadline minus one hour, so "
                "the full two-axis view becomes available from the next "
                "gameweek it runs for.")

    @property
    def buy_candidates(self) -> list[VarianceRow]:
        rows = [r for r in self.variance if r.verdict == "Unlucky"]
        return sorted(rows, key=lambda r: r.luck)

    @property
    def sell_candidates(self) -> list[VarianceRow]:
        return sorted((r for r in self.variance if r.verdict == "Fortunate"),
                      key=lambda r: -r.luck)

    @property
    def has_squad(self) -> bool:
        return self.kpis.players_total > 0


# --------------------------------------------------------------------------
def _latest_squad_gw(conn: sqlite3.Connection, target: int) -> int | None:
    row = conn.execute(
        "SELECT MAX(gw) g FROM my_picks WHERE gw <= ?", (target,)
    ).fetchone()
    return int(row["g"]) if row and row["g"] is not None else None


def _kpis(conn: sqlite3.Connection, gw: int, squad_gw: int | None) -> Kpis:
    kpis = Kpis(gw=gw)
    if squad_gw is None:
        return kpis

    rows = conn.execute(
        """SELECT mp.player_id, mp.multiplier,
                  COALESCE(pg.total_points, 0) AS pts,
                  COALESCE(pg.minutes, 0) AS mins
           FROM my_picks mp
           LEFT JOIN player_gw pg
             ON pg.player_id = mp.player_id AND pg.gw = ?
           WHERE mp.gw = ?""",
        (gw, squad_gw),
    ).fetchall()

    kpis.players_total = len(rows)
    kpis.my_points = sum(int(r["pts"]) * int(r["multiplier"] or 0) for r in rows)
    kpis.players_played = sum(1 for r in rows
                              if int(r["multiplier"] or 0) > 0 and int(r["mins"]) > 0)

    avg = conn.execute(
        "SELECT average_score FROM gw_state WHERE gw = ?", (gw,)
    ).fetchone()
    if avg and avg["average_score"]:
        kpis.average_points = int(avg["average_score"])
        kpis.vs_average = kpis.my_points - kpis.average_points

    xp_rows = conn.execute(
        """SELECT mp.multiplier, xp.xp_total FROM my_picks mp
           JOIN xp_projection xp
             ON xp.player_id = mp.player_id AND xp.gw = ?
           WHERE mp.gw = ?
             AND xp.run_id = (SELECT run_id FROM xp_projection WHERE gw = ?
                              ORDER BY computed_at DESC LIMIT 1)""",
        (gw, squad_gw, gw),
    ).fetchall()
    kpis.xp_total = round(
        sum(float(r["xp_total"] or 0) * int(r["multiplier"] or 0) for r in xp_rows), 1)
    if kpis.xp_total:
        kpis.luck_index = round(kpis.my_points - kpis.xp_total, 1)

    return kpis


def _variance(conn: sqlite3.Connection, gw: int, squad_gw: int | None,
              squad_only: bool = True,
              frozen: bool | None = None) -> list[VarianceRow]:
    """Decompose actual points into process and luck.

    `xp` here is the pre-gameweek projection. The split is:
        process = what the underlying numbers say they deserved, minus forecast
        luck    = what they actually scored, minus what they deserved

    With no per-fixture underlying data ingested yet, the deserved figure falls
    back to the projection itself, so `process` reads 0 and the whole surprise
    lands in `luck`. That is honest rather than invented, and the source column
    says which regime each row is in.
    """
    sql = """SELECT p.id, p.web_name, p.position, t.short_name AS team_short,
                    COALESCE(pg.total_points, 0) AS actual,
                    COALESCE(pg.minutes, 0) AS minutes,
                    COALESCE(pg.goals_scored, 0) AS goals,
                    COALESCE(pg.assists, 0) AS assists,
                    COALESCE(pg.expected_goals, 0) AS xg,
                    COALESCE(pg.expected_assists, 0) AS xa,
                    COALESCE(pg.bonus, 0) AS bonus,
                    COALESCE(pg.clean_sheets, 0) AS cs,
                    xp.xp_total, xp.xp_goals, xp.xp_assists, xp.xp_clean_sheet,
                    xp.xp_saves, xp.xp_defcon, xp.xp_bonus, xp.source,
                    COALESCE(mp.multiplier, 0) AS multiplier
             FROM players p
             LEFT JOIN teams t ON t.id = p.team_id
             LEFT JOIN player_gw pg ON pg.player_id = p.id AND pg.gw = ?
             {xp_join}
             LEFT JOIN mp_scope mp ON mp.player_id = p.id
             WHERE pg.player_id IS NOT NULL"""

    # The Process axis is only meaningful against a forecast made BEFORE
    # kickoff. `projection_snapshot` is that forecast, frozen an hour before
    # the deadline and never rewritten. `xp_projection` is not: `recompute_xp`
    # overwrites it continuously, so for a played gameweek it has already seen
    # the result and subtracting it would measure nothing. Hence the join swaps
    # entirely rather than falling back -- a post-hoc projection is not a
    # degraded pre-kickoff one, it is a different quantity.
    if frozen is None:
        frozen = snapshot_mod.has_snapshot(conn, gw)

    if frozen:
        xp_join = ("LEFT JOIN projection_snapshot xp "
                   "ON xp.player_id = p.id AND xp.gw = ?")
        xp_params = [gw]
    else:
        # A typed all-NULL row so the SELECT list still resolves. Every xp
        # column reads NULL, which the loop below already treats as "no
        # pre-kickoff forecast" and collapses to the luck-only regime.
        xp_join = (
            "LEFT JOIN (SELECT NULL xp_total, NULL xp_goals, NULL xp_assists, "
            "NULL xp_clean_sheet, NULL xp_saves, NULL xp_defcon, "
            "NULL xp_bonus, NULL source) xp ON 0")
        xp_params = []
    sql = sql.format(xp_join=xp_join)

    scope = ("WITH mp_scope AS (SELECT player_id, multiplier FROM my_picks "
             "WHERE gw = ?) ") if squad_gw is not None else \
            ("WITH mp_scope AS (SELECT NULL AS player_id, 0 AS multiplier) ")
    params: list = [squad_gw] if squad_gw is not None else []
    params += [gw] + xp_params

    if squad_only and squad_gw is not None:
        sql += " AND mp.player_id IS NOT NULL"

    rows = conn.execute(scope + sql, params).fetchall()

    out: list[VarianceRow] = []
    for r in rows:
        xp_total = float(r["xp_total"]) if r["xp_total"] is not None else 0.0
        actual = float(r["actual"])

        # Underlying-deserved points from realised xG/xA, valued at the same
        # per-position rates the projection used.
        goal_pts = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}.get(r["position"], 4)
        underlying = (float(r["xg"]) * goal_pts + float(r["xa"]) * 3
                      + float(r["bonus"]) + (2 if int(r["minutes"]) >= 60 else
                                             1 if int(r["minutes"]) > 0 else 0))
        if r["position"] in ("GKP", "DEF"):
            underlying += 4 * float(r["cs"])

        process = round(underlying - xp_total, 2) if xp_total else 0.0
        luck = round(actual - underlying, 2)

        attacking = (float(r["xp_goals"] or 0) + float(r["xp_assists"] or 0))
        defensive = (float(r["xp_clean_sheet"] or 0) + float(r["xp_saves"] or 0)
                     + float(r["xp_defcon"] or 0))

        out.append(VarianceRow(
            player_id=r["id"], web_name=r["web_name"] or "?",
            team_short=r["team_short"] or "?", position=r["position"] or "?",
            multiplier=float(r["multiplier"] or 0),
            actual=actual, xp=round(xp_total, 2),
            process=process, luck=luck,
            attacking_xp=round(attacking, 2),
            defensive_xp=round(defensive, 2),
            bonus_xp=round(float(r["xp_bonus"] or 0), 2),
            minutes=int(r["minutes"]),
            source=r["source"] or "fpl_baseline",
        ))
    return out


def rival_options(conn: sqlite3.Connection, league_id: int | None = None
                  ) -> list[dict]:
    """Selectable rivals from the stored standings."""
    sql = """SELECT DISTINCT entry_id, player_name, entry_name, rank, total
             FROM league_standing"""
    params: list = []
    if league_id:
        sql += " WHERE league_id = ?"
        params.append(league_id)
    sql += " ORDER BY rank"
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.Error:
        return []


def build(conn: sqlite3.Connection, cfg, quality: DataQuality,
          gw: int | None = None, rival_ids: list[int] | None = None,
          league_id: int = 0, squad_only: bool = True) -> GWSummaryVM:
    """Assemble the Page 1 view-model. Individual panels fail soft."""
    state = temporal.gw_state(conn)
    target = gw or state.scoring_gw
    squad_gw = _latest_squad_gw(conn, target)

    vm = GWSummaryVM(
        state=state, quality=quality, gw=target,
        kpis=Kpis(gw=target),
        selected_rivals=list(rival_ids or []),
    )

    try:
        vm.kpis = _kpis(conn, target, squad_gw)
    except sqlite3.Error as exc:
        vm.errors.append(f"KPIs unavailable: {exc}")

    try:
        # Mode is decided by whether a pre-kickoff snapshot exists, NOT by
        # whether any xP value happens to be non-zero. The old test would have
        # reported "full" as soon as `recompute_xp` had touched a played
        # gameweek -- which is exactly the leaked projection the Process axis
        # must never be built on.
        frozen = snapshot_mod.has_snapshot(conn, target)
        vm.variance = _variance(conn, target, squad_gw, squad_only,
                                frozen=frozen)
        vm.variance_mode = "full" if frozen else "luck_only"
        vm.snapshot_meta = snapshot_mod.snapshot_meta(conn, target)
    except sqlite3.Error as exc:
        vm.errors.append(f"Variance unavailable: {exc}")

    try:
        vm.rival_options = rival_options(conn, league_id or None)
    except sqlite3.Error as exc:
        vm.errors.append(f"Rival list unavailable: {exc}")

    if rival_ids:
        try:
            vm.swing = eo_mod.swing_matrix(conn, target, rival_ids,
                                           league_id=league_id)
        except sqlite3.Error as exc:
            vm.errors.append(f"ILEO matrix unavailable: {exc}")

    try:
        vm.template = [dict(r) for r in conn.execute(
            """SELECT o.ownership_pct, o.captain_pct, p.web_name, p.position,
                      p.id AS player_id, t.short_name AS team_short,
                      p.selected_by_percent AS overall_own
               FROM top_owned o
               JOIN players p ON p.id = o.player_id
               LEFT JOIN teams t ON t.id = p.team_id
               WHERE o.gw = (SELECT MAX(gw) FROM top_owned)
               ORDER BY o.ownership_pct DESC LIMIT 30""")]
    except sqlite3.Error as exc:
        vm.errors.append(f"Template unavailable: {exc}")

    if squad_gw is not None:
        try:
            vm.bench = [dict(r) for r in conn.execute(
                """SELECT p.web_name, p.position, mp.position AS slot,
                          COALESCE(pg.total_points, 0) AS pts,
                          COALESCE(pg.minutes, 0) AS minutes
                   FROM my_picks mp
                   JOIN players p ON p.id = mp.player_id
                   LEFT JOIN player_gw pg
                     ON pg.player_id = mp.player_id AND pg.gw = ?
                   WHERE mp.gw = ? AND mp.multiplier = 0
                   ORDER BY mp.position""", (target, squad_gw))]
        except sqlite3.Error as exc:
            vm.errors.append(f"Bench unavailable: {exc}")

    return vm


def ensure_projections(conn: sqlite3.Connection, gws: list[int],
                       understat_ok: bool = True) -> int:
    """Compute projections for `gws` if none exist. Returns rows written."""
    results = xp_model.project(conn, gws, understat_ok=understat_ok, persist=True)
    return len(results)
