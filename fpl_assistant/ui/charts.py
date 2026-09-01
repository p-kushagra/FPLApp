"""Plotly figures: shot maps, radars and distribution plots.

Every function returns a Plotly `Figure` and never touches Streamlit, so the
figures are unit-testable without a browser and reusable outside the app. When
the data a chart needs is missing, it returns a figure carrying a readable
annotation rather than None -- an empty axis with "Understat offline" written
across it tells the operator what happened; a blank space does not.

Colours are read from one palette so the four pages stay visually consistent,
and every scale is colour-blind safe: difficulty runs green-to-red *and*
light-to-dark, so the ordering survives being seen in greyscale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Fixture difficulty 1 (easiest) to 5 (hardest), on the green-to-dark-red ramp
# every FPL manager already reads without a legend. Two properties are load
# bearing: hue runs green -> amber -> red so the ordering is conventional, and
# luminance falls monotonically with difficulty so the ramp still ranks
# correctly in greyscale or for a red-green colour-blind reader. 5 is a darker
# red than 4 rather than a brighter one -- "worse" must read as "heavier".
FDR_COLOURS = {
    1: "#1a7f37", 2: "#4ac26b", 3: "#d4a72c", 4: "#d1442f", 5: "#8b1a13",
}
FDR_TEXT = {1: "#ffffff", 2: "#04260f", 3: "#2b1d00", 4: "#ffffff", 5: "#ffffff"}

ACCENT = "#3f7fd4"
POSITIVE = "#1a7f37"
NEGATIVE = "#b3211f"
MUTED = "#8b949e"
GRID = "rgba(140,140,140,0.25)"

# The app surface (Streamlit's default dark canvas). Overlapping marks carry a
# ring in this colour rather than a white border: the ring reads as a gap
# between marks, where a light border reads as a fourth thing on the chart.
SURFACE = "#0e1117"

# -- shot-map marker scale --------------------------------------------------
# xG is an absolute probability, so the marker scale is absolute too: area is
# proportional to xG against a FIXED anchor of 1.0, never to the selected
# player's own maximum. Per-figure normalisation would size every player's
# best chance identically, so a defender's six headers would look like
# Haaland's six tap-ins -- the one comparison a shot map exists to support.
# With this anchor a marker's area IS its scoring probability, and the same
# shot is the same size on every player's map and in every season.
SHOT_MAX_XG = 1.0      # a certain goal, the theoretical maximum
SHOT_MAX_PX = 26.0     # diameter of that certain goal
SHOT_MIN_PX = 8.0      # the >=8px floor every mark needs to stay a hit target


def _shot_marker_px(xg: float) -> float:
    """Marker DIAMETER in px for one shot's xG.

    Diameter goes as sqrt(xG) so that AREA goes as xG -- area is what the eye
    compares, and sizing by diameter instead would overstate a big chance
    roughly fourfold. Plotly's `marker.size` is a diameter, so the square root
    belongs here rather than in the trace.
    """
    ratio = max(0.0, float(xg)) / SHOT_MAX_XG
    return max(SHOT_MIN_PX, (ratio ** 0.5) * SHOT_MAX_PX)


def _plotly():
    """Import Plotly lazily so the app still starts without it installed."""
    import plotly.graph_objects as go
    return go


def available() -> bool:
    try:
        import plotly  # noqa: F401
        return True
    except ImportError:
        return False


def _empty(message: str, height: int = 320):
    """A figure that explains why it is empty."""
    go = _plotly()
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, xref="paper", yref="paper",
                       x=0.5, y=0.5, font={"size": 13, "color": MUTED},
                       align="center")
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(height=height, margin={"l": 10, "r": 10, "t": 30, "b": 10},
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig


def _theme(fig, height: int, title: str | None = None):
    fig.update_layout(
        height=height, title=title,
        margin={"l": 40, "r": 20, "t": 50 if title else 20, "b": 40},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"size": 12}, hovermode="closest",
        legend={"orientation": "h", "y": -0.15})
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID)
    return fig


# --------------------------------------------------------------------------
# Shot map
# --------------------------------------------------------------------------
# Pitch geometry in METRES (IFAB dimensions), not in Understat's 0-1 units.
# Understat's axes are anisotropic -- one x-unit is 105m/1 and one y-unit is
# 68m/1 -- so any figure drawn in those units needs a correction factor on the
# aspect ratio to avoid distorting distance and angle, which is the whole
# content of a shot map. Working in metres makes the correct aspect exactly
# 1:1 and removes the class of bug entirely.
PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0
GOAL_WIDTH_M = 7.32
PEN_AREA_DEPTH_M, PEN_AREA_WIDTH_M = 16.5, 40.32
SIX_YARD_DEPTH_M, SIX_YARD_WIDTH_M = 5.5, 18.32
PEN_SPOT_M = 11.0
PEN_ARC_RADIUS_M = 9.15

# How much of the pitch to draw, measured back from the goal line. 35m keeps
# 99.2% of all shots; the attacking half (52.5m) keeps 99.5%. Spending a third
# of the vertical resolution to gain 0.3% of the data is what made the goalmouth
# -- where every shot actually is -- an unreadable smudge.
#
# The frame is FIXED, for the same reason the marker scale is: two players'
# maps have to be comparable at a glance. Letting one hopeful 50-yarder stretch
# the frame would compress the goalmouth of the map it appears on and make it
# quietly incomparable with every other. Shots outside the frame are counted in
# the subtitle instead -- stated rather than silently dropped, and never drawn
# at a position they were not taken from.
SHOT_VIEW_DEPTH_M = 35.0

# Same argument across the pitch: 99.7% of in-frame shots fall inside a 44m
# band, so drawing all 68m spends a third of the width on touchline strips no
# shot is ever taken from, and shrinks the goalmouth to pay for it. 54m keeps
# 99.94% and still leaves ~7m of context outside each edge of the penalty area.
SHOT_VIEW_WIDTH_M = 54.0


@dataclass
class Shot:
    """One shot. Coordinates are Understat's 0-1 fractions of the pitch."""

    x: float
    y: float
    xg: float
    result: str = "MissedShots"
    minute: int | None = None
    situation: str = ""
    opponent: str = ""

    @property
    def is_own_goal(self) -> bool:
        """Understat files own goals in the shooter's own shot list.

        They carry xG 0.0 and coordinates at the shooter's OWN end, so they
        are not shots at the goal this figure draws. Counting one as a goal
        adds +1 goal against 0.00 xG, which reads on the Goals - xG tile as
        elite finishing -- the exact opposite of what happened.
        """
        return self.result.lower() == "owngoal"

    @property
    def is_goal(self) -> bool:
        return self.result.lower() == "goal"

    @property
    def across_m(self) -> float:
        """Position across the pitch, 0 to 68m. The figure's x axis."""
        return self.y * PITCH_WIDTH_M

    @property
    def upfield_m(self) -> float:
        """Position along the pitch, with the attacked goal at 105m."""
        return self.x * PITCH_LENGTH_M

    @property
    def distance_m(self) -> float:
        """Straight-line distance to the centre of the goal."""
        dx = self.across_m - PITCH_WIDTH_M / 2.0
        dy = PITCH_LENGTH_M - self.upfield_m
        return (dx * dx + dy * dy) ** 0.5


def _in_frame(shot: Shot, depth_m: float) -> bool:
    """Whether a shot falls inside the drawn crop, in both directions."""
    half_w = PITCH_WIDTH_M / 2.0
    return (PITCH_LENGTH_M - shot.upfield_m <= depth_m
            and abs(shot.across_m - half_w) <= SHOT_VIEW_WIDTH_M / 2.0)


def shot_map(shots: list[Shot], *, title: str | None = None, height: int = 460):
    """Goalmouth shot map: position, xG as marker area, goals starred.

    Drawn in portrait with the goal line horizontal across the top, which is
    the orientation every published shot map uses: the eye reads distance from
    goal as vertical, and the penalty area frames the cluster where almost
    every shot is. Only the final ~35m is drawn -- see SHOT_VIEW_DEPTH_M.

    Proportions are true. The axes are locked 1:1 in metres via `scaleanchor`
    AND `constrain="domain"`; the second half of that pair is load bearing.
    Plotly's default is `constrain="range"`, which honours an aspect ratio by
    widening the RANGE until the figure fills its container -- so the same
    figure rendered as a tall strip in a narrow column and a stretched
    landscape in a wide one, with the pitch adrift in the middle of both, and
    the requested `range` acting only as a minimum. `domain` shrinks the
    plotting area instead, which is what "keep the pitch the right shape"
    actually means.
    """
    go = _plotly()
    if not shots:
        return _empty("No shot data available.<br>"
                      "Understat provides shot coordinates; it is currently "
                      "unreachable, so the xP model is running on FPL "
                      "baseline stats.", height)

    # An own goal is not an attempt at the goal drawn here, and it sits ~100m
    # away, which would drag the frame back to the halfway line and squash the
    # goalmouth flat. Dropped rather than plotted somewhere misleading.
    shots = [s for s in shots if not s.is_own_goal]
    if not shots:
        return _empty("No shots at goal in this selection.", height)

    depth = SHOT_VIEW_DEPTH_M
    fig = go.Figure()
    _draw_goalmouth(fig, depth)

    goals = [s for s in shots if s.is_goal]
    misses = [s for s in shots if not s.is_goal]

    for group, colour, symbol, name in (
            (misses, ACCENT, "circle", "Shot"),
            (goals, POSITIVE, "star", "Goal")):
        if not group:
            continue
        fig.add_trace(go.Scatter(
            x=[s.across_m for s in group],
            y=[s.upfield_m for s in group],
            mode="markers", name=name,
            marker={
                # One scale for both traces: a goal and a miss of equal xG must
                # be the same size, or the map encodes the outcome twice (in
                # colour AND in area) and reads as though goals were the better
                # chances -- which is precisely the bias a shot map disproves.
                "size": [_shot_marker_px(s.xg) for s in group],
                "color": colour, "symbol": symbol,
                "opacity": 0.6 if name == "Shot" else 0.95,
                "line": {"width": 2, "color": SURFACE},
            },
            customdata=[[s.xg, s.minute or "-", s.situation or "-",
                         s.opponent or "-", s.distance_m] for s in group],
            hovertemplate=("<b>%{customdata[0]:.2f} xG</b><br>"
                           "%{customdata[4]:.0f} m from goal<br>"
                           "minute %{customdata[1]}<br>"
                           "%{customdata[2]}<br>vs %{customdata[3]}"
                           "<extra></extra>")))

    total_xg = sum(s.xg for s in shots)
    subtitle = (f"{len(shots)} shots · {total_xg:.2f} xG · "
                f"{len(goals)} scored")
    # Say what the fixed frame leaves out. A count the reader cannot see is
    # worse than an ugly subtitle, and their xG is still in the total above.
    off_frame = sum(1 for s in shots if not _in_frame(s, depth))
    if off_frame:
        subtitle += f" · {off_frame} outside the view, counted but not drawn"
    fig.update_layout(
        title=f"{title}<br><sub>{subtitle}</sub>" if title else subtitle,
        height=height, showlegend=True,
        margin={"l": 10, "r": 10, "t": 60, "b": 40},
        paper_bgcolor="rgba(0,0,0,0)",
        # `constrain="domain"` shrinks the plotting area to exactly the cropped
        # pitch, so the plot background IS the pitch and painting it costs
        # nothing. Without it the grass below the D reads as empty canvas and
        # the figure looks broken rather than looking like a player who does
        # not shoot from distance. Neutral grey rather than a fixed shade, so
        # it lifts on a dark theme and recedes on a light one.
        plot_bgcolor="rgba(128,128,128,0.10)",
        hovermode="closest",
        legend={"orientation": "h", "y": -0.02, "x": 0.5, "xanchor": "center"})

    # `fixedrange` disables drag-zoom and drag-pan. A pitch has one correct
    # framing and no reason to be panned off it; leaving zoom enabled only
    # offers the reader a way to break the aspect ratio the figure just fixed.
    pad = 1.5
    half_w = PITCH_WIDTH_M / 2.0
    fig.update_xaxes(visible=False, fixedrange=True, constrain="domain",
                     range=[half_w - SHOT_VIEW_WIDTH_M / 2.0,
                            half_w + SHOT_VIEW_WIDTH_M / 2.0])
    fig.update_yaxes(visible=False, fixedrange=True, constrain="domain",
                     scaleanchor="x", scaleratio=1,
                     range=[PITCH_LENGTH_M - depth, PITCH_LENGTH_M + pad])
    return fig


def _draw_goalmouth(fig, depth_m: float) -> None:
    """Pitch furniture in metres, goal line horizontal across the top.

    Markings are recessive: they orient the reader and give the shots a scale,
    and then they get out of the way. The goal itself is the one line drawn at
    full strength, because "how far out, and at what angle" is the question the
    reader brings to the figure and the goal is the thing both are measured to.
    """
    line = {"color": "rgba(140,140,140,0.45)", "width": 1.2}
    goal_line = {"color": "rgba(235,235,235,0.85)", "width": 3}

    half_w = PITCH_WIDTH_M / 2.0
    goal_y = PITCH_LENGTH_M
    view_half = SHOT_VIEW_WIDTH_M / 2.0

    # The goal line is real and runs the full width. The touchlines and the
    # back of the view are NOT drawn: the frame is a crop, and outlining a crop
    # draws three lines that do not exist on a pitch, inviting the reader to
    # take the nearest one for a touchline and misjudge every angle from it.
    fig.add_shape(type="line", layer="below",
                  x0=half_w - view_half, y0=goal_y,
                  x1=half_w + view_half, y1=goal_y, line=line)

    for x0, y0, x1, y1 in (
            (half_w - PEN_AREA_WIDTH_M / 2.0, goal_y - PEN_AREA_DEPTH_M,
             half_w + PEN_AREA_WIDTH_M / 2.0, goal_y),
            (half_w - SIX_YARD_WIDTH_M / 2.0, goal_y - SIX_YARD_DEPTH_M,
             half_w + SIX_YARD_WIDTH_M / 2.0, goal_y),
    ):
        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1, y1=y1, line=line,
                      layer="below")

    # Penalty spot. Deliberately smaller and fainter than the smallest shot
    # marker -- at equal weight it reads as an 11m tap-in that nobody took.
    spot_y = goal_y - PEN_SPOT_M
    fig.add_shape(type="circle", layer="below",
                  x0=half_w - 0.22, y0=spot_y - 0.22,
                  x1=half_w + 0.22, y1=spot_y + 0.22,
                  fillcolor="rgba(140,140,140,0.35)",
                  line={"color": "rgba(140,140,140,0.35)", "width": 0.5})

    # The D: the arc of the penalty circle lying outside the penalty area.
    box_edge = goal_y - PEN_AREA_DEPTH_M
    dy = spot_y - box_edge
    if PEN_ARC_RADIUS_M > dy:
        half_span = (PEN_ARC_RADIUS_M ** 2 - dy ** 2) ** 0.5
        steps = 24
        pts = []
        for i in range(steps + 1):
            # Sweep the arc below the box edge only.
            t = -half_span + (2 * half_span) * i / steps
            pts.append((half_w + t,
                        spot_y - (PEN_ARC_RADIUS_M ** 2 - t * t) ** 0.5))
        path = "M " + " L ".join(f"{px:.2f},{py:.2f}" for px, py in pts)
        fig.add_shape(type="path", path=path, line=line, layer="below")

    # The goal, drawn as a horizontal segment on the goal line.
    fig.add_shape(type="line", layer="below",
                  x0=half_w - GOAL_WIDTH_M / 2.0, y0=goal_y,
                  x1=half_w + GOAL_WIDTH_M / 2.0, y1=goal_y, line=goal_line)


# --------------------------------------------------------------------------
# Radar
# --------------------------------------------------------------------------
def radar(series: dict[str, dict[str, float]], *, title: str | None = None,
          height: int = 440):
    """Multi-metric comparison, one closed polygon per entrant.

    `series` is `{"You": {"npxG": 12.1, ...}, "Rival": {...}}`. Every metric is
    normalised to the maximum observed across entrants, because a radar with
    raw axes at different scales is a picture of the units, not of the teams.
    """
    go = _plotly()
    if not series:
        return _empty("No squads to compare.", height)

    metrics = sorted({m for values in series.values() for m in values})
    if not metrics:
        return _empty("No metrics to compare.", height)

    maxima = {m: max((abs(v.get(m, 0.0)) for v in series.values()), default=0.0)
              for m in metrics}
    palette = [ACCENT, "#d4a72c", POSITIVE, "#8957e5", NEGATIVE, "#0f8b8d"]

    fig = go.Figure()
    for index, (label, values) in enumerate(series.items()):
        scaled = [(values.get(m, 0.0) / maxima[m] * 100.0) if maxima[m] else 0.0
                  for m in metrics]
        colour = palette[index % len(palette)]
        fig.add_trace(go.Scatterpolar(
            r=scaled + scaled[:1],                 # close the polygon
            theta=metrics + metrics[:1],
            fill="toself", name=label,
            line={"color": colour, "width": 2},
            fillcolor=colour.replace(")", ", 0.16)").replace("#", "rgba(")
            if colour.startswith("rgba") else None,
            opacity=0.55 if index else 0.8,
            customdata=[[values.get(m, 0.0)] for m in metrics] +
                       [[values.get(metrics[0], 0.0)]],
            hovertemplate="<b>%{theta}</b><br>%{customdata[0]:.2f}<extra>"
                          + label + "</extra>"))

    fig.update_layout(
        title=title, height=height,
        polar={"radialaxis": {"visible": True, "range": [0, 105],
                              "showticklabels": False, "gridcolor": GRID},
               "angularaxis": {"gridcolor": GRID},
               "bgcolor": "rgba(0,0,0,0)"},
        margin={"l": 60, "r": 60, "t": 60 if title else 30, "b": 40},
        paper_bgcolor="rgba(0,0,0,0)",
        legend={"orientation": "h", "y": -0.08})
    return fig


# --------------------------------------------------------------------------
# Distribution
# --------------------------------------------------------------------------
def distribution_bars(rows: list[dict], *, title: str | None = None,
                      height: int = 420):
    """Floor / expected / ceiling per player, as a horizontal range.

    Rows need `player`, `floor`, `mean` and `ceiling`. The bar is the range and
    the marker is the expectation, so a wide bar with a low marker reads
    instantly as boom-or-bust -- which is the entire point of simulating.
    """
    go = _plotly()
    if not rows:
        return _empty("No simulation results yet.", height)

    ordered = sorted(rows, key=lambda r: r.get("mean", 0.0))
    names = [r["player"] for r in ordered]
    floors = [float(r.get("floor", 0.0)) for r in ordered]
    ceilings = [float(r.get("ceiling", 0.0)) for r in ordered]
    means = [float(r.get("mean", 0.0)) for r in ordered]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[c - f for c, f in zip(ceilings, floors)], y=names, base=floors,
        orientation="h", name="10th-90th percentile",
        marker={"color": ACCENT, "opacity": 0.35,
                "line": {"width": 0}},
        customdata=list(zip(floors, ceilings)),
        hovertemplate=("floor %{customdata[0]:.1f}  "
                       "ceiling %{customdata[1]:.1f}<extra></extra>")))
    fig.add_trace(go.Scatter(
        x=means, y=names, mode="markers", name="Expected",
        marker={"color": ACCENT, "size": 11, "symbol": "diamond",
                "line": {"width": 1, "color": "rgba(255,255,255,0.8)"}},
        hovertemplate="expected %{x:.2f} pts<extra></extra>"))

    fig = _theme(fig, height, title)
    fig.update_xaxes(title="points")
    return fig


def fdr_heatmap(teams: list[str], gws: list[int], grid: list[list[Any]],
                *, labels: list[list[str]] | None = None,
                title: str | None = None, height: int | None = None):
    """Fixture difficulty grid: teams down, gameweeks across.

    `grid[i][j]` is the difficulty of `teams[i]`'s fixture in `gws[j]`, or None
    for a blank. `labels` supplies the opponent text drawn in each cell.
    """
    go = _plotly()
    if not teams or not gws:
        return _empty("No fixtures in this horizon.", height or 300)

    height = height or max(320, 26 * len(teams) + 110)
    colourscale = [[0.0, FDR_COLOURS[1]], [0.25, FDR_COLOURS[2]],
                   [0.5, FDR_COLOURS[3]], [0.75, FDR_COLOURS[4]],
                   [1.0, FDR_COLOURS[5]]]

    fig = go.Figure(go.Heatmap(
        z=grid, x=[f"GW{g}" for g in gws], y=teams,
        zmin=1, zmax=5, colorscale=colourscale, showscale=False,
        xgap=2, ygap=2,
        text=labels or [["" for _ in gws] for _ in teams],
        texttemplate="%{text}", textfont={"size": 10},
        hovertemplate="%{y} · %{x}<br>%{text}<extra></extra>"))

    fig.update_layout(
        title=title, height=height,
        margin={"l": 10, "r": 10, "t": 50 if title else 20, "b": 30},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"size": 11})
    fig.update_xaxes(side="top", showgrid=False)
    fig.update_yaxes(autorange="reversed", showgrid=False)
    return fig
