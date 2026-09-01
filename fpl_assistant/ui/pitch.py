"""Interactive tactical pitch: formation, badges, fixture pills, click-to-swap.

The squad table is the wrong shape for the decisions taken on it. Bench order,
formation legality and "which of my three keepers-worth of defenders is actually
starting" are spatial questions, and reading them off eleven rows of a dataframe
is slower and more error-prone than looking at a pitch.

Two halves, deliberately separated:

* `figure()` builds a Plotly pitch and returns it. No Streamlit, so it is
  unit-testable and the layout maths can be asserted directly.
* `render()` draws that figure in Streamlit and adds the swap controls beneath.

Swapping is a select-two-then-confirm interaction rather than true drag and
drop. Streamlit has no drag primitive that survives a rerun without a custom
component, and a swap control that silently loses the drop on rerun is worse
than two clicks. The proposed XI is validated against the same formation rules
the auto-sub engine uses (`live.formation_legal`), so an illegal swap is refused
with a reason instead of being rendered as a broken team.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..live import MAX_FORMATION, MIN_FORMATION, XI_SIZE, formation_legal
from . import charts

# Row height per position band, as a fraction of pitch length. Keepers on their
# own line, then three outfield bands.
ROW_Y = {"GKP": 8.0, "DEF": 30.0, "MID": 55.0, "FWD": 80.0}
BENCH_Y = 96.0
POSITION_ORDER = ("GKP", "DEF", "MID", "FWD")

CAPTAIN_MARK = "(C)"
VICE_MARK = "(V)"


@dataclass
class PitchPlayer:
    """One player as drawn on the pitch."""

    player_id: int
    name: str
    position: str
    team: str = ""
    cost: float = 0.0
    starting: bool = True
    multiplier: float = 1.0
    is_captain: bool = False
    is_vice: bool = False
    bench_order: int = 0
    xp: float = 0.0
    points: int | None = None
    next_fdr: int | None = None
    next_opponent: str = ""
    badges: list[str] = field(default_factory=list)
    status: str = "a"
    availability: float = 1.0

    @property
    def label(self) -> str:
        mark = (CAPTAIN_MARK if self.is_captain
                else (VICE_MARK if self.is_vice else ""))
        return f"{self.name} {mark}".strip()

    @property
    def flagged(self) -> bool:
        return self.status != "a" or self.availability < 1.0


def formation_string(starters: list[PitchPlayer]) -> str:
    """"3-4-3" from the starting XI."""
    counts = {p: 0 for p in POSITION_ORDER}
    for player in starters:
        counts[player.position] = counts.get(player.position, 0) + 1
    return "-".join(str(counts[p]) for p in ("DEF", "MID", "FWD"))


def _layout(players: list[PitchPlayer]) -> list[tuple[PitchPlayer, float, float]]:
    """Assign (x, y) to every player: bands by position, spread across the width."""
    placed: list[tuple[PitchPlayer, float, float]] = []

    for position in POSITION_ORDER:
        band = [p for p in players if p.starting and p.position == position]
        if not band:
            continue
        # Even spacing with margins, so a back three and a back five both look
        # deliberate rather than crowded to one side.
        step = 100.0 / (len(band) + 1)
        for index, player in enumerate(sorted(band, key=lambda p: -p.cost), 1):
            placed.append((player, step * index, ROW_Y[position]))

    bench = sorted([p for p in players if not p.starting],
                   key=lambda p: p.bench_order)
    if bench:
        step = 100.0 / (len(bench) + 1)
        for index, player in enumerate(bench, 1):
            placed.append((player, step * index, BENCH_Y))
    return placed


def figure(players: list[PitchPlayer], *, show_points: bool = False,
           height: int = 620, title: str | None = None):
    """Build the tactical pitch figure."""
    if not charts.available():
        raise RuntimeError("plotly is required for the pitch view")
    go = charts._plotly()

    if not players:
        return charts._empty("No squad loaded for this gameweek.", height)

    fig = go.Figure()
    _draw_pitch(fig)

    placed = _layout(players)
    for starting in (True, False):
        group = [(p, x, y) for p, x, y in placed if p.starting is starting]
        if not group:
            continue
        fig.add_trace(_shirt_trace(go, group, starting, show_points))

    # Fixture-difficulty pills sit under each shirt, coloured by FDR so the
    # whole team's next fixture is legible at a glance.
    for player, x, y in placed:
        if player.next_fdr is None:
            continue
        fdr = max(1, min(5, int(player.next_fdr)))
        fig.add_annotation(
            x=x, y=y + 7.4, text=f" {player.next_opponent or ''} ".strip() or " ",
            showarrow=False, font={"size": 9, "color": charts.FDR_TEXT[fdr]},
            bgcolor=charts.FDR_COLOURS[fdr], borderpad=2, opacity=0.95)

    badge_lines = [(p, x, y) for p, x, y in placed if p.badges]
    for player, x, y in badge_lines:
        fig.add_annotation(
            x=x, y=y - 6.6, text=" ".join(player.badges[:2]), showarrow=False,
            font={"size": 8, "color": "#f0f6fc"},
            bgcolor="rgba(63,127,212,0.85)", borderpad=2)

    # Captain and vice armbands, drawn as their own high-contrast chip beside
    # the shirt. The "(C)" suffix inside the shirt label was legible only if
    # you already knew to look for it, and the armband is the first thing a
    # manager checks on a pitch.
    for player, x, y in placed:
        if not (player.is_captain or player.is_vice):
            continue
        fig.add_annotation(
            x=x + 4.6, y=y - 3.4,
            text=f"<b>{'C' if player.is_captain else 'V'}</b>",
            showarrow=False,
            font={"size": 12,
                  "color": "#1c1200" if player.is_captain else "#f0f6fc"},
            bgcolor="#f0c000" if player.is_captain else "#57606a",
            bordercolor="#ffffff", borderwidth=1, borderpad=3, opacity=0.98)

    fig.update_layout(
        title=title, height=height, showlegend=False,
        margin={"l": 10, "r": 10, "t": 50 if title else 16, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(visible=False, range=[-4, 104])
    fig.update_yaxes(visible=False, range=[104, -6])   # keepers at the top
    return fig


def _shirt_trace(go, group, starting: bool, show_points: bool):
    players = [p for p, _x, _y in group]
    texts = []
    for player in players:
        head = player.label
        if show_points and player.points is not None:
            tail = f"{int(player.points * player.multiplier)} pts"
        else:
            tail = f"{player.xp:.1f} xP" if player.xp else f"{player.cost:.1f}m"
        texts.append(f"{head}<br>{tail}")

    return go.Scatter(
        x=[x for _p, x, _y in group], y=[y for _p, _x, y in group],
        mode="markers+text", text=texts, textposition="middle center",
        textfont={"size": 9, "color": "#ffffff"},
        marker={
            "size": 46 if starting else 38,
            "symbol": "circle",
            "color": ["#b3211f" if p.flagged
                      else ("#1f6feb" if starting else "#57606a")
                      for p in players],
            "opacity": 1.0 if starting else 0.75,
            "line": {"width": [3 if p.is_captain else 1 for p in players],
                     "color": "#f0c000"},
        },
        customdata=[[p.team, p.position, p.cost, p.xp,
                     " ".join(p.badges) or "-",
                     "starting" if p.starting else f"bench {p.bench_order}"]
                    for p in players],
        hovertemplate=("<b>%{text}</b><br>%{customdata[0]} · "
                       "%{customdata[1]} · %{customdata[2]:.1f}m<br>"
                       "xP %{customdata[3]:.2f}<br>%{customdata[4]}<br>"
                       "%{customdata[5]}<extra></extra>"),
        name="Starting XI" if starting else "Bench")


def _draw_pitch(fig) -> None:
    """Turf, boxes, halfway line and the bench strip."""
    line = {"color": "rgba(255,255,255,0.35)", "width": 1.5}
    fig.add_shape(type="rect", x0=0, y0=-4, x1=100, y1=92,
                  fillcolor="rgba(26,127,55,0.16)", line=line, layer="below")
    for x0, y0, x1, y1 in ((22, -4, 78, 12), (36, -4, 64, 2)):
        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1, y1=y1, line=line,
                      layer="below")
    fig.add_shape(type="line", x0=0, y0=88, x1=100, y1=88, line=line,
                  layer="below")
    fig.add_shape(type="circle", x0=40, y0=78, x1=60, y1=98, line=line,
                  layer="below")
    # Bench strip, visually separated from the pitch.
    fig.add_shape(type="rect", x0=0, y0=92, x1=100, y1=102,
                  fillcolor="rgba(120,120,120,0.14)",
                  line={"color": "rgba(255,255,255,0.15)", "width": 1},
                  layer="below")


# --------------------------------------------------------------------------
# Swap validation
# --------------------------------------------------------------------------
@dataclass
class SwapResult:
    ok: bool
    reason: str = ""
    formation: str = ""


def validate_swap(players: list[PitchPlayer], out_id: int,
                  in_id: int) -> SwapResult:
    """Would swapping a starter for a bench player leave a legal XI?

    Reuses `live.formation_legal`, so the pitch cannot disagree with the
    auto-substitution engine about what a legal team is.
    """
    by_id = {p.player_id: p for p in players}
    going_out, coming_in = by_id.get(out_id), by_id.get(in_id)
    if going_out is None or coming_in is None:
        return SwapResult(False, "player not in this squad")
    if going_out.starting == coming_in.starting:
        where = "starting" if going_out.starting else "on the bench"
        return SwapResult(False, f"both players are already {where}")

    starter = going_out if going_out.starting else coming_in
    sub = coming_in if going_out.starting else going_out

    if (starter.position == "GKP") != (sub.position == "GKP"):
        return SwapResult(
            False, "a goalkeeper can only be swapped with the other goalkeeper")

    positions = [p.position for p in players
                 if p.starting and p.player_id != starter.player_id]
    positions.append(sub.position)

    if not formation_legal(positions):
        counts = {p: positions.count(p) for p in POSITION_ORDER}
        broken = [f"{counts[p]} {p}" for p in POSITION_ORDER
                  if not (MIN_FORMATION[p] <= counts[p] <= MAX_FORMATION[p])]
        return SwapResult(
            False,
            f"illegal formation ({', '.join(broken) or 'wrong XI size'}) - "
            f"needs {MIN_FORMATION['DEF']}+ DEF, {MIN_FORMATION['MID']}+ MID, "
            f"{MIN_FORMATION['FWD']}+ FWD in an XI of {XI_SIZE}")

    counts = {p: positions.count(p) for p in POSITION_ORDER}
    return SwapResult(True, formation="-".join(
        str(counts[p]) for p in ("DEF", "MID", "FWD")))


def apply_swap(players: list[PitchPlayer], out_id: int,
               in_id: int) -> list[PitchPlayer]:
    """Return a new squad list with the two players' starting flags exchanged.

    Non-mutating: the caller decides whether to keep the proposal, which is what
    lets the UI preview a swap before committing it to session state.
    """
    result = validate_swap(players, out_id, in_id)
    if not result.ok:
        raise ValueError(result.reason)

    out: list[PitchPlayer] = []
    for player in players:
        if player.player_id in (out_id, in_id):
            other = in_id if player.player_id == out_id else out_id
            partner = next(p for p in players if p.player_id == other)
            out.append(_replace(player, starting=partner.starting,
                                bench_order=partner.bench_order))
        else:
            out.append(player)
    return out


def _replace(player: PitchPlayer, **changes) -> PitchPlayer:
    data = {**player.__dict__, **changes}
    return PitchPlayer(**data)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_squad(conn, gw: int, *, xp_by_player: dict[int, float] | None = None,
               badges: dict[int, list[str]] | None = None,
               points: dict[int, int] | None = None) -> list[PitchPlayer]:
    """Build the pitch model for a stored gameweek's picks."""
    from ..models import minutes as minutes_mod
    from ..rules import ELEMENT_TYPE_TO_POS

    rows = conn.execute(
        """SELECT mp.player_id, mp.position AS slot, mp.multiplier,
                  mp.is_captain, mp.is_vice, p.web_name, p.element_type,
                  p.now_cost, p.status, p.news, p.news_added,
                  p.chance_of_playing_next_round,
                  p.team_id, t.short_name AS team
           FROM my_picks mp
           JOIN players p ON p.id = mp.player_id
           LEFT JOIN teams t ON t.id = p.team_id
           WHERE mp.gw = ? ORDER BY mp.position""", (gw,)).fetchall()
    if not rows:
        return []

    next_fixture = _next_fixtures(conn, gw)
    xp_by_player = xp_by_player or {}
    badges = badges or {}
    points = points or {}

    out: list[PitchPlayer] = []
    for r in rows:
        pid = int(r["player_id"])
        slot = int(r["slot"] or 0)
        fdr, opponent = next_fixture.get(r["team_id"], (None, ""))
        out.append(PitchPlayer(
            player_id=pid, name=r["web_name"] or "",
            position=ELEMENT_TYPE_TO_POS.get(r["element_type"], "MID"),
            team=r["team"] or "", cost=float(r["now_cost"] or 0.0),
            starting=float(r["multiplier"] or 0) > 0,
            multiplier=float(r["multiplier"] or 0),
            is_captain=bool(r["is_captain"]), is_vice=bool(r["is_vice"]),
            bench_order=max(0, slot - XI_SIZE),
            xp=float(xp_by_player.get(pid, 0.0)),
            points=points.get(pid), next_fdr=fdr, next_opponent=opponent,
            badges=list(badges.get(pid, [])),
            status=r["status"] or "a",
            availability=minutes_mod.availability(dict(r))))
    return out


def _next_fixtures(conn, gw: int) -> dict[int, tuple[int, str]]:
    """`{team_id: (fdr, "ARS (H)")}` for each team's next unplayed fixture."""
    shorts = {int(r["id"]): r["short_name"] for r in
              conn.execute("SELECT id, short_name FROM teams")}
    out: dict[int, tuple[int, str]] = {}
    for r in conn.execute(
            """SELECT event, team_h, team_a, team_h_difficulty, team_a_difficulty
               FROM fixtures WHERE event IS NOT NULL AND event >= ?
               ORDER BY event""", (gw,)):
        for team, opp, home in ((r["team_h"], r["team_a"], True),
                                (r["team_a"], r["team_h"], False)):
            if team in out:
                continue
            fdr = r["team_h_difficulty"] if home else r["team_a_difficulty"]
            out[int(team)] = (int(fdr or 3),
                              f"{shorts.get(opp, '?')} ({'H' if home else 'A'})")
    return out
