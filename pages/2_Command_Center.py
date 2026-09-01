"""Page 2 - Strategic Command Center.

The prescriptive page: what to do now, and what must stay true for it to still
be right in five gameweeks. Consolidates v1's transfer market, template,
captaincy and role-arbitrage pages behind one decision.

Transfer routes are rendered as visual pathway cards rather than raw solver
text -- a swap is a comparison between two players, and a table of ids is the
wrong shape for a comparison a person has to make in ninety seconds.

Logic lives in the services and models (ADR-001); this file renders.
"""
from __future__ import annotations

import streamlit as st

from fpl_assistant import price_predictor
from fpl_assistant.models import arbitrage as arbitrage_mod
from fpl_assistant.models import template as template_mod
from fpl_assistant.services import briefing as briefing_svc
from fpl_assistant.services import command_center
from fpl_assistant.ui import boot_full
from fpl_assistant.ui.components import (
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
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

FDR_TINT = {1: "#1a7f37", 2: "#4ac26b", 3: "#d4a72c", 4: "#e16f24", 5: "#b3211f"}


@st.cache_data(show_spinner=False, ttl=900)
def _solve(horizon: int, deficit: int, gws_left: int, k: int,
           time_limit: int, _stamp: str):
    _cfg, _conn, _q = boot_full()
    vm = command_center.build(_conn, _cfg, _q, horizon=horizon, deficit=deficit,
                              gameweeks_left=gws_left, run_solver=True,
                              time_limit=time_limit, candidates_k=k)
    _conn.close()
    return vm


@st.cache_data(show_spinner=False, ttl=300)
def _preview(horizon: int, deficit: int, gws_left: int, _stamp: str):
    _cfg, _conn, _q = boot_full()
    vm = command_center.build(_conn, _cfg, _q, horizon=horizon, deficit=deficit,
                              gameweeks_left=gws_left, run_solver=False)
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
        gws_left = c2.number_input("GWs remaining", 1, 38, 20)
        deficit = st.number_input(
            "Points behind your target rival", -200, 200, 0,
            help="Negative means you lead. Drives the Shield/Sword regime.")

quality_bar(quality)

if not quality.has_projections:
    st.warning("No expected-points projections yet.")
    if st.button("Compute projections", type="primary"):
        with st.spinner("Projecting..."):
            from fpl_assistant.services.gw_summary import ensure_projections
            n = ensure_projections(conn, state.planning_window(horizon),
                                   understat_ok=not quality.understat_offline)
        st.success(f"Projected {n} player-gameweeks.")
        st.cache_data.clear()
        st.rerun()
    st.stop()

stamp = quality.projection_run_id or "none"
anchor = state.anchor_gw

with st.spinner("Loading planning view..."):
    vm = _preview(horizon, int(deficit), int(gws_left), stamp)
for err in vm.errors:
    st.warning(err)

squad_ids, squad_gw = command_center.current_squad(conn)

# --- status strip ----------------------------------------------------------
with error_boundary("Status strip", quality=quality):
    if not vm.has_squad:
        empty_state("No squad loaded",
                    "Set `FPL_TEAM_ID` in `.env`, then run **My squad** on "
                    "Refresh Config. Market views below still work.")
    else:
        cols = st.columns(4)
        metric_card(cols[0], "Free transfers", vm.free_transfers)
        metric_card(cols[1], "Bank", f"£{vm.bank:.1f}m")
        metric_card(cols[2], "Team value", f"£{vm.team_value:.1f}m")
        best = vm.recommended_route
        metric_card(cols[3], "Best route xP",
                    f"{best.total_xp:.1f}" if best else "-",
                    best.label if best else "solve to populate")

# --- actionable alert strip ------------------------------------------------
# Tactical badges and tonight's price moves were both reachable only by
# opening a tab, which is the wrong place for the two things that are time
# critical: a price change happens tonight whether or not you scrolled, and a
# player deployed out of position is the cheapest edge on the page.
with error_boundary("Alert strip", quality=quality):
    alert_left, alert_right = st.columns(2)

    with alert_left:
        badged = [p for p in arbitrage_mod.squad_profiles(conn, squad_gw or 0)
                  if p.badges] if squad_gw else []
        st.markdown("##### \U0001F3AD Tactical badges in your squad")
        if badged:
            st.markdown(" &nbsp; ".join(
                f"<span style='background:rgba(63,127,212,0.16);"
                f"border:1px solid rgba(63,127,212,0.5);border-radius:4px;"
                f"padding:2px 7px;font-size:.82rem;white-space:nowrap'>"
                f"<b>{p.player}</b> {p.badge_text()}</span>"
                for p in badged[:8]), unsafe_allow_html=True)
            st.caption("Out-of-position and set-piece duty, from the deployed "
                       "role rather than the FPL listing. Full scan in the "
                       "Role Arbitrage tab below.")
        else:
            st.caption("No tactical badges on your squad yet — needs at least "
                       "one 60-minute appearance per player.")

    with alert_right:
        st.markdown("##### \U0001F4B0 Price moves tonight")
        try:
            strip = price_predictor.ticker(conn, limit=4, squad=squad_ids)
            owned_fall = strip.owned_falling
            if owned_fall:
                st.markdown(" &nbsp; ".join(
                    f"<span style='background:rgba(179,33,31,0.14);"
                    f"border:1px solid rgba(179,33,31,0.5);border-radius:4px;"
                    f"padding:2px 7px;font-size:.82rem'>\U0001F53B "
                    f"<b>{f.player}</b> £{f.now_cost:.1f}m</span>"
                    for f in owned_fall[:5]), unsafe_allow_html=True)
                st.caption(f"**You own these and they may drop in "
                           f"~{strip.hours_to_change:.0f}h** — selling before "
                           f"the change protects team value.")
            elif strip.rising:
                st.markdown(" &nbsp; ".join(
                    f"<span style='background:rgba(26,127,55,0.14);"
                    f"border:1px solid rgba(26,127,55,0.5);border-radius:4px;"
                    f"padding:2px 7px;font-size:.82rem'>\U0001F53A "
                    f"<b>{f.player}</b> £{f.now_cost:.1f}m</span>"
                    for f in strip.rising[:5]), unsafe_allow_html=True)
                st.caption(f"Rising in ~{strip.hours_to_change:.0f}h. Nothing "
                           "you own is falling. Full ticker below.")
            else:
                st.caption("No price movement predicted.")
        except Exception as exc:  # noqa: BLE001 - strip must never block the page
            st.caption(f"Price ticker unavailable: {exc}")

# --- 1-click tactical briefing --------------------------------------------
st.divider()
brief_col, spacer = st.columns([1, 3])
if brief_col.button("⚡ Generate Tactical Briefing", type="primary",
                    width="stretch"):
    st.session_state["briefing_open"] = True

if st.session_state.get("briefing_open"):
    with error_boundary("Tactical briefing", quality=quality):
        with st.spinner("Assembling the briefing..."):
            brief = briefing_svc.build(conn, cfg, anchor, deficit=int(deficit),
                                       gameweeks_left=int(gws_left),
                                       squad_gw=squad_gw)
            markdown = briefing_svc.to_markdown(brief)

        with st.expander(f"⚡ Tactical Briefing — Gameweek {anchor}",
                         expanded=True):
            if brief.has_squad:
                top = st.columns(4)
                metric_card(top[0], "Formation", brief.formation)
                metric_card(top[1], "Projected", f"{brief.projected_points} pts")
                metric_card(top[2], "Captain",
                            brief.captain.web_name if brief.captain else "-")
                metric_card(top[3], "Alerts", len(brief.alerts),
                            "availability" if brief.alerts else "all clear")

            # The captaincy call is the single decision this briefing exists to
            # settle, so it gets a banner colour-coded by regime rather than a
            # line of body text: Shield is a defensive posture (blue), Sword an
            # aggressive one (amber), and the verdict has to survive a glance.
            if brief.regime is not None and brief.captain is not None:
                shielding = brief.regime.regime.value == "shield"
                accent = "#3f7fd4" if shielding else "#d4a72c"
                icon = "\U0001F6E1" if shielding else "⚔"
                c = brief.captain
                st.markdown(
                    f"<div style='border-left:5px solid {accent};"
                    f"background:linear-gradient(90deg,{accent}22,transparent);"
                    f"padding:12px 16px;border-radius:6px;margin:10px 0'>"
                    f"<div style='font-size:.78rem;letter-spacing:.09em;"
                    f"opacity:.75;text-transform:uppercase'>"
                    f"{icon} {brief.regime.regime.value} regime — captaincy "
                    f"verdict</div>"
                    f"<div style='font-size:1.5rem;font-weight:700;"
                    f"margin:2px 0 6px'>{c.web_name} (C) "
                    f"<span style='font-size:.95rem;font-weight:400;opacity:.8'>"
                    f"{c.team_short} · {c.position}</span></div>"
                    f"<div style='font-size:.9rem;opacity:.9'>"
                    f"xP <b>{c.xp:.2f}</b> &nbsp;·&nbsp; rival captain EO "
                    f"<b>{c.ileo_cap:.0%}</b> &nbsp;·&nbsp; haul chance "
                    f"<b>{c.p_haul:.0%}</b> &nbsp;·&nbsp; floor "
                    f"<b>{c.p_floor:.0%}</b></div>"
                    f"<div style='font-size:.87rem;opacity:.8;margin-top:6px'>"
                    f"{brief.regime.reason}</div></div>",
                    unsafe_allow_html=True)

                alt_cols = st.columns(2)
                if brief.shield_pick is not None:
                    s = brief.shield_pick
                    alt_cols[0].markdown(
                        f"\U0001F6E1 **Shield pick — {s.web_name}** &nbsp; "
                        f"<span style='opacity:.75'>protects rank: "
                        f"{s.p_floor:.0%} floor, score {s.shield:.2f}</span>",
                        unsafe_allow_html=True)
                if brief.sword_pick is not None:
                    w = brief.sword_pick
                    alt_cols[1].markdown(
                        f"⚔ **Sword pick — {w.web_name}** &nbsp; "
                        f"<span style='opacity:.75'>chases rank: "
                        f"{w.p_haul:.0%} haul, score {w.sword:.2f}</span>",
                        unsafe_allow_html=True)

            if brief.alerts:
                st.error("**Availability** — " + " · ".join(
                    f"{a['player']} ({a['availability']:.0%})"
                    for a in brief.alerts[:6]))

            st.markdown(markdown)

            export_cols = st.columns([1, 1, 2])
            export_cols[0].download_button(
                "\U0001F4E5 Markdown", markdown,
                file_name=f"fpl-briefing-gw{anchor}.md",
                mime="text/markdown", width="stretch")
            # A real PDF needs a renderer this project deliberately does not
            # ship; the browser's own print dialogue produces a better one and
            # costs no dependency, so the button is honest about being a hint.
            export_cols[1].button("\U0001F5A8 Print / PDF", width="stretch",
                                  help="Use your browser's print dialogue "
                                       "(Ctrl+P) and choose 'Save as PDF'.")
            if export_cols[2].button("Close briefing"):
                st.session_state["briefing_open"] = False
                st.rerun()
            st.caption(
                "Composed from the same modules the pages render, so it cannot "
                "disagree with them.")

# --- transfer pathways -----------------------------------------------------
st.divider()
st.subheader("Transfer pathways")
st.caption(
    "Three routes over the horizon from one ILP model with different risk "
    "profiles. Conservative banks transfers and avoids hits; Aggressive takes "
    "hits when the expected gain covers them; the Chip Enabler builds toward a "
    "chip week.")

solve_cols = st.columns([1.2, 1, 1, 2])
run = solve_cols[0].button("Solve routes", type="primary",
                           width="stretch",
                           disabled=not vm.has_squad)
candidates_k = solve_cols[1].slider("Candidates", 20, 60, 40, step=5)
time_limit = solve_cols[2].slider("Time limit (s)", 5, 60, 30, step=5)

if run:
    st.session_state["solve"] = True

if st.session_state.get("solve") and vm.has_squad:
    with error_boundary("Solver", quality=quality):
        with st.spinner("Solving three routes..."):
            solved = _solve(horizon, int(deficit), int(gws_left),
                            int(candidates_k), int(time_limit), stamp)

        paths = solved.routes or []
        if not paths:
            st.info("The solver returned no feasible route. "
                    "Check the relaxation notes on the status strip.")
        else:
            players = {int(r["id"]): dict(r) for r in conn.execute(
                """SELECT p.id, p.web_name, p.now_cost, p.position, p.team_id,
                          t.short_name AS team
                   FROM players p LEFT JOIN teams t ON t.id = p.team_id""")}
            fdr = {}
            for r in conn.execute(
                    """SELECT team_h, team_a, team_h_difficulty,
                              team_a_difficulty FROM fixtures WHERE event = ?""",
                    (anchor,)):
                fdr[r["team_h"]] = r["team_h_difficulty"]
                fdr[r["team_a"]] = r["team_a_difficulty"]

            def _pill(pid: int) -> str:
                p = players.get(pid, {})
                name = p.get("web_name", f"#{pid}")
                cost = p.get("now_cost") or 0.0
                team_fdr = fdr.get(p.get("team_id"), 3)
                return (f"{name} (£{cost:.1f}m, "
                        f"{p.get('team', '?')}, FDR {team_fdr or '-'})")

            route_cols = st.columns(len(paths))
            for col, path in zip(route_cols, paths):
                with col:
                    st.markdown(f"#### {path.label}")
                    st.caption(f"{path.status} · {path.total_xp:.1f} xP over "
                               f"{horizon} GWs · {path.total_hits} hit(s)")
                    # A route can be all-rolls: every step legal but empty.
                    # That is a real recommendation ("bank the transfer"), so
                    # it needs saying rather than leaving a blank card.
                    active = [s for s in path.steps if s.moves or s.chip]
                    if not active:
                        st.info("No move recommended - hold and bank the "
                                "free transfer.")
                    for step in active:
                        header = f"**GW{step.gw}**"
                        if step.chip:
                            header += f" · chip: `{step.chip}`"
                        st.markdown(header)
                        for move in step.moves:
                            st.markdown(
                                f"<div style='border-left:3px solid #3f7fd4;"
                                f"padding:6px 10px;margin:4px 0;"
                                f"background:rgba(63,127,212,0.08);"
                                f"border-radius:4px;font-size:0.86rem'>"
                                f"<b style='color:#1a7f37'>[IN]</b> "
                                f"{_pill(move.player_in)}<br>"
                                f"<b style='color:#b3211f'>[OUT]</b> "
                                f"{_pill(move.player_out)}<br>"
                                f"<span style='opacity:.75'>"
                                f"Δ xP {move.xp_delta:+.2f} · "
                                f"Δ cost £{move.cost_delta:+.1f}m</span>"
                                f"</div>", unsafe_allow_html=True)
                        st.caption(
                            f"FTs: {step.ft_before} → {step.ft_after} · "
                            f"Bank: £{step.bank_after:.1f}m"
                            + (f" · −{step.hits * 4} pts hit"
                               if step.hits else ""))
                    if path.relaxations:
                        panel_badge("relaxed: " + ", ".join(path.relaxations),
                                    "warn")

# --- transfer market ticker ------------------------------------------------
st.divider()
st.subheader("Transfer market ticker")

with error_boundary("Price ticker", quality=quality):
    with st.spinner("Reading transfer flow..."):
        ticker = price_predictor.ticker(conn, limit=8, squad=squad_ids)

    caveat = ticker.caveat
    if caveat:
        panel_badge(caveat, "warn")
    st.caption(f"Next price change in ~{ticker.hours_to_change:.1f}h. "
               "Buying before a rise and selling before a fall is how team "
               "value compounds over a season.")

    rise_col, fall_col = st.columns(2)
    with rise_col:
        st.markdown("##### \U0001F4C8 Likely to rise tonight")
        if ticker.rising:
            dataframe([{
                "Player": f.player, "Team": f.team, "£": f"{f.now_cost:.1f}",
                "Own %": f"{f.ownership:.1f}", "Net": f"{f.net_transfers:+,}",
                "P(rise)": f"{f.p_rise:.0%}", "Confidence": f.confidence,
            } for f in ticker.rising])
            st.caption("**Buy trigger** - purchase before the change to bank "
                       "the extra 0.1m of team value.")
        else:
            st.info("No rise candidates.")
    with fall_col:
        st.markdown("##### \U0001F4C9 Likely to fall tonight")
        if ticker.falling:
            dataframe([{
                "Player": f.player, "Team": f.team, "£": f"{f.now_cost:.1f}",
                "Own %": f"{f.ownership:.1f}", "Net": f"{f.net_transfers:+,}",
                "P(fall)": f"{f.p_fall:.0%}", "Confidence": f.confidence,
            } for f in ticker.falling])
            st.caption("**Sell trigger** - a fall you own is value leaving "
                       "your team tonight.")
        else:
            st.info("No fall candidates.")

    if ticker.owned_falling:
        st.warning("In your squad and falling: "
                   + ", ".join(f.player for f in ticker.owned_falling))

    with st.expander("Ownership migration by price bracket"):
        st.caption(
            "Where the market's money is moving. A drain from premiums into "
            "mid-price assets is the signature of a template shift, and it is "
            "invisible player by player.")
        dataframe([{
            "Bracket": m["bracket"], "Players": m["players"],
            "Net transfers": f"{m['net_transfers']:+,}",
            "Rising": m["rising"], "Falling": m["falling"],
            "Flow": m["flow"],
        } for m in ticker.migration])

# --- captaincy, template, arbitrage ---------------------------------------
st.divider()
cap_tab, template_tab, arb_tab = st.tabs(
    ["\U0001F1E8 Captaincy (Shield vs Sword)",
     "\U0001F4CA Template vs Differentials",
     "\U0001F3AD Role Arbitrage"])

with cap_tab, error_boundary("Captaincy matrix", quality=quality):
    options = vm.captains
    call = vm.regime
    if call is not None:
        st.info(f"**{call.regime.value.upper()}** — {call.reason}")
    if not options:
        st.info("No captaincy options - project the planning window first.")
    else:
        dataframe([{
            "Player": o.web_name, "Team": o.team_short, "Pos": o.position,
            "xP": round(o.xp, 2), "Rival cap EO": f"{o.ileo_cap:.0%}",
            "P(haul)": f"{o.p_haul:.0%}", "P(floor)": f"{o.p_floor:.0%}",
            "Shield": round(o.shield, 2), "Sword": round(o.sword, 2),
            "Type": o.classification,
        } for o in options])
        st.caption(
            "Shield protects a lead by matching the field's exposure; "
            "Sword chases a deficit by taking unmatched variance. The "
            "regime above decides which column to read.")

with template_tab, error_boundary("Template analysis", quality=quality):
    relax = st.slider("Max mean FDR over the next 3", 2.0, 5.0,
                      template_mod.DIFFERENTIAL_MAX_FDR, step=0.1,
                      key="diff_fdr")
    report = template_mod.build(conn, anchor, squad=squad_ids,
                                max_fdr=relax)
    if report.basis_caveat:
        panel_badge(report.basis_caveat, "warn")

    left, right = st.columns(2)
    with left:
        st.markdown("##### Template core (>50% owned)")
        st.caption(f"Coverage: **{report.coverage:.0%}** of the core. "
                   "These do not win rank; missing them loses it.")
        if report.core:
            dataframe([{
                "Player": a.player, "Team": a.team, "£": f"{a.cost:.1f}",
                "Owned %": a.ownership, "Cap %": a.captaincy,
                "You": "yes" if a.owned else "NO", "Risk": a.risk,
            } for a in report.core])
        else:
            st.info("No asset reaches the template threshold yet.")
    with right:
        st.markdown("##### Differential punches (<10% owned)")
        st.caption(
            f"Ownership < {template_mod.DIFFERENTIAL_OWNERSHIP:.0f}%, xGI90 "
            f"in the top quintile (≥ {report.xgi_threshold:.2f}), and a "
            f"next-3 fixture run at or below {relax:.1f} FDR.")
        if report.differentials:
            dataframe([{
                "Player": d.player, "Team": d.team, "£": f"{d.cost:.1f}",
                "Own %": d.ownership, "xGI90": d.xgi90,
                "pct": d.xgi_percentile, "FDR3": d.next_fdr,
                "Next 3": d.fixtures, "Upside": d.upside,
                "Badges": " ".join(d.badges),
            } for d in report.differentials])
        else:
            st.info(report.binding_gate or "No differentials this week.")
            if report.funnel:
                st.caption("Filter funnel: " + " → ".join(
                    f"{k}: {v}" for k, v in report.funnel.items()))

with arb_tab, error_boundary("Role arbitrage", quality=quality):
    st.caption(
        "FPL scores by *listed* position. A midfielder playing as a striker "
        "banks 5 points a goal and keeps a clean-sheet point; a defender "
        "pushed up the wing banks 6 and keeps the 4-point clean sheet. "
        "Detection needs attacking output above positional peers **and** "
        "defensive workload below them.")
    with st.spinner("Scanning deployed roles..."):
        profiles = arbitrage_mod.candidates(conn, limit=20)
    if not profiles:
        st.info("No arbitrage candidates - needs at least one gameweek "
                "of 60+ minute appearances.")
    else:
        dataframe([{
            "Player": p.player, "Team": p.team, "Pos": p.position,
            "£": f"{p.cost:.1f}", "Own %": p.ownership,
            "Badges": p.badge_text() or "-",
            "Attack ratio": p.attack_ratio,
            "Defence ratio": p.defence_ratio,
            "Premium/90": p.premium_per90,
            "Score": arbitrage_mod.score(p),
        } for p in profiles])
        st.caption(
            "Attack ratio and defence ratio are multiples of the median "
            "for the player's listed position. Premium/90 is the points "
            "earned purely from the classification, not the football.")
