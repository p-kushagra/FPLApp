"""Page 4 - Squad & News.

The squad-shaped page: an interactive tactical pitch, Understat shot maps, a
head-to-head radar against mini-league rivals, and a news feed that defaults to
*your* players rather than to a search box.

The news default is the whole point of the consolidation. v1 opened on an empty
search field, which meant the most common question -- "is anyone in my team
injured?" -- required typing fifteen names. Here it is the landing state.
"""
from __future__ import annotations

import streamlit as st

from fpl_assistant import search
from fpl_assistant.models import arbitrage as arbitrage_mod
from fpl_assistant.models import minutes as minutes_mod
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
    ["\U0001F3DF Tactical pitch", "\U0001F3AF Shot maps",
     "\U0001F578 Rival radar", "\U0001F4F0 News"])

# --- tactical pitch --------------------------------------------------------
with pitch_tab, error_boundary("Tactical pitch", quality=quality):
    if squad_gw is None:
        empty_state("No squad loaded",
                    "Set `FPL_TEAM_ID` in `.env`, then run **My squad** "
                    "on Refresh Config.")
    elif not charts.available():
        st.info("Install `plotly` to render the pitch: `pip install plotly`")
    else:
        ids = [int(r["player_id"]) for r in conn.execute(
            "SELECT player_id FROM my_picks WHERE gw = ?", (squad_gw,))]
        badges = arbitrage_mod.badges_for(conn, ids)
        xp = {int(r["player_id"]): float(r["xp_total"] or 0.0)
              for r in conn.execute(
                  "SELECT player_id, xp_total FROM xp_projection "
                  "WHERE gw = (SELECT MAX(gw) FROM xp_projection)")}

        squad = st.session_state.get("pitch_squad")
        if not squad or st.session_state.get("pitch_gw") != squad_gw:
            squad = pitch_mod.load_squad(conn, squad_gw,
                                         xp_by_player=xp, badges=badges)
            st.session_state["pitch_squad"] = squad
            st.session_state["pitch_gw"] = squad_gw

        starters = [p for p in squad if p.starting]
        cols = st.columns(4)
        metric_card(cols[0], "Formation",
                    pitch_mod.formation_string(starters))
        metric_card(cols[1], "Squad value",
                    f"£{sum(p.cost for p in squad):.1f}m")
        metric_card(cols[2], "XI xP",
                    f"{sum(p.xp for p in starters):.1f}")
        metric_card(cols[3], "Flagged",
                    sum(1 for p in squad if p.flagged))

        st.plotly_chart(pitch_mod.figure(squad), width="stretch")
        st.caption(
            "Gold ring marks the captain. Coloured pill under each shirt is "
            "the next fixture and its difficulty; blue chip is a tactical "
            "badge. Red shirts are flagged for availability.")

        st.markdown("##### Swap a starter and a substitute")
        swap = st.columns([2, 2, 1, 1])
        bench_players = [p for p in squad if not p.starting]
        out_id = swap[0].selectbox(
            "Starter out", [p.player_id for p in starters],
            format_func=lambda i: next(
                f"{p.name} ({p.position})" for p in squad
                if p.player_id == i))
        in_id = swap[1].selectbox(
            "Substitute in", [p.player_id for p in bench_players],
            format_func=lambda i: next(
                f"{p.name} ({p.position})" for p in squad
                if p.player_id == i))

        check = pitch_mod.validate_swap(squad, out_id, in_id)
        if check.ok:
            swap[2].success(check.formation)
        else:
            swap[2].error("illegal")

        if swap[3].button("Apply", disabled=not check.ok,
                          width="stretch"):
            st.session_state["pitch_squad"] = pitch_mod.apply_swap(
                squad, out_id, in_id)
            st.rerun()
        if not check.ok:
            panel_badge(check.reason, "warn")
        if st.button("Reset to stored line-up"):
            st.session_state.pop("pitch_squad", None)
            st.rerun()
        st.caption(
            "Validated against the same formation rules the auto-sub "
            "engine uses, so the pitch cannot propose a team FPL would "
            "reject. Changes are local to this view - set your real "
            "line-up on the FPL site.")

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

    shot_rows = []
    try:
        shot_rows = [dict(r) for r in conn.execute(
            """SELECT * FROM understat_player_match LIMIT 1""")]
    except Exception:
        shot_rows = []

    if not shot_rows:
        st.plotly_chart(charts.shot_map([]), width="stretch")
        st.info(
            "No Understat data has been ingested. When Understat is "
            "reachable, run the Understat jobs and resolve entities; the "
            "map populates automatically.")
    else:
        names = {int(r["id"]): r["web_name"] for r in conn.execute(
            "SELECT id, web_name FROM players WHERE understat_id IS NOT NULL")}
        if names:
            chosen = st.selectbox("Player", list(names),
                                  format_func=lambda i: names[i])
            st.plotly_chart(charts.shot_map([], title=names[chosen]),
                            width="stretch")
        else:
            st.info("No players are resolved to Understat ids yet.")

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
                "No rival squads frozen for this gameweek. Ingest a "
                "mini-league to compare - showing your squad alone.")
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
