"""Page 1 - Schedule & Congestion.

Merges v1's fixture planner and rotation/congestion pages. They answered one
question between them -- "who plays, and how hard is it?" -- and splitting it
across two screens meant cross-referencing to get an answer.

All logic lives in `services.schedule` (ADR-001); this file only renders.
"""
from __future__ import annotations

import streamlit as st

from fpl_assistant.services import schedule as schedule_svc
from fpl_assistant.ui import boot_full, charts
from fpl_assistant.ui.components import (
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
)

st.set_page_config(page_title="Schedule & Congestion", page_icon="\U0001F4C5",
                   layout="wide")

cfg, conn, quality = boot_full()

st.title("\U0001F4C5 Schedule & Congestion")

blocking = quality.blocking_reason()
if blocking:
    empty_state("No data yet", blocking, icon="\U0001F6A6")
    st.stop()

# --- controls --------------------------------------------------------------
controls = st.columns([1.1, 1.2, 1.2, 2])
horizon = controls[0].slider("Horizon (GWs)", schedule_svc.MIN_HORIZON,
                             schedule_svc.MAX_HORIZON,
                             schedule_svc.DEFAULT_HORIZON)
sort_by = controls[1].selectbox(
    "Sort teams by", ["Easiest run", "Hardest run", "Most good fixtures",
                      "Alphabetical"])
rotation_positions = controls[2].multiselect(
    "Rotation positions", ["DEF", "GKP", "MID", "FWD"], default=["DEF", "GKP"])
max_price = controls[3].slider(
    "Rotation budget ceiling (per player, £m)", 4.0, 6.5,
    schedule_svc.ROTATION_MAX_PRICE, step=0.1)

quality_bar(quality)

with st.spinner("Building the fixture picture..."):
    vm = schedule_svc.build(
        conn, cfg, horizon=horizon,
        rotation_positions=tuple(rotation_positions or ("DEF",)),
        max_price=max_price)

if not vm.rows:
    empty_state("No fixtures", "Run **FPL data** on the Refresh Config page.")
    st.stop()

for note in vm.notes:
    panel_badge(note, "warn")

# --- FDR heatmap -----------------------------------------------------------
st.subheader("Fixture difficulty")
st.caption(
    f"GW{vm.gws[0]}-GW{vm.gws[-1]}. Green is easier, red is harder; a blank "
    "gameweek scores as the hardest case because having no fixture is the "
    "worst outcome for a run.")

with error_boundary("FDR heatmap", quality=quality):
    rows = list(vm.rows)
    if sort_by == "Hardest run":
        rows.sort(key=lambda r: -r.mean_fdr)
    elif sort_by == "Most good fixtures":
        rows.sort(key=lambda r: (-r.good_fixtures, r.mean_fdr))
    elif sort_by == "Alphabetical":
        rows.sort(key=lambda r: r.team)

    teams = [r.team for r in rows]
    grid = [[(c.fdr if c.fdr is not None else 5) for c in r.cells] for r in rows]
    labels = [[c.label for c in r.cells] for r in rows]

    if charts.available():
        st.plotly_chart(
            charts.fdr_heatmap(teams, vm.gws, grid, labels=labels),
            width="stretch")
    else:
        dataframe([{"Team": r.team, "Mean FDR": r.mean_fdr,
                    **{f"GW{c.gw}": c.label for c in r.cells}} for r in rows])

    best, worst = min(rows, key=lambda r: r.mean_fdr), \
        max(rows, key=lambda r: r.mean_fdr)
    tight = [w for w in vm.warnings if w.severity == "high"]
    cols = st.columns(4)
    metric_card(cols[0], "Softest run", best.team, f"{best.mean_fdr} mean FDR")
    metric_card(cols[1], "Hardest run", worst.team, f"{worst.mean_fdr} mean FDR")
    metric_card(cols[2], "Blank gameweeks", sum(r.blanks for r in rows),
                f"across {len(rows)} teams")
    metric_card(cols[3], "Rotation risks", len(tight),
                "tight turnarounds / doubles" if tight else "none in horizon")

# --- rotation pairs --------------------------------------------------------
with st.expander(
        f"\U0001F501 Rotation pair finder "
        f"(£{schedule_svc.ROTATION_MIN_PRICE:.1f}m-£{max_price:.1f}m)",
        expanded=bool(vm.pairs)):
    st.caption(
        "Two cheap assets from different clubs whose easy fixtures fall in "
        "opposite gameweeks, so one of them is always startable. Coverage "
        f"counts gameweeks where at least one faces an FDR of "
        f"{schedule_svc.GOOD_FDR} or better.")

    with error_boundary("Rotation pairs", quality=quality):
        if not vm.pairs:
            st.info(
                "No pairs found in this band with minutes on the board. Widen "
                "the budget ceiling or add positions above.")
        else:
            dataframe([{
                "Pair": f"{p.player_a} ({p.team_a}) + {p.player_b} ({p.team_b})",
                "Pos": p.position,
                "Cost": f"£{p.combined_cost:.1f}m",
                "Coverage": f"{p.covered_gws}/{p.horizon}",
                "Verdict": p.verdict,
                "Mean started FDR": p.mean_best_fdr,
            } for p in vm.pairs])

            chosen = st.selectbox(
                "Inspect a pair",
                range(len(vm.pairs)),
                format_func=lambda i: (f"{vm.pairs[i].player_a} + "
                                       f"{vm.pairs[i].player_b}"))
            pair = vm.pairs[chosen]
            st.markdown(
                f"**Week-by-week plan — {pair.player_a} ({pair.team_a}) / "
                f"{pair.player_b} ({pair.team_b})**")

            # The whole point of a rotation pair is knowing which one to play
            # each week, so the table names the starter AND the player to bench
            # rather than leaving the reader to infer it from two FDR columns.
            plan = []
            for step in pair.schedule:
                start_name = step["start"]
                if start_name == "-":
                    bench_name, verdict = "-", "both blank"
                else:
                    bench_name = (pair.player_b if start_name == pair.player_a
                                  else pair.player_a)
                    fdr = step["fdr"]
                    verdict = ("good week" if fdr is not None
                               and fdr <= schedule_svc.GOOD_FDR
                               else "no good option")
                plan.append({
                    "GW": step["gw"],
                    "▶ START": start_name,
                    "⏸ BENCH": bench_name,
                    "Started FDR": (step["fdr"] if step["fdr"] is not None
                                    else "blank"),
                    "Verdict": verdict,
                    f"{pair.player_a} FDR": (step["a_fdr"]
                                             if step["a_fdr"] is not None
                                             else "blank"),
                    f"{pair.player_b} FDR": (step["b_fdr"]
                                             if step["b_fdr"] is not None
                                             else "blank"),
                })
            dataframe(plan)
            covered = pair.covered_gws
            st.caption(
                f"Starting the named player every week gives a startable "
                f"fixture (FDR {schedule_svc.GOOD_FDR} or better) in "
                f"**{covered} of {pair.horizon}** gameweeks, for "
                f"£{pair.combined_cost:.1f}m across both squad slots.")

# --- congestion ------------------------------------------------------------
with st.expander("⚡ Congestion & fatigue warnings",
                 expanded=bool([w for w in vm.warnings if w.severity == "high"])):
    st.caption(
        f"Doubles, turnarounds under {schedule_svc.TIGHT_TURNAROUND_HOURS}h and "
        "European commitments. Midweek European fixtures are not in the FPL "
        "fixture table, so turnaround gaps are computed from league kickoffs "
        "only and understate rather than overstate the load.")

    with error_boundary("Congestion", quality=quality):
        # Split by kind rather than listing everything at one level. Every club
        # is in both domestic cups, so a combined list is dominated by rows
        # that apply to all twenty teams and the two that matter get lost.
        actionable = [w for w in vm.warnings if w.severity in ("high", "watch")]
        european = [w for w in vm.warnings if w.severity == "european"]

        if not actionable:
            st.success(
                f"No doubles or sub-{schedule_svc.TIGHT_TURNAROUND_HOURS}h "
                "turnarounds in this horizon.")
        else:
            st.warning(
                f"**{len(actionable)} rotation risk(s)** — a tight turnaround "
                "or a double gameweek is where managers rest players.")
            dataframe([{
                "Team": w.team,
                "Risk": ("tight turnaround" if w.turnaround_hours
                         else f"{w.matches} fixtures"),
                "Severity": w.severity,
                "GW": w.gw or "-",
                "Rest": (f"{w.turnaround_hours:.0f}h"
                         if w.turnaround_hours else "-"),
                "Detail": w.note,
            } for w in actionable])

        if european:
            st.markdown("##### Midweek European commitments")
            st.caption(
                f"{len(european)} club(s) playing midweek in Europe. Not a "
                "warning on its own — every club is also in both domestic "
                "cups — but it compounds any tight turnaround above, and it "
                "is the usual reason a nailed starter is rested.")
            dataframe([{"Team": w.team,
                        "Competition": ", ".join(w.competitions)}
                       for w in european])
