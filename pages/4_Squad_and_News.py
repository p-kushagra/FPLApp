"""Page 4 - Squad & News.

The squad-shaped page: an interactive tactical pitch, Understat shot maps, a
head-to-head radar against mini-league rivals, and a news feed that defaults to
*your* players rather than to a search box.

The news default is the whole point of the consolidation. v1 opened on an empty
search field, which meant the most common question -- "is anyone in my team
injured?" -- required typing fifteen names. Here it is the landing state.
"""
from __future__ import annotations

import dataclasses

import pandas as pd
import streamlit as st

from fpl_assistant import search
from fpl_assistant.models import minutes as minutes_mod
from fpl_assistant.models import stochastic
from fpl_assistant.services import sandbox
from fpl_assistant.ui import boot_full, charts
from fpl_assistant.ui import pitch as pitch_mod
from fpl_assistant.ui.components import (
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
)

st.set_page_config(page_title="Squad & News", page_icon="\U0001F465",
                   layout="wide")

# Plotly's modebar, minus every control that would fight the fixed pitch aspect.
SHOT_MAP_CONFIG = {
    "displaylogo": False,
    "scrollZoom": False,
    "modeBarButtonsToRemove": ["zoom2d", "pan2d", "select2d", "lasso2d",
                               "zoomIn2d", "zoomOut2d", "autoScale2d",
                               "resetScale2d"],
}
# The pitch is a click target, so box- and lasso-select are removed too: they
# produce multi-point selections this page has no meaning for.
PITCH_CONFIG = {**SHOT_MAP_CONFIG, "displayModeBar": False}


def _clicked_player(event, squad) -> int | None:
    """Unwrap Streamlit's event, then resolve it in `pitch` where it is tested."""
    selection = getattr(event, "selection", None) if event is not None else None
    return pitch_mod.player_from_selection(selection, squad)


def _run_monte_carlo(conn, state):
    """10k runs over the scenario XI, honouring the active chip.

    Bench Boost widens the pool to all 15 and Triple Captain lifts the armband
    to 3x, so the simulation answers the same question the impact bar does
    rather than a different one that happens to be nearby.
    """
    scorers = (state.squad if state.chip == "bench_boost" else state.starters)
    captain = state.captain
    multipliers = {}
    if captain is not None:
        multipliers[captain.player_id] = float(
            sandbox.captain_multiplier(state.chip))
    return stochastic.simulate_squad(
        conn, state.gw, [p.player_id for p in scorers],
        multipliers=multipliers, runs=10_000)


def _roster_card(candidate, state, selected, conn) -> None:
    """One transfer target: the numbers, then the one action.

    Refusals are shown on the card that caused them and name the rule that
    broke -- "£0.4m short" is actionable where a disabled button is not.
    """
    card = st.container(border=True)
    head, action = card.columns([3, 1])

    fdr = candidate.next_fdr or 3
    colour = charts.FDR_COLOURS[max(1, min(5, fdr))]
    head.markdown(
        f"**{candidate.name}** &nbsp;<span style='background:{colour};"
        f"color:#fff;padding:1px 5px;border-radius:3px;font-size:11px'>"
        f"{candidate.next_opponent or '—'}</span><br>"
        f"<span style='font-size:12px;color:#8b949e'>"
        f"{candidate.position} · {candidate.team} · £{candidate.cost:.1f}m · "
        f"xP {candidate.xp:.1f} · form {candidate.form:.1f} · "
        f"{candidate.ownership:.1f}% owned</span>",
        unsafe_allow_html=True)

    disabled = selected is None
    if action.button("+ Swap in", key=f"swap_{candidate.player_id}",
                     disabled=disabled, width="stretch"):
        team_ids = _squad_team_ids(conn, state)
        outcome = sandbox.transfer_in(state, candidate, team_ids)
        if outcome.ok:
            st.session_state["sandbox_state"] = outcome.state
            st.session_state.pop("sandbox_mc", None)
            st.rerun()
        else:
            card.error(outcome.reason)


def _squad_team_ids(conn, state) -> dict[int, int]:
    """`{player_id: team_id}` for the three-per-club check.

    Read fresh rather than cached: it is one indexed scan of ~600 rows, and a
    stale club map would let a fourth Arsenal player through the one rule
    people actually hit.
    """
    return {int(r[0]): int(r[1] or -1) for r in conn.execute(
        "SELECT id, team_id FROM players")}

cfg, conn, quality = boot_full()
st.title("\U0001F465 Squad & News")

blocking = quality.blocking_reason()
if blocking:
    empty_state("No data yet", blocking, icon="\U0001F6A6")
    st.stop()

squad_row = conn.execute("SELECT MAX(gw) FROM my_picks").fetchone()
squad_gw = int(squad_row[0]) if squad_row and squad_row[0] else None

quality_bar(quality)

# --- availability banner ---------------------------------------------------
if squad_gw is not None:
    with error_boundary("Availability alerts", quality=quality):
        alerts = minutes_mod.availability_alerts(conn, squad_gw)
        critical = [a for a in alerts if a["severity"] == "critical"]
        high = [a for a in alerts if a["severity"] == "high"]
        doubts = [a for a in alerts if a["severity"] == "doubt"]

        # Banner precedence follows what a manager must act on before the
        # deadline: someone who cannot play, then a coin flip, then a 75% flag.
        # Starters are named ahead of bench players inside each band.
        if critical:
            st.error(
                "🔴 **Pre-deadline alert — will not play** &nbsp; "
                + " · ".join(
                    f"**{a['player']}**"
                    + (" (C)" if a["is_captain"] else
                       (" · XI" if a["starting"] else " · bench"))
                    for a in critical))
        if high:
            st.warning(
                "🟠 **Coin flip (50% or worse)** &nbsp; "
                + " · ".join(f"**{a['player']}** {a['availability']:.0%}"
                             for a in high))
        if doubts and not critical:
            st.info(
                "🟡 **Flagged doubts (75%)** &nbsp; "
                + " · ".join(f"{a['player']} {a['availability']:.0%}"
                             for a in doubts[:6]))

pitch_tab, shots_tab, radar_tab, news_tab = st.tabs(
    ["\U0001F3DF Pitch & sandbox", "\U0001F3AF Shot maps",
     "\U0001F578 Rival radar", "\U0001F4F0 News"])

# --- tactical pitch & transfer sandbox -------------------------------------
# Two columns rather than stacked panels: choosing a transfer is a comparison
# between a squad and a candidate, and a comparison that needs a scroll to see
# both halves is not one. The pitch keeps the wider column because it holds
# fifteen nodes; the roster is a list and reads fine narrow.
with pitch_tab, error_boundary("Pitch & sandbox", quality=quality):
    if squad_gw is None:
        empty_state("No squad loaded",
                    "Set `FPL_TEAM_ID` in `.env`, then run **My squad** "
                    "on Refresh Config.")
    elif not charts.available():
        st.info("Install `plotly` to render the pitch: `pip install plotly`")
    else:
        state = st.session_state.get("sandbox_state")
        if state is None or st.session_state.get("sandbox_gw") != squad_gw:
            state = sandbox.open_sandbox(conn, squad_gw)
            st.session_state["sandbox_state"] = state
            st.session_state["sandbox_gw"] = squad_gw
            st.session_state.pop("sandbox_click", None)

        left, right = st.columns([1.8, 1.2], gap="large")

        # ==================================================================
        # LEFT - pitch and scenario controls
        # ==================================================================
        with left:
            controls = st.columns([1.1, 1.3, 1.0])
            mode = ("\U0001F9EA **Sandbox active**" if state.dirty
                    else "Current squad")
            controls[0].markdown(f"**Mode**<br>{mode}", unsafe_allow_html=True)

            chip_choice = controls[1].selectbox(
                "Active chip",
                [None, *sandbox.CHIPS],
                index=([None, *sandbox.CHIPS]).index(state.chip),
                format_func=lambda c: sandbox.CHIP_LABELS[c],
                help="Chips change the arithmetic, never the squad rules: a "
                     "Free Hit XI still has to be legal and inside budget.")
            if chip_choice != state.chip:
                state = sandbox.set_chip(state, chip_choice)
                st.session_state["sandbox_state"] = state

            entry = controls[2].columns(2)
            free_transfers = entry[0].number_input(
                "Free transfers", min_value=0, max_value=5,
                value=state.free_transfers, step=1,
                help="Banked FTs, up to the 5 the modern rules allow. Drives "
                     "how many transfers are free before a -4 applies.")
            # Bank is DERIVED (budget minus squad sell value) because this app
            # does not ingest `/entry/`, which is where the real figure lives.
            # Editable rather than merely approximate: a bank that reads £0.0m
            # when you actually hold £1.5m silently refuses transfers you can
            # afford, and the refusal looks like a rule rather than a guess.
            bank = entry[1].number_input(
                "Bank (£m)", min_value=0.0, max_value=50.0,
                value=float(state.bank), step=0.1, format="%.1f",
                help="Derived from the £100m budget and your squad's sell "
                     "value. Correct it from the FPL site if it disagrees.")
            if (free_transfers != state.free_transfers
                    or abs(bank - state.bank) > 1e-6):
                state = dataclasses.replace(
                    state, free_transfers=int(free_transfers),
                    bank=round(float(bank), 1))
                st.session_state["sandbox_state"] = state

            density = st.radio(
                "Node detail", [pitch_mod.DENSITY_CLEAN,
                                pitch_mod.DENSITY_DETAILED],
                format_func=lambda d: ("Clean" if d == pitch_mod.DENSITY_CLEAN
                                       else "Detailed (+ price, roles)"),
                horizontal=True, label_visibility="collapsed")

            fig = pitch_mod.figure(state.squad, height=620,
                                   selected_id=state.selected_id,
                                   density=density)
            event = st.plotly_chart(
                fig, width="stretch", key="sandbox_pitch",
                on_select="rerun", selection_mode="points",
                config=PITCH_CONFIG)

            # A Plotly click arrives as (curve, point). `node_player_ids`
            # reproduces the figure's own plotting order, so the two cannot
            # drift and start selecting the wrong player.
            clicked = _clicked_player(event, state.squad)
            if clicked is not None and clicked != st.session_state.get(
                    "sandbox_click"):
                st.session_state["sandbox_click"] = clicked
                st.session_state["sandbox_state"] = sandbox.select(
                    state, clicked)
                st.rerun()

            st.caption(
                "Click a player to line them up for a transfer. Gold ring is "
                "the captain, white ring is your current selection. The pill "
                "under each shirt is the next fixture, coloured by difficulty; "
                "red shirts are flagged. GK/1/2/3 on the bench is auto-sub "
                "priority.")

        # ==================================================================
        # RIGHT - roster browser
        # ==================================================================
        with right:
            selected = next((p for p in state.squad
                             if p.player_id == state.selected_id), None)
            if selected is None:
                st.info("Select a player on the pitch to see transfer targets "
                        "that fit your budget.")
            else:
                st.markdown(
                    f"**Transferring out:** {selected.name} "
                    f"({selected.position} · {selected.team}) — sells for "
                    f"**£{state.sell_price(selected.player_id):.1f}m**")
                budget = state.bank + state.sell_price(selected.player_id)
                st.caption(
                    f"Bank £{state.bank:.1f}m + £"
                    f"{state.sell_price(selected.player_id):.1f}m sale = "
                    f"**£{budget:.1f}m** to spend. FPL sells at your purchase "
                    "price plus half the profit, not today's list price.")

            search = st.text_input("Search", placeholder="Name or team",
                                   label_visibility="collapsed")
            filters = st.columns([1.2, 1.0])
            position = filters[0].selectbox(
                "Position", ["ALL", "GKP", "DEF", "MID", "FWD"],
                index=(["ALL", "GKP", "DEF", "MID", "FWD"].index(
                    selected.position) if selected else 0))
            sort = filters[1].selectbox("Sort by", list(sandbox.SORTS))
            max_price = st.slider("Max price (£m)", 3.5, 16.0,
                                  value=16.0, step=0.1)

            pool = st.session_state.get("sandbox_pool")
            if pool is None or st.session_state.get("sandbox_pool_gw") != squad_gw:
                pool = sandbox.candidates(conn, limit=0)
                st.session_state["sandbox_pool"] = pool
                st.session_state["sandbox_pool_gw"] = squad_gw

            owned = {p.player_id for p in state.squad}
            rows = sandbox.filter_candidates(
                [c for c in pool if c.player_id not in owned],
                query=search, position=position, max_price=max_price,
                sort=sort)

            st.caption(f"{len(rows)} players match — showing the top 25.")
            for candidate in rows[:25]:
                _roster_card(candidate, state, selected, conn)

        # ==================================================================
        # IMPACT BAR - the number the whole screen exists to produce
        # ==================================================================
        st.divider()
        metrics = sandbox.impact(state)
        bar = st.columns(6)
        metric_card(bar[0], "Transfers", metrics.transfers,
                    caption=(f"{metrics.free_used} free"
                             if metrics.transfers else "none yet"))
        metric_card(bar[1], "Hit",
                    f"-{metrics.hits} pts" if metrics.hits else "0 pts",
                    caption=(sandbox.CHIP_LABELS[state.chip]
                             if state.chip in sandbox.FREE_TRANSFER_CHIPS
                             else f"{state.free_transfers} FT"))
        metric_card(bar[2], "Bank", f"£{metrics.bank:.1f}m",
                    caption=f"squad £{metrics.squad_value:.1f}m")
        metric_card(bar[3], "Scenario xP", f"{metrics.scenario_xp:.1f}",
                    caption=f"baseline {metrics.baseline_xp:.1f}")
        metric_card(bar[4], "Δ xP", f"{metrics.xp_delta:+.1f}",
                    caption="before the hit")
        # Net EV is the decision. Everything else on this bar is an input to it.
        metric_card(bar[5], "Net EV", f"{metrics.net_ev:+.1f}",
                    caption="Δ xP − hit")

        if metrics.transfers and metrics.net_ev < 0:
            panel_badge(
                f"This scenario loses {abs(metrics.net_ev):.1f} points against "
                f"just rolling the transfer. A -{metrics.hits} hit needs "
                f"{metrics.hits / max(1, metrics.transfers):.0f}+ xP per "
                "transfer to break even.", "warn")

        actions = st.columns([1, 1, 1, 2])
        if actions[0].button("↺ Reset sandbox", width="stretch",
                             disabled=not state.dirty):
            st.session_state.pop("sandbox_state", None)
            st.session_state.pop("sandbox_click", None)
            st.rerun()

        if actions[1].button("⚡ Monte Carlo", width="stretch",
                             help="10,000 runs on the scenario XI"):
            st.session_state["sandbox_mc"] = _run_monte_carlo(conn, state)

        with actions[2].popover("\U0001F4BE Save", width="stretch"):
            name = st.text_input("Scenario name",
                                 value=f"GW{state.gw} {state.formation}")
            if st.button("Save scenario", width="stretch"):
                sandbox.save_scenario(conn, state, name)
                st.success(f"Saved “{name}”.")

        saved = sandbox.list_scenarios(conn, gw=squad_gw, limit=5)
        if saved:
            with actions[3].popover(f"\U0001F4C2 {len(saved)} saved",
                                    width="stretch"):
                for row in saved:
                    line = st.columns([3, 1])
                    line[0].markdown(
                        f"**{row['name']}** · {row['transfers']} transfers · "
                        f"net EV {row['net_ev']:+.1f}")
                    if line[1].button("Load", key=f"load_{row['scenario_id']}"):
                        base = sandbox.open_sandbox(conn, squad_gw)
                        st.session_state["sandbox_state"] = (
                            sandbox.load_scenario(
                                conn, row["scenario_id"], base))
                        st.rerun()

        mc = st.session_state.get("sandbox_mc")
        if mc is not None:
            st.markdown("##### Monte Carlo — 10,000 runs on this scenario")
            sim = st.columns(4)
            metric_card(sim[0], "Mean", f"{mc.mean:.1f}")
            metric_card(sim[1], "Floor (p10)", f"{mc.floor:.1f}")
            metric_card(sim[2], "Ceiling (p90)", f"{mc.ceiling:.1f}")
            metric_card(sim[3], "P(any haul)", f"{mc.p_haul_squad:.0%}")
            for note in mc.notes:
                st.caption(note)

        if state.transfers:
            st.markdown("##### Transfers in this scenario")
            dataframe(pd.DataFrame([
                {"Out": t.out_name, "Sold": f"£{t.sold_for:.1f}m",
                 "In": t.in_name, "Bought": f"£{t.bought_for:.1f}m",
                 "Net spend": f"£{t.net_spend:+.1f}m"}
                for t in state.transfers]))

        st.caption(
            "Nothing here touches your stored squad. The sandbox lives in "
            "this session until you press Save, and even then it is written "
            "to its own scenario tables — `my_picks` stays the record of what "
            "you actually own. Set your real line-up on the FPL site.")

# --- shot maps -------------------------------------------------------------
with shots_tab, error_boundary("Shot maps", quality=quality):
    st.caption(
        "Shot position with xG encoded as marker area, goals starred. "
        "Coordinates come from Understat, which the FPL API does not "
        "provide.")
    if quality.understat_offline:
        panel_badge(
            "Understat is offline - shot coordinates are unavailable and "
            "the xP model is running on FPL baseline stats.", "warn")

    # Only squad and watchlist players are offered: the selector exists to
    # answer "where is my striker shooting from", not to browse 537 names.
    shooters = [dict(r) for r in conn.execute(
        """SELECT p.id, p.web_name, p.understat_id, COUNT(s.shot_id) n
           FROM players p
           JOIN understat_shot s ON s.understat_id = p.understat_id
           WHERE p.understat_id IS NOT NULL
           GROUP BY p.id HAVING n > 0
           ORDER BY p.web_name""")]

    if not shooters:
        st.plotly_chart(charts.shot_map([]), width="stretch")
        st.info(
            "No shot data ingested yet. Run "
            "`python -m fpl_assistant.ingest --understat` — it resolves "
            "entities and stores per-shot coordinates alongside the "
            "per-match rows.")
    else:
        owned = {int(r["player_id"]) for r in conn.execute(
            "SELECT player_id FROM my_picks WHERE gw = ?", (squad_gw,))
        } if squad_gw is not None else set()
        squad_first = [s for s in shooters if int(s["id"]) in owned]
        pool = squad_first or shooters
        labels = {int(s["id"]): f"{s['web_name']} ({s['n']})" for s in pool}
        by_id = {int(s["id"]): s for s in pool}

        left, right = st.columns([2, 1])
        chosen = left.selectbox("Player", list(labels),
                                format_func=lambda i: labels[i])
        seasons = [r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM understat_shot WHERE understat_id = ?"
            " ORDER BY season DESC", (by_id[chosen]["understat_id"],))]
        season = right.selectbox("Season", ["All"] + seasons)

        sql = ("SELECT x, y, xg, result, minute, situation, h_team, a_team, h_a"
               " FROM understat_shot WHERE understat_id = ?")
        params: list = [by_id[chosen]["understat_id"]]
        if season != "All":
            sql += " AND season = ?"
            params.append(season)

        shots = []
        for r in conn.execute(sql, params):
            # The opponent is whichever club the shooter was not playing for.
            opponent = r["a_team"] if (r["h_a"] or "").lower() == "h" else r["h_team"]
            shots.append(charts.Shot(
                x=float(r["x"] or 0), y=float(r["y"] or 0),
                xg=float(r["xg"] or 0), result=r["result"] or "MissedShots",
                minute=r["minute"], situation=r["situation"] or "",
                opponent=opponent or ""))

        # Centred in a narrower column on purpose. The figure holds a true
        # pitch aspect, so in a full-width `layout="wide"` container it is
        # limited by height and shrinks to a small pitch adrift in a lot of
        # empty canvas. Constraining the column instead lets the same height
        # draw a much bigger pitch.
        #
        # The pitch has one correct framing, so the zoom/pan/autoscale cluster
        # only offers ways to break it. The camera icon is worth keeping: a
        # shot map is something people paste into a league chat.
        _, middle, _ = st.columns([1, 4, 1])
        with middle:
            st.plotly_chart(
                charts.shot_map(shots, title=by_id[chosen]["web_name"],
                                height=520),
                width="stretch", config=SHOT_MAP_CONFIG)

        # Own goals are in Understat's shot feed for the player who scored
        # them, at 0.00 xG. They are not attempts at the opponent's goal, so
        # they belong in neither the map nor the finishing metrics: counting
        # one adds a goal against no xG and flatters Goals - xG by a full
        # goal. Excluded here so the tiles and the figure agree.
        shots = [s for s in shots if not s.is_own_goal]
        goals = [s for s in shots if s.is_goal]
        total_xg = sum(s.xg for s in shots)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Shots", len(shots))
        m2.metric("Goals", len(goals))
        m3.metric("xG", f"{total_xg:.2f}")
        m4.metric("Goals − xG", f"{len(goals) - total_xg:+.2f}",
                  help="Positive means finishing above the chance quality. "
                       "Regresses hard; treat a big number as noise, not skill.")
        if not squad_first:
            st.caption("No squad player has stored shots yet — showing every "
                       "resolved player.")

# --- radar -----------------------------------------------------------------
with radar_tab, error_boundary("Rival radar", quality=quality):
    st.caption(
        "Your squad against mini-league rivals on the four underlying "
        "measures that drive points. Axes are normalised to the largest "
        "value on show, because a radar with raw axes at different scales "
        "is a picture of the units rather than of the teams.")

    if squad_gw is None:
        st.info("No squad loaded.")
    else:
        def _totals(player_ids: list[int]) -> dict[str, float]:
            if not player_ids:
                return {}
            marks = ",".join("?" * len(player_ids))
            row = conn.execute(
                f"""SELECT SUM(g.expected_goals) xg,
                               SUM(g.expected_assists) xa,
                               SUM(g.clean_sheets) cs,
                               SUM(g.total_points) pts
                        FROM player_gw g
                        WHERE g.player_id IN ({marks})""", player_ids).fetchone()
            value = conn.execute(
                f"SELECT SUM(now_cost) v FROM players WHERE id IN ({marks})",
                player_ids).fetchone()
            return {
                "npxG": round(float(row["xg"] or 0), 2),
                "xA": round(float(row["xa"] or 0), 2),
                "xCS": float(row["cs"] or 0),
                "Points": float(row["pts"] or 0),
                "Squad value": round(float(value["v"] or 0), 1),
            }

        mine = [int(r["player_id"]) for r in conn.execute(
            "SELECT player_id FROM my_picks WHERE gw = ?", (squad_gw,))]
        series = {"You": _totals(mine)}

        # Rival names live on `league_standing`; the picks table keys on
        # entry_id alone, so the join is left-outer and falls back to the id.
        rivals = [dict(r) for r in conn.execute(
            """SELECT rp.entry_id, MAX(ls.entry_name) AS name
                   FROM league_rival_pick rp
                   LEFT JOIN league_standing ls ON ls.entry_id = rp.entry_id
                   WHERE rp.gw = ?
                   GROUP BY rp.entry_id LIMIT 3""", (squad_gw,))]

        for rival in rivals:
            ids = [int(r["player_id"]) for r in conn.execute(
                """SELECT player_id FROM league_rival_pick
                       WHERE gw = ? AND entry_id = ?""",
                (squad_gw, rival["entry_id"]))]
            label = str(rival["name"] or f"Entry {rival['entry_id']}")
            series[label] = _totals(ids)

        if len(series) == 1:
            st.info(
                "No rival squads frozen for this gameweek. Pick a rival set on "
                "**Leagues & Rivals** - they are captured after each deadline. "
                "Showing your squad alone.")
        st.plotly_chart(charts.radar(series), width="stretch")
        dataframe([{"Squad": k, **v} for k, v in series.items()])

# --- news ------------------------------------------------------------------
with news_tab, error_boundary("News", quality=quality):
    query = st.text_input(
        "Search all news", placeholder="e.g. hamstring, rotation, press")

    if query:
        hits = search.search_text(conn, query, limit=40)
        st.caption(f"{len(hits)} result(s) for '{query}'")
        for hit in hits:
            with st.container(border=True):
                st.markdown(f"**{hit.get('title') or 'Untitled'}**")
                st.caption(f"{hit.get('source', '')} · "
                           f"{(hit.get('published_at') or '')[:16]}")
                st.write((hit.get("text") or "")[:400])
                if hit.get("url"):
                    st.markdown(f"[Read more]({hit['url']})")
    else:
        st.markdown("##### Tier 1 — your squad")
        st.caption("Injury status and press notes for the 15 players you "
                   "actually own. This is the default view because it is "
                   "the question asked every week.")
        if squad_gw is None:
            st.info("No squad loaded.")
        else:
            rows = conn.execute(
                """SELECT p.id, p.web_name, p.status, p.news, p.news_added,
                              p.chance_of_playing_next_round,
                              mp.multiplier, t.short_name AS team
                       FROM my_picks mp
                       JOIN players p ON p.id = mp.player_id
                       LEFT JOIN teams t ON t.id = p.team_id
                       WHERE mp.gw = ? ORDER BY mp.position""",
                (squad_gw,)).fetchall()
            # Sorted by availability, worst first, and keyed on the gate rather
            # than on whether FPL happened to write news text. A 75% flag is a
            # genuine doubt that must be the first thing on the page, and a
            # player can carry one with an empty `news` field.
            flagged = sorted(
                (r for r in rows
                 if (r["news"] or "").strip()
                 or minutes_mod.availability(dict(r)) < 1.0),
                key=lambda r: minutes_mod.availability(dict(r)))
            starters_at_risk = [
                r for r in flagged
                if r["multiplier"] and minutes_mod.availability(dict(r)) <= 0.75]

            if not flagged:
                st.success("No injury or availability news on your squad.")
            elif starters_at_risk:
                st.error(
                    f"**{len(starters_at_risk)} starter(s) at 75% or below** — "
                    + " · ".join(
                        f"{r['web_name']} "
                        f"{minutes_mod.availability(dict(r)):.0%}"
                        for r in starters_at_risk))

            for r in flagged:
                gate = minutes_mod.availability(dict(r))
                returns = minutes_mod.parse_return_date(r["news"])
                starting = bool(r["multiplier"])
                pip = ("\U0001F534" if gate <= 0.25
                       else ("\U0001F7E0" if gate <= 0.75 else "\U0001F7E1"))
                with st.container(border=True):
                    st.markdown(
                        f"{pip} **{r['web_name']}** ({r['team']}) — "
                        f"availability **{gate:.0%}**"
                        + ("  ·  *starting XI*" if starting else "  ·  bench")
                        + (f" · expected back {returns:%d %b}"
                           if returns else ""))
                    st.caption(r["news"])

            st.markdown("##### Tier 2 — transfer targets & watchlist")
            st.caption("News for the players the solver and the "
                       "differential screen are pointing at.")
            targets = conn.execute(
                """SELECT DISTINCT p.web_name, p.news, t.short_name AS team
                       FROM players p LEFT JOIN teams t ON t.id = p.team_id
                       WHERE p.news IS NOT NULL AND p.news != ''
                         AND p.selected_by_percent > 5
                         AND p.id NOT IN (SELECT player_id FROM my_picks
                                          WHERE gw = ?)
                       ORDER BY p.selected_by_percent DESC LIMIT 12""",
                (squad_gw,)).fetchall()
            if targets:
                dataframe([{"Player": r["web_name"], "Team": r["team"],
                            "News": r["news"]} for r in targets])
            else:
                st.info("No news on widely-owned players outside your squad.")
