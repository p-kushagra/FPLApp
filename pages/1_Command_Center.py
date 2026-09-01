"""Page 2 - Strategic Command Center.

Answers "what is the highest-EV action available now, and what must be true for
it to still be right in five weeks". Renders one view-model; all logic lives in
`services.command_center` (ADR-001).

The ILP is opt-in behind a button: it takes seconds, and a page that solves on
every navigation is a page that feels frozen.
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from fpl_assistant.services import command_center
from fpl_assistant.ui import boot_full
from fpl_assistant.ui.components import (
    assumptions_drawer,
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
    skeleton_cards,
    temporal_header,
)

st.set_page_config(page_title="Command Center", page_icon="\U0001F3AF",
                   layout="wide")

cfg, conn, quality = boot_full()

st.title("\U0001F3AF Command Center")

blocking = quality.blocking_reason()
if blocking:
    empty_state("No data yet", blocking, icon="\U0001F6A6")
    st.stop()


# ---------------------------------------------------------------------------
# Cached solve. Keyed on everything that changes the answer, so a rerun that
# changes nothing is instant and a rerun that changes something re-solves.
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=900)
def _solve(horizon: int, rivals: tuple[int, ...], deficit: int,
           gws_left: int, k: int, time_limit: int, _stamp: str):
    _cfg, _conn, _quality = boot_full()
    vm = command_center.build(
        _conn, _cfg, _quality, horizon=horizon, rival_ids=list(rivals),
        deficit=deficit, gameweeks_left=gws_left, run_solver=True,
        time_limit=time_limit, candidates_k=k)
    _conn.close()
    return vm


@st.cache_data(show_spinner=False, ttl=300)
def _preview(horizon: int, rivals: tuple[int, ...], deficit: int,
             gws_left: int, _stamp: str):
    _cfg, _conn, _quality = boot_full()
    vm = command_center.build(
        _conn, _cfg, _quality, horizon=horizon, rival_ids=list(rivals),
        deficit=deficit, gameweeks_left=gws_left, run_solver=False)
    _conn.close()
    return vm


# --- controls --------------------------------------------------------------
with error_boundary("Page header", quality=quality, fatal=True):
    import fpl_assistant.temporal as temporal_mod

    state = temporal_mod.gw_state(conn)

    head, ctrl = st.columns([3, 2])
    with head:
        temporal_header(state, planning=True, horizon=5)
    with ctrl:
        c1, c2 = st.columns(2)
        horizon = c1.slider("Horizon (GWs)", 3, 5, 5)
        gws_left = c2.number_input("GWs remaining in season", 1, 38, 20)
        deficit = st.number_input(
            "Points behind the rival you are chasing", -200, 200, 0,
            help="Negative means you lead. Drives the Shield/Sword regime.")

quality_bar(quality)

if not quality.has_projections:
    st.warning("No expected-points projections yet.")
    if st.button("Compute projections for the planning window",
                 type="primary"):
        with st.spinner("Projecting..."):
            from fpl_assistant.services.gw_summary import ensure_projections

            n = ensure_projections(
                conn, state.planning_window(horizon),
                understat_ok=not quality.understat_offline)
        st.success(f"Projected {n} player-gameweeks.")
        st.cache_data.clear()
        st.rerun()
    st.stop()

stamp = quality.projection_run_id or "none"
rivals: tuple[int, ...] = ()

with st.spinner("Loading planning view..."):
    vm = _preview(horizon, rivals, int(deficit), int(gws_left), stamp)

for err in vm.errors:
    st.warning(err)

# --- status strip ----------------------------------------------------------
with error_boundary("Status strip", quality=quality):
    if not vm.has_squad:
        empty_state(
            "No squad loaded",
            "Set `FPL_TEAM_ID` in `.env`, then run **My squad** on Refresh "
            "Config. Market-wide views below still work.")
    else:
        s1, s2, s3, s4 = st.columns(4)
        pips = "▪" * vm.free_transfers + "▫" * (5 - vm.free_transfers)
        metric_card(s1, "Free transfers", vm.free_transfers, caption=pips)
        metric_card(s2, "Bank", f"£{vm.bank:.1f}m")
        metric_card(s3, "Team value", f"£{vm.team_value:.1f}m")
        metric_card(s4, "Chips left", len(vm.chips_available),
                    caption=", ".join(vm.chips_available[:3]) or "none")

st.divider()

tab_routes, tab_moves, tab_cap, tab_chips = st.tabs(
    ["Transfer routes", "Top 10 moves", "Captaincy matrix", "Chip horizon"])

# ===========================================================================
# Three solver routes
# ===========================================================================
with tab_routes, error_boundary("Transfer routes", quality=quality):
    st.caption(
        "Three parameterisations of one rolling-horizon ILP over a shared "
        "candidate set, so the objectives are directly comparable.")

    if not vm.has_squad:
        empty_state("Solver needs a squad",
                    "Run **My squad** on Refresh Config first.")
    else:
        run = st.button("Solve transfer routes", type="primary",
                        help="Runs the ILP over the planning window "
                             "(seconds, cached for 15 minutes)")

        placeholder = st.container()
        if run:
            with placeholder:
                skeleton_cards(3)
            with st.spinner("Solving three routes..."):
                solved = _solve(horizon, rivals, int(deficit), int(gws_left),
                                25, 30, stamp)
            placeholder.empty()
            st.session_state["cc_routes"] = solved

        solved = st.session_state.get("cc_routes")
        if solved is None:
            st.info("Press **Solve transfer routes** to run the optimiser.")
        elif not solved.routes:
            empty_state("No routes returned",
                        "The model could not be built - check the warnings above.")
        else:
            best = solved.recommended_route
            cols = st.columns(len(solved.routes))
            for col, path in zip(cols, solved.routes):
                with col, st.container(border=True):
                    star = " ★" if best and path.profile == best.profile else ""
                    st.markdown(f"**{path.label}**{star}")

                    if path.status != "Optimal":
                        st.error(f"{path.status}")
                        if path.relaxations:
                            st.caption("Relaxed: " + ", ".join(path.relaxations))
                        continue

                    if path.relaxations:
                        panel_badge("Relaxed: " + ", ".join(path.relaxations),
                                    "warn")

                    for step in path.steps:
                        if step.chip:
                            st.write(f"**GW{step.gw}: {step.chip.upper()}** "
                                     f"({len(step.moves)} moves)")
                        elif step.is_roll:
                            st.write(f"GW{step.gw}: roll → {step.ft_after} FT")
                        else:
                            for m in step.moves:
                                st.write(f"GW{step.gw}: {m.player_out} → "
                                         f"{m.player_in}")
                            if step.hits:
                                st.caption(f"  hit −{4 * step.hits}")

                    st.divider()
                    m1, m2 = st.columns(2)
                    m1.metric("Net xP", f"{path.net_xp:.1f}")
                    m2.metric("Hits", f"−{4 * path.total_hits}"
                              if path.total_hits else "0")
                    st.caption(
                        f"end FT {path.end_ft} · bank £{path.end_bank:.1f}m · "
                        f"{path.candidate_count} candidates · "
                        f"{path.wall_seconds:.1f}s")

            assumptions_drawer({
                "Horizon": f"{horizon} GWs",
                "Candidates per position": 25,
                "Solver time limit": "30s",
                "Conservative": "0 hits, gamma 0.95, terminal FT 3.0",
                "Aggressive": "<=1 hit, gamma 0.75, differential bonus 1.2",
                "Chip enabler": "<=1 hit, routes toward a chip",
                "FT rules": "cap 5, chip retains bank, chip does NOT accrue",
            })

# ===========================================================================
# Top 10 prescriptive moves
# ===========================================================================
with tab_moves, error_boundary("Transfer suggestions", quality=quality):
    st.caption(
        "Best single swap per outgoing player, ranked by expected-points gain "
        "across the horizon. This answers 'what one move helps most', which is "
        "a different question from the solver's 'what sequence is optimal' - "
        "they disagree often, and the disagreement is informative.")

    if not vm.moves:
        empty_state(
            "No profitable swaps found",
            "Either the squad is already optimal over the candidate set, or "
            "projections are missing. Compute projections above.")
    else:
        dataframe(
            [{"#": i, "OUT": f"{m.out_name} ({m.out_team})",
              "£out": m.out_cost,
              "IN": f"{m.in_name} ({m.in_team})", "£in": m.in_cost,
              "Pos": m.position, "ΔxP": m.xp_delta, "Δ£": m.cost_delta,
              "ILEO": m.ileo or None, "Why": m.rationale}
             for i, m in enumerate(vm.moves, start=1)],
            columns=["#", "OUT", "£out", "IN", "£in", "Pos", "ΔxP", "Δ£",
                     "ILEO", "Why"],
            column_config={
                "ΔxP": st.column_config.NumberColumn(
                    help="expected-points gain over the horizon", format="%.2f"),
                "Δ£": st.column_config.NumberColumn(
                    help="positive costs money, negative frees it",
                    format="%.1f"),
            })

# ===========================================================================
# Captaincy matrix
# ===========================================================================
with tab_cap, error_boundary("Captaincy matrix", quality=quality):
    if vm.regime:
        banner = (st.success if vm.regime.regime.value == "shield" else st.warning)
        banner(f"**REGIME: {vm.regime.regime.value.upper()}** — {vm.regime.reason}")

    if not vm.captains:
        empty_state("No captain candidates",
                    "Needs projections for the anchor gameweek.")
    else:
        if vm.captain_pick:
            st.info(f"**Recommendation:** {vm.captain_reason}")

        frame = pd.DataFrame([{
            "Player": c.web_name, "Team": c.team_short, "xP": c.xp,
            "ILEO_cap": c.ileo_cap, "P(haul)": c.p_haul, "P(floor)": c.p_floor,
            "Shield": c.shield, "Sword": c.sword, "Class": c.classification,
        } for c in vm.captains])

        if frame["ILEO_cap"].sum() == 0:
            panel_badge(
                "No rival captaincy data, so Shield is zero for everyone and "
                "only the Sword axis is meaningful. Freeze a rival set to "
                "populate ILEO.", "warn")

        st.altair_chart(
            alt.Chart(frame)
            .mark_circle(size=160, opacity=0.85)
            .encode(
                x=alt.X("ILEO_cap:Q", title="Captain EO across rivals →",
                        scale=alt.Scale(domain=[0, 1])),
                y=alt.Y("P(haul):Q", title="Ceiling: P(12+ points) →"),
                color=alt.Color("Class:N", legend=alt.Legend(title="Class")),
                tooltip=["Player", "Team", "xP", "ILEO_cap", "P(haul)",
                         "Shield", "Sword"],
            ).properties(height=340),
            width='stretch')

        dataframe(
            frame.to_dict("records"),
            columns=["Player", "Team", "xP", "ILEO_cap", "P(haul)", "P(floor)",
                     "Shield", "Sword", "Class"],
            height=380)

# ===========================================================================
# Chip horizon
# ===========================================================================
with tab_chips, error_boundary("Chip horizon", quality=quality):
    st.caption(
        "Gameweek shape against your squad's coverage of it. Blanks and doubles "
        "the FPL fixture list has not confirmed yet are projected from the cup "
        "calendar and labelled as such.")

    if not vm.horizon:
        empty_state("No fixture horizon",
                    "Run **FPL data** on Refresh Config to load fixtures.")
    else:
        rows = [{
            "GW": w.gw, "Shape": w.kind, "Fixtures": w.fixtures,
            "Squad playing": w.coverage,
            "Blanks": ", ".join(w.blank_teams) or "",
            "Doubles": ", ".join(w.double_teams) or "",
        } for w in vm.horizon]
        dataframe(rows, columns=["GW", "Shape", "Fixtures", "Squad playing",
                                 "Blanks", "Doubles"], height=300)

        chart_frame = pd.DataFrame([
            {"GW": f"GW{w.gw}", "Players with a fixture": w.squad_playing,
             "Shape": w.kind}
            for w in vm.horizon])
        st.altair_chart(
            alt.Chart(chart_frame).mark_bar().encode(
                x=alt.X("GW:N", sort=None),
                y=alt.Y("Players with a fixture:Q"),
                color=alt.Color("Shape:N"),
                tooltip=["GW", "Players with a fixture", "Shape"],
            ).properties(height=240),
            width='stretch')

        if vm.projected_disruption:
            gws = ", ".join(f"GW{d.get('gw')}" for d in vm.projected_disruption)
            st.warning(
                f"⚠ {gws} carry **projected** disruption from the cup "
                "calendar (`config/calendar.yaml`), not confirmed by the FPL "
                "fixture list. Treat as a watch item, not a plan.")
            with st.expander("Projected disruption detail"):
                for d in vm.projected_disruption:
                    comps = ", ".join(
                        c.get("competition", "?")
                        for c in (d.get("collisions") or []))
                    st.write(f"- **GW{d.get('gw')}** "
                             f"({d.get('start_date')}): {comps or 'unknown'}")

        st.markdown("#### Recommended activation windows")
        if not vm.chips:
            empty_state("No chip plan",
                        "Needs fixtures and a loaded squad.")
        else:
            for rec in vm.chips:
                icon = "▶" if rec.action == "play" else "⏸"
                target = f"GW{rec.target_gw}" if rec.target_gw else "—"
                with st.container(border=True):
                    st.markdown(
                        f"{icon} **{rec.chip}** → {target} "
                        f"*(confidence: {rec.confidence})*")
                    st.caption(rec.reason)

st.divider()
st.caption(
    "The planner writes locally only. This app uses the public read-only FPL "
    "API and never posts a transfer to your team.")
