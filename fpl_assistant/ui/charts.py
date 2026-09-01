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
    def is_goal(self) -> bool:
        return self.result.lower() in ("goal", "owngoal")


def shot_map(shots: list[Shot], *, title: str | None = None, height: int = 460):
    """Attacking-half shot map: position, xG as area, goals marked.

    Only the attacking half is drawn. Shots from a team's own half are so rare
    that including the full pitch would compress every real shot into a corner
    and waste the resolution where all the information actually is.
    """
    go = _plotly()
    if not shots:
        return _empty("No shot data available.<br>"
                      "Understat provides shot coordinates; it is currently "
                      "unreachable, so the xP model is running on FPL "
                      "baseline stats.", height)

    fig = go.Figure()
    _draw_attacking_half(fig)

    goals = [s for s in shots if s.is_goal]
    misses = [s for s in shots if not s.is_goal]

    for group, colour, symbol, name in (
            (misses, ACCENT, "circle", "Shot"),
            (goals, POSITIVE, "star", "Goal")):
        if not group:
            continue
        fig.add_trace(go.Scatter(
            x=[s.x * 100 for s in group],
            y=[s.y * 100 for s in group],
            mode="markers", name=name,
            marker={
                # Area, not radius, encodes xG: doubling the value doubles the
                # ink, which is what the eye actually compares.
                "size": [max(8.0, (s.xg ** 0.5) * 70) for s in group],
                "color": colour, "symbol": symbol,
                "opacity": 0.55 if name == "Shot" else 0.95,
                "line": {"width": 1, "color": "rgba(255,255,255,0.7)"},
            },
            customdata=[[s.xg, s.minute or "-", s.situation or "-",
                         s.opponent or "-"] for s in group],
            hovertemplate=("<b>%{customdata[0]:.2f} xG</b><br>"
                           "minute %{customdata[1]}<br>"
                           "%{customdata[2]}<br>vs %{customdata[3]}"
                           "<extra></extra>")))

    total_xg = sum(s.xg for s in shots)
    subtitle = (f"{len(shots)} shots &middot; {total_xg:.2f} xG &middot; "
                f"{len(goals)} scored")
    fig.update_layout(
        title=f"{title}<br><sub>{subtitle}</sub>" if title else subtitle,
        height=height, showlegend=True,
        margin={"l": 10, "r": 10, "t": 60, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend={"orientation": "h", "y": -0.05})
    fig.update_xaxes(visible=False, range=[48, 102],
                     scaleanchor="y", scaleratio=0.72)
    fig.update_yaxes(visible=False, range=[-2, 102])
    return fig


def _draw_attacking_half(fig) -> None:
    """Pitch furniture: box, six-yard box, penalty spot, goal line."""
    line = {"color": "rgba(140,140,140,0.55)", "width": 1.5}
    for x0, y0, x1, y1 in (
            (50, 0, 100, 100),        # half
            (84, 21.1, 100, 78.9),    # penalty area
            (94.5, 36.8, 100, 63.2),  # six-yard box
    ):
        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1, y1=y1, line=line)
    fig.add_shape(type="circle", x0=88.3, y0=49, x1=89.2, y1=51,
                  fillcolor="rgba(140,140,140,0.55)", line=line)
    fig.add_shape(type="line", x0=50, y0=0, x1=50, y1=100, line=line)


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
        hovertemplate="%{y} &middot; %{x}<br>%{text}<extra></extra>"))

    fig.update_layout(
        title=title, height=height,
        margin={"l": 10, "r": 10, "t": 50 if title else 20, "b": 30},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"size": 11})
    fig.update_xaxes(side="top", showgrid=False)
    fig.update_yaxes(autorange="reversed", showgrid=False)
    return fig
