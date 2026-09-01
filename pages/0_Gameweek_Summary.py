"""Page 1 - Gameweek Performance & Mini-League Benchmark.

Answers "where did my rank actually come from" against a named rival set rather
than the abstract global field. Renders one view-model; all logic lives in
`services.gw_summary` (ADR-001).
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from fpl_assistant.services import gw_summary
from fpl_assistant.strategy.eo import Exposure
from fpl_assistant.ui import boot_full
from fpl_assistant.ui.components import (
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
    skeleton_table,
    temporal_header,
)

st.set_page_config(page_title="Gameweek Summary", page_icon="\U0001F4CA",
                   layout="wide")

cfg, conn, quality = boot_full()

st.title("\U0001F4CA Gameweek Summary")

blocking = quality.blocking_reason()
if blocking:
    empty_state("No data yet", blocking, icon="\U0001F6A6")
    st.stop()

# --- gameweek selector -----------------------------------------------------
with error_boundary("Page header", quality=quality, fatal=True):
    import fpl_assistant.temporal as temporal_mod

    state = temporal_mod.gw_state(conn)
    played = [r["gw"] for r in conn.execute(
        "SELECT DISTINCT gw FROM player_gw ORDER BY gw DESC")]
    if not played:
        empty_state("No gameweek history",
                    "Run **History (learning)** on the Refresh Config page.")
        st.stop()

    head, picker = st.columns([3, 1])
    with head:
        temporal_header(state)
    gw = picker.selectbox("Gameweek", played, index=0,
                          format_func=lambda g: f"GW{g}")

quality_bar(quality)

with st.spinner("Building gameweek view..."):
    vm = gw_summary.build(conn, cfg, quality, gw=gw)

for err in vm.errors:
    st.warning(err)

# --- KPI row ---------------------------------------------------------------
with error_boundary("Headline metrics", quality=quality):
    if not vm.has_squad:
        empty_state(
            "No squad loaded",
            "Set `FPL_TEAM_ID` in `.env`, then run **My squad** on Refresh Config.")
    else:
        c1, c2, c3, c4, c5 = st.columns(5)
        metric_card(c1, "GW points", vm.kpis.my_points,
                    caption=f"{vm.kpis.players_played}/11 played")
        metric_card(c2, "vs average",
                    f"{vm.kpis.vs_average:+d}" if vm.kpis.average_points else "—",
                    caption=(f"avg {vm.kpis.average_points}"
                             if vm.kpis.average_points
                             else "needs gw_state ingest"))
        metric_card(c3, "Squad xP",
                    vm.kpis.xp_total if vm.kpis.xp_total else "—",
                    caption="projection for this GW"
                    if vm.kpis.xp_total else "not projected")
        metric_card(c4, "Luck index",
                    f"{vm.kpis.luck_index:+.1f}" if vm.kpis.xp_total else "—",
                    caption=vm.kpis.luck_label if vm.kpis.xp_total else "needs xP")
        metric_card(c5, "ML rank",
                    f"{vm.kpis.league_rank}" if vm.kpis.league_rank else "—",
                    caption=(f"of {vm.kpis.league_size}" if vm.kpis.league_size
                             else "no league tracked"))

st.divider()

tab_swing, tab_template, tab_var, tab_bench = st.tabs(
    ["Swing matrix (ILEO)", "Template", "Luck vs Process", "Bench & autosubs"])

# ===========================================================================
# ILEO / swing matrix
# ===========================================================================
with tab_swing, error_boundary("ILEO matrix", quality=quality):
    st.caption(
        "Global ownership is the wrong denominator when you are racing named "
        "people. ILEO measures exposure across your actual rivals, and the "
        "**swing** column is what you gain per point a player scores.")

    if not vm.rival_options:
        empty_state(
            "No mini-league rivals loaded",
            "Open **Leagues & Rivals** and press *Discover my leagues*. Your "
            "mini-leagues are read straight from your FPL entry — nothing to "
            "type in. Rival squads are then frozen at each deadline, so they "
            "can only be captured after a deadline has passed.",
            icon="\U0001F465")
    else:
        labels = {r["entry_id"]: f"{r.get('player_name') or r['entry_id']} "
                                 f"(#{r.get('rank') or '?'})"
                  for r in vm.rival_options}
        # The saved rival set is the default; the multiselect is there to
        # narrow it for one look, not to be rebuilt from scratch every visit.
        import fpl_assistant.leagues as leagues_mod

        saved = [e for e in leagues_mod.rival_ids(conn) if e in labels]
        chosen = st.multiselect(
            "Rival set", options=list(labels),
            default=saved or list(labels)[:8],
            format_func=lambda e: labels.get(e, str(e)),
            help="Edit and save the persistent set on the "
                 "**Leagues & Rivals** page.")

        if not chosen:
            empty_state("No rivals selected",
                        "Pick at least one rival above to build the matrix.")
        else:
            vm = gw_summary.build(conn, cfg, quality, gw=gw, rival_ids=chosen)
            matrix = vm.swing

            if matrix is None or not matrix.rows:
                empty_state(
                    "No frozen rival squads for this gameweek",
                    f"Rival picks for GW{gw} have not been captured. They are "
                    "snapshotted once, after the deadline passes.")
            else:
                if matrix.coverage_note:
                    panel_badge(matrix.coverage_note, "warn")
                if matrix.frozen:
                    panel_badge("Rival squads frozen at the deadline", "ok")

                rows = [{
                    "Player": r.web_name, "Pos": r.position, "Team": r.team_short,
                    "Pts": r.points, "Mine": r.my_multiplier, "ILEO": r.ileo,
                    "Swing": r.swing, "Net": r.realised_swing,
                    "Exposure": {
                        Exposure.OVER: "needs to haul",
                        Exposure.UNDER: "needs to blank",
                        Exposure.NEUTRALISED: "neutralised",
                        Exposure.IRRELEVANT: "irrelevant",
                    }[r.exposure],
                } for r in matrix.rows]

                dataframe(
                    rows,
                    columns=["Player", "Pos", "Team", "Pts", "Mine", "ILEO",
                             "Swing", "Net", "Exposure"],
                    column_config={
                        "Swing": st.column_config.NumberColumn(
                            help="my multiplier minus ILEO; "
                                 "points gained per point scored",
                            format="%.2f"),
                        "Net": st.column_config.NumberColumn(
                            "Net swing", help="swing x actual points",
                            format="%.1f"),
                    },
                    height=420)

                st.metric("Net swing vs rival mean",
                          f"{matrix.net_realised():+.1f} pts")

                b1, b2, b3 = st.columns(3)
                with b1:
                    st.markdown("**Needs to haul**")
                    st.caption("you own, the field mostly does not")
                    for r in matrix.needs_haul()[:8]:
                        st.write(f"- {r.web_name} ({r.swing:+.2f})")
                with b2:
                    st.markdown("**Needs to blank**")
                    st.caption("the field owns, you do not")
                    for r in matrix.needs_blank()[:8]:
                        st.write(f"- {r.web_name} ({r.swing:+.2f})")
                with b3:
                    st.markdown("**Irrelevant**")
                    st.caption("shared - cannot move your rank")
                    neutral = matrix.neutralised()
                    for r in neutral[:6]:
                        st.write(f"- {r.web_name}")
                    if len(neutral) > 6:
                        st.caption(f"...and {len(neutral) - 6} more")

# ===========================================================================
# Template
# ===========================================================================
with tab_template, error_boundary("Template", quality=quality):
    st.caption(f"What the top {cfg.top_managers_sample} sampled managers own "
               "and captain, against your own holdings.")
    if not vm.template:
        empty_state("No template data",
                    "Run **Template (top managers)** on Refresh Config.")
    else:
        mine = {r["player_id"] for r in conn.execute(
            "SELECT player_id FROM my_picks WHERE gw = (SELECT MAX(gw) FROM my_picks)")}
        dataframe(
            [{"Player": r["web_name"], "Pos": r["position"],
              "Team": r["team_short"],
              "Top own%": round(float(r["ownership_pct"] or 0), 1),
              "Top capt%": round(float(r["captain_pct"] or 0), 1),
              "Overall own%": r["overall_own"],
              "Owned": "yes" if r["player_id"] in mine else ""}
             for r in vm.template],
            columns=["Player", "Pos", "Team", "Top own%", "Top capt%",
                     "Overall own%", "Owned"],
            height=460)

# ===========================================================================
# Luck vs Process
# ===========================================================================
with tab_var, error_boundary("Variance analysis", quality=quality):
    st.caption(
        "Actual points split into **process** (underlying numbers vs forecast - "
        "repeatable) and **luck** (conversion vs underlying - largely not). "
        "The high-process / low-luck quadrant is the only systematic way to buy "
        "a player before the market prices them in.")

    if quality.on_baseline or quality.understat_offline:
        panel_badge(
            quality.understat_badge
            or "Baseline stats: xG/xA from the FPL API, not Understat "
               "shot-level data", "warn")

    if vm.variance_caveat:
        st.info(vm.variance_caveat)

    if not vm.variance:
        skeleton_table(rows=6, cols=6, label="No variance data")
        empty_state("Nothing to decompose",
                    "Needs **History (learning)** ingested for this gameweek.")
    else:
        frame = pd.DataFrame([{
            "Player": r.web_name, "Team": r.team_short, "Pos": r.position,
            "Process": r.process, "Luck": r.luck, "Actual": r.actual,
            "xP": r.xp, "Verdict": r.verdict,
        } for r in vm.variance])

        chart = (
            alt.Chart(frame)
            .mark_circle(size=140, opacity=0.85)
            .encode(
                x=alt.X("Process:Q", title="Process (repeatable) →"),
                y=alt.Y("Luck:Q", title="Luck (not repeatable) →"),
                color=alt.Color("Verdict:N", legend=alt.Legend(title="Verdict")),
                tooltip=["Player", "Team", "Pos", "Actual", "xP",
                         "Process", "Luck", "Verdict"],
            )
            .properties(height=380)
        )
        rules = (alt.Chart(pd.DataFrame({"v": [0]}))
                 .mark_rule(strokeDash=[4, 4], opacity=0.5))
        st.altair_chart(
            chart + rules.encode(x="v:Q") + rules.encode(y="v:Q"),
            width='stretch')

        st.markdown("#### xP breakdown by component")
        dataframe(
            [{"Player": r.web_name, "Pos": r.position, "Mins": r.minutes,
              "Actual": r.actual, "xP": r.xp,
              "Attacking": r.attacking_xp, "Defensive": r.defensive_xp,
              "Bonus": r.bonus_xp,
              "Process": r.process, "Luck": r.luck,
              "Verdict": r.verdict, "Action": r.action, "Source": r.source}
             for r in sorted(vm.variance, key=lambda r: r.luck)],
            columns=["Player", "Pos", "Mins", "Actual", "xP", "Attacking",
                     "Defensive", "Bonus", "Process", "Luck", "Verdict",
                     "Action", "Source"],
            height=420)

        left, right = st.columns(2)
        with left:
            st.markdown("**★ Buy candidates** (underlying ahead of returns)")
            if vm.buy_candidates:
                for r in vm.buy_candidates[:5]:
                    st.write(f"- **{r.web_name}** ({r.team_short}) "
                             f"— luck {r.luck:+.1f}, actual {r.actual:.0f}")
            else:
                st.caption("None this gameweek.")
        with right:
            st.markdown("**Sell candidates** (returns ahead of underlying)")
            if vm.sell_candidates:
                for r in vm.sell_candidates[:5]:
                    st.write(f"- **{r.web_name}** ({r.team_short}) "
                             f"— luck {r.luck:+.1f}, actual {r.actual:.0f}")
            else:
                st.caption("None this gameweek.")

# ===========================================================================
# Bench
# ===========================================================================
with tab_bench, error_boundary("Bench", quality=quality):
    if not vm.bench:
        empty_state("No bench data",
                    "Needs **My squad** ingested for this gameweek.")
    else:
        wasted = sum(int(b["pts"]) for b in vm.bench)
        st.metric("Points left on the bench", wasted)
        dataframe(
            [{"Player": b["web_name"], "Pos": b["position"],
              "Slot": b["slot"], "Points": b["pts"], "Minutes": b["minutes"]}
             for b in vm.bench],
            columns=["Player", "Pos", "Slot", "Points", "Minutes"])

st.divider()
st.caption(
    f"Data as of {quality.fpl_last_ingest or 'unknown'} · "
    f"xP source mix: {quality.xp_source_mix or 'none'}")
