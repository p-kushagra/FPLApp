"""Schedule intelligence: FDR grid, rotation pairs and congestion warnings.

Consolidates what v1 split across a fixture planner and a rotation/congestion
page. They were always one question asked twice -- "who plays, and how hard is
it?" -- and separating them meant cross-referencing two screens to answer it.

The rotation-pair finder is the piece with real edge. Two £4.0-4.5m defenders
whose easy fixtures fall in opposite gameweeks occupy one squad slot's worth of
attention and one slot's worth of money, but deliver a startable player every
week. Finding them by eye across 20 teams and 8 gameweeks is exactly the sort of
combinatorial scan a person does badly and a loop does perfectly.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from itertools import combinations, pairwise

# A fixture at or below this difficulty is one you are happy to start.
GOOD_FDR = 3

# Rotation-pair budget band, in millions.
ROTATION_MIN_PRICE = 4.0
ROTATION_MAX_PRICE = 4.5

# Hours between two kickoffs below which fatigue-driven rotation is likely.
TIGHT_TURNAROUND_HOURS = 72

DEFAULT_HORIZON = 5
MIN_HORIZON = 3
MAX_HORIZON = 8


@dataclass
class FixtureCell:
    """One team's fixture in one gameweek, or a blank."""

    gw: int
    opponent: str = ""
    home: bool = True
    fdr: int | None = None
    kickoff: str | None = None

    @property
    def blank(self) -> bool:
        return self.fdr is None

    @property
    def label(self) -> str:
        if self.blank:
            return "-"
        return f"{self.opponent}{'(H)' if self.home else '(A)'}"


@dataclass
class TeamRow:
    """A team's fixture run across the horizon."""

    team_id: int
    team: str
    cells: list[FixtureCell] = field(default_factory=list)

    @property
    def mean_fdr(self) -> float:
        # Blanks score 5: having no fixture is the worst outcome for a run, and
        # averaging only the fixtures that exist would rank a blank as neutral.
        values = [c.fdr if c.fdr is not None else 5 for c in self.cells]
        return round(sum(values) / len(values), 2) if values else 5.0

    @property
    def doubles(self) -> int:
        return sum(1 for c in self.cells if c.fdr is not None and c.gw in
                   [x.gw for x in self.cells if x is not c and x.fdr is not None])

    @property
    def blanks(self) -> int:
        return sum(1 for c in self.cells if c.blank)

    @property
    def good_fixtures(self) -> int:
        return sum(1 for c in self.cells
                   if c.fdr is not None and c.fdr <= GOOD_FDR)


@dataclass
class RotationPair:
    """Two cheap assets whose good fixtures fall in opposite gameweeks."""

    player_a: str
    player_b: str
    player_a_id: int
    player_b_id: int
    team_a: str
    team_b: str
    position: str
    combined_cost: float
    covered_gws: int
    horizon: int
    mean_best_fdr: float
    schedule: list[dict] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return round(self.covered_gws / self.horizon, 2) if self.horizon else 0.0

    @property
    def verdict(self) -> str:
        if self.coverage >= 0.99:
            return "full cover"
        if self.coverage >= 0.8:
            return "strong"
        return "partial"


@dataclass
class CongestionWarning:
    """A team facing a fixture pile-up, and why."""

    team_id: int
    team: str
    gw: int
    matches: int
    turnaround_hours: float | None = None
    competitions: list[str] = field(default_factory=list)
    severity: str = "watch"
    note: str = ""


@dataclass
class ScheduleVM:
    horizon: int
    gws: list[int] = field(default_factory=list)
    rows: list[TeamRow] = field(default_factory=list)
    pairs: list[RotationPair] = field(default_factory=list)
    warnings: list[CongestionWarning] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def grid(self) -> tuple[list[str], list[int], list[list], list[list[str]]]:
        """`(teams, gws, difficulty grid, label grid)` for the heatmap."""
        teams = [r.team for r in self.rows]
        z = [[(c.fdr if c.fdr is not None else 5) for c in r.cells]
             for r in self.rows]
        labels = [[c.label for c in r.cells] for r in self.rows]
        return teams, self.gws, z, labels


# --------------------------------------------------------------------------
# Fixture grid
# --------------------------------------------------------------------------
def _parse(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def anchor_gw(conn: sqlite3.Connection) -> int:
    """The next gameweek with unplayed fixtures."""
    row = conn.execute(
        "SELECT MIN(event) FROM fixtures WHERE finished = 0 AND event IS NOT NULL"
    ).fetchone()
    if row and row[0]:
        return int(row[0])
    played = conn.execute("SELECT MAX(gw) FROM player_gw").fetchone()
    return int(played[0] or 0) + 1 if played else 1


def fixture_grid(conn: sqlite3.Connection, from_gw: int,
                 horizon: int = DEFAULT_HORIZON) -> list[TeamRow]:
    """Every team's fixtures across `horizon` gameweeks from `from_gw`."""
    gws = list(range(from_gw, from_gw + horizon))
    teams = {int(r["id"]): r["short_name"] for r in
             conn.execute("SELECT id, short_name FROM teams ORDER BY short_name")}
    if not teams:
        return []

    marks = ",".join("?" * len(gws))
    by_team: dict[int, dict[int, list[FixtureCell]]] = {
        tid: {gw: [] for gw in gws} for tid in teams}

    for r in conn.execute(
            f"""SELECT event, team_h, team_a, team_h_difficulty,
                       team_a_difficulty, kickoff_time
                FROM fixtures WHERE event IN ({marks})""", gws):
        for team, opp, home in ((r["team_h"], r["team_a"], True),
                                (r["team_a"], r["team_h"], False)):
            if team not in by_team:
                continue
            by_team[team][int(r["event"])].append(FixtureCell(
                gw=int(r["event"]), opponent=teams.get(opp, "?"), home=home,
                fdr=int((r["team_h_difficulty"] if home
                         else r["team_a_difficulty"]) or 3),
                kickoff=r["kickoff_time"]))

    rows: list[TeamRow] = []
    for tid, short in teams.items():
        cells: list[FixtureCell] = []
        for gw in gws:
            found = by_team[tid][gw]
            if not found:
                cells.append(FixtureCell(gw=gw))
            elif len(found) == 1:
                cells.append(found[0])
            else:
                # Double gameweek: collapse to one cell showing both, taking the
                # easier difficulty since that is the one driving the decision.
                best = min(found, key=lambda c: c.fdr or 5)
                merged = FixtureCell(
                    gw=gw, opponent="+".join(c.opponent for c in found),
                    home=best.home, fdr=best.fdr, kickoff=best.kickoff)
                cells.append(merged)
        rows.append(TeamRow(team_id=tid, team=short, cells=cells))

    rows.sort(key=lambda r: r.mean_fdr)
    return rows


# --------------------------------------------------------------------------
# Rotation pairs
# --------------------------------------------------------------------------
def rotation_pairs(conn: sqlite3.Connection, from_gw: int,
                   horizon: int = DEFAULT_HORIZON, *,
                   positions: tuple[str, ...] = ("DEF", "GKP"),
                   min_price: float = ROTATION_MIN_PRICE,
                   max_price: float = ROTATION_MAX_PRICE,
                   limit: int = 12) -> list[RotationPair]:
    """Budget pairs whose good fixtures alternate across the horizon.

    Both players must be in the price band and expected to play; the pair is
    scored on how many gameweeks have at least one of them facing a fixture at
    or below `GOOD_FDR`. Two players from the same club are excluded outright --
    their fixtures are identical, so the pair covers nothing.
    """
    gws = list(range(from_gw, from_gw + horizon))
    rows = fixture_grid(conn, from_gw, horizon)
    fdr_by_team = {r.team_id: {c.gw: c.fdr for c in r.cells} for r in rows}

    marks = ",".join("?" * len(positions))
    players = [dict(r) for r in conn.execute(
        f"""SELECT p.id, p.web_name, p.position, p.now_cost, p.team_id,
                   p.minutes, p.status, t.short_name AS team
            FROM players p LEFT JOIN teams t ON t.id = p.team_id
            WHERE p.position IN ({marks})
              AND p.now_cost BETWEEN ? AND ?
              AND p.status = 'a'
            ORDER BY p.minutes DESC""",
        [*positions, min_price, max_price])]

    # Only players with real minutes are useful: a £4.0m defender who never
    # plays covers no gameweek at all, however good his fixtures look.
    played = [p for p in players if (p["minutes"] or 0) > 0]
    pool = played or players
    if len(pool) < 2:
        return []

    # Cap the scan. 20 teams of budget defenders is ~60 players = 1,770 pairs,
    # which is instant; an uncapped pool on a full 700-player table is not.
    pool = pool[:60]

    pairs: list[RotationPair] = []
    for a, b in combinations(pool, 2):
        if a["team_id"] == b["team_id"] or a["position"] != b["position"]:
            continue
        fa = fdr_by_team.get(a["team_id"], {})
        fb = fdr_by_team.get(b["team_id"], {})

        schedule, covered, best_total = [], 0, 0.0
        for gw in gws:
            da, db = fa.get(gw), fb.get(gw)
            options = [(d, who) for d, who in ((da, a), (db, b)) if d is not None]
            if options:
                best_fdr, best_player = min(options, key=lambda o: o[0])
                best_total += best_fdr
                if best_fdr <= GOOD_FDR:
                    covered += 1
                schedule.append({"gw": gw, "start": best_player["web_name"],
                                 "fdr": best_fdr,
                                 "a_fdr": da, "b_fdr": db})
            else:
                best_total += 5
                schedule.append({"gw": gw, "start": "-", "fdr": None,
                                 "a_fdr": da, "b_fdr": db})

        pairs.append(RotationPair(
            player_a=a["web_name"], player_b=b["web_name"],
            player_a_id=int(a["id"]), player_b_id=int(b["id"]),
            team_a=a["team"] or "", team_b=b["team"] or "",
            position=a["position"],
            combined_cost=round(float(a["now_cost"]) + float(b["now_cost"]), 1),
            covered_gws=covered, horizon=len(gws),
            mean_best_fdr=round(best_total / len(gws), 2) if gws else 5.0,
            schedule=schedule))

    # Coverage first, then the quality of the fixtures actually started, then
    # price -- cheaper is strictly better when the football is equivalent.
    pairs.sort(key=lambda p: (-p.covered_gws, p.mean_best_fdr, p.combined_cost))
    return pairs[:limit]


# --------------------------------------------------------------------------
# Congestion
# --------------------------------------------------------------------------
def congestion_warnings(conn: sqlite3.Connection, cfg, from_gw: int,
                        horizon: int = DEFAULT_HORIZON
                        ) -> list[CongestionWarning]:
    """Teams facing doubles, tight turnarounds or European commitments."""
    gws = list(range(from_gw, from_gw + horizon))
    marks = ",".join("?" * len(gws))
    teams = {int(r["id"]): r["short_name"] for r in
             conn.execute("SELECT id, short_name FROM teams")}

    kickoffs: dict[int, list[dt.datetime]] = {}
    per_gw: dict[tuple[int, int], int] = {}
    for r in conn.execute(
            f"""SELECT event, team_h, team_a, kickoff_time
                FROM fixtures WHERE event IN ({marks})""", gws):
        when = _parse(r["kickoff_time"])
        for team in (r["team_h"], r["team_a"]):
            if team is None:
                continue
            per_gw[(int(team), int(r["event"]))] = \
                per_gw.get((int(team), int(r["event"])), 0) + 1
            if when is not None:
                kickoffs.setdefault(int(team), []).append(when)

    warnings: list[CongestionWarning] = []

    for (team_id, gw), count in per_gw.items():
        if count >= 2:
            warnings.append(CongestionWarning(
                team_id=team_id, team=teams.get(team_id, "?"), gw=gw,
                matches=count, severity="high",
                note=f"{count} fixtures in GW{gw} - rotation likely across both"))

    # Tight turnarounds, measured between consecutive league kickoffs only.
    # European midweek games are not in this table, so this understates rather
    # than overstates -- which is the safe direction for a warning.
    for team_id, times in kickoffs.items():
        times.sort()
        for earlier, later in pairwise(times):
            hours = (later - earlier).total_seconds() / 3600.0
            if hours < TIGHT_TURNAROUND_HOURS:
                warnings.append(CongestionWarning(
                    team_id=team_id, team=teams.get(team_id, "?"),
                    gw=0, matches=2, turnaround_hours=round(hours, 1),
                    severity="high" if hours < 60 else "watch",
                    note=(f"{hours:.0f}h between fixtures "
                          f"({earlier:%d %b} to {later:%d %b})")))

    # European commitments come from the maintained calendar; the FPL API has
    # no knowledge of them at all.
    #
    # Only *European* competitions are reported. Every one of the twenty clubs
    # is in the FA Cup and the EFL Cup, so listing those produced twenty
    # identical rows that said nothing and buried the handful of real
    # turnaround warnings underneath them. A signal every team triggers is not
    # a warning, it is a constant.
    try:
        from .. import congestion as congestion_mod
        for team_id, short in teams.items():
            comps = congestion_mod.team_competitions(cfg, short)
            european = [c.get("name", "") for c in comps
                        if _is_european(c.get("name", ""))]
            if european:
                warnings.append(CongestionWarning(
                    team_id=team_id, team=short, gw=0, matches=0,
                    competitions=european, severity="european",
                    note="midweek European football: " + ", ".join(european)))
    except Exception:  # noqa: BLE001, S110 - see below
        # The calendar is a hand-maintained YAML file. A typo in it must cost
        # the European-commitment warnings, never the fixture grid the page
        # exists to show.
        pass

    order = {"high": 0, "watch": 1, "european": 2}
    warnings.sort(key=lambda w: (order.get(w.severity, 3), w.team))
    return warnings


def _is_european(name: str) -> bool:
    """True for UEFA competitions only, not the domestic cups every club is in."""
    lowered = (name or "").lower()
    return any(token in lowered for token in
               ("champions league", "europa", "conference league", "uefa"))


def build(conn: sqlite3.Connection, cfg, *, horizon: int = DEFAULT_HORIZON,
          from_gw: int | None = None,
          rotation_positions: tuple[str, ...] = ("DEF", "GKP"),
          max_price: float = ROTATION_MAX_PRICE) -> ScheduleVM:
    """Assemble the whole Schedule & Congestion view."""
    horizon = max(MIN_HORIZON, min(MAX_HORIZON, int(horizon)))
    start = from_gw or anchor_gw(conn)

    vm = ScheduleVM(horizon=horizon, gws=list(range(start, start + horizon)))
    vm.rows = fixture_grid(conn, start, horizon)
    if not vm.rows:
        vm.notes.append("No fixtures ingested yet - run the FPL data refresh.")
        return vm

    vm.pairs = rotation_pairs(conn, start, horizon,
                              positions=rotation_positions,
                              max_price=max_price)
    if not vm.pairs:
        vm.notes.append(
            f"No rotation pairs found in the {ROTATION_MIN_PRICE:.1f}-"
            f"{max_price:.1f}m band with minutes on the board.")
    vm.warnings = congestion_warnings(conn, cfg, start, horizon)
    return vm
