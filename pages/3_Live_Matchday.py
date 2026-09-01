"""Page 3 - Live Matchday Arena.

The only page that is useful *during* a gameweek: provisional bonus, the
auto-substitutions that have not been applied yet, and how much rank each live
performance is costing or winning against the rival field.

Logic lives in `fpl_assistant.live` (ADR-001); this file renders.
"""
from __future__ import annotations

import streamlit as st

from fpl_assistant import live as live_mod
from fpl_assistant.strategy import eo as eo_mod
from fpl_assistant.ui import boot_full
from fpl_assistant.ui.components import (
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
)

st.set_page_config(page_title="Live Matchday", page_icon="\U0001F534",
                   layout="wide")

cfg, conn, quality = boot_full()
st.title("\U0001F534 Live Matchday")

blocking = quality.blocking_reason()
if blocking:
    empty_state("No data yet", blocking, icon="\U0001F6A6")
    st.stop()

# --- gameweek picker -------------------------------------------------------
with error_boundary("Page header", quality=quality, fatal=True):
    played = [int(r["gw"]) for r in conn.execute(
        "SELECT DISTINCT gw FROM player_gw ORDER BY gw DESC")]
    if not played:
        empty_state("No gameweek history",
                    "Run **History (learning)** on the Refresh Config page.")
        st.stop()

    head, picker, refresh = st.columns([3, 1, 1])
    head.caption(
        "Bonus is provisional until a match is verified: the top three by BPS "
        "in each unfinished fixture are shown, and they move -- sometimes in "
        "the 89th minute.")
    gw = picker.selectbox("Gameweek", played, index=0,
                          format_func=lambda g: f"GW{g}")
    fetch_live = refresh.toggle("Fetch live", value=False,
                                help="Poll the FPL live endpoint. Off replays "
                                     "stored results.")

quality_bar(quality)

with st.spinner("Reading the live feed..." if fetch_live else "Loading..."):
    with error_boundary("Live state", quality=quality, fatal=True):
        swings: dict[int, tuple[float, float, int, int]] = {}
        try:
            rival_ids = [int(r["entry_id"]) for r in conn.execute(
                "SELECT DISTINCT entry_id FROM league_rival_pick WHERE gw = ?",
                (gw,))]
            if rival_ids:
                matrix = eo_mod.swing_matrix(conn, gw, rival_ids)
                for row in matrix.rows:
                    swings[int(row.player_id)] = (
                        float(row.my_multiplier), float(row.ileo),
                        len(row.owned_by or []), len(matrix.rival_ids))
        except Exception:
            # ILEO needs a frozen rival set; without one the threat meter is
            # simply absent rather than the page failing.
            swings = {}

        state = live_mod.build(conn, gw, fetch=fetch_live,
                               rival_swings=swings or None)

for note in state.notes:
    panel_badge(note, "warn")

# --- scoreboard ------------------------------------------------------------
with error_boundary("Scoreboard", quality=quality):
    cols = st.columns(5)
    metric_card(cols[0], "Provisional points", state.provisional_points,
                f"{state.settled_points} settled")
    metric_card(cols[1], "After auto-subs", state.points_after_subs,
                f"+{state.points_after_subs - state.provisional_points}"
                if state.subs else "no subs pending")
    metric_card(cols[2], "Fixtures",
                f"{state.fixtures_finished}/{state.fixtures_total}",
                "in progress" if state.in_progress else "complete")
    metric_card(cols[3], "Active chip", state.active_chip or "none")
    # Without a frozen rival set the swing is structurally 0, not measured 0.
    # Showing "+0.0" would read as "level with the field", which is a claim
    # this page cannot make.
    metric_card(cols[4], "Net rank swing",
                f"{state.net_threat:+.1f}" if state.threats else "—",
                "vs rival field" if state.threats
                else "ingest a mini-league")

    if state.vice_activated:
        st.warning("Captain did not play - the vice-captain's armband applies.")

# --- squad -----------------------------------------------------------------
st.divider()
squad_tab, bps_tab, subs_tab, threat_tab = st.tabs(
    ["\U0001F465 My squad", "\U0001F3C5 Provisional bonus",
     "\U0001F504 Auto-subs", "\U0001F4C9 Rank threat"])

with squad_tab, error_boundary("Squad", quality=quality):
    if not state.squad:
        st.info("No squad stored for this gameweek.")
    else:
        picks = {int(r["player_id"]): dict(r) for r in conn.execute(
            """SELECT player_id, multiplier, is_captain, is_vice
                   FROM my_picks WHERE gw = ?""", (gw,))}
        dataframe([{
            "Player": p.name, "Team": p.team, "Pos": p.position,
            "Min": p.minutes,
            "Role": ("C" if picks.get(p.player_id, {}).get("is_captain")
                     else ("V" if picks.get(p.player_id, {}).get("is_vice")
                           else ("XI" if picks.get(p.player_id, {})
                                 .get("multiplier") else "bench"))),
            "x": int(picks.get(p.player_id, {}).get("multiplier") or 0),
            "Pts": p.total_points,
            # Kept as an int, not an int-or-"-" mix: a column holding both
            # crashes Arrow serialisation, and Streamlit then dumps a
            # traceback to the console and silently re-types the column.
            "Prov. bonus": int(p.provisional_bonus or 0),
            "Live": p.live_points,
            "Scored": int(p.live_points
                          * (picks.get(p.player_id, {}).get("multiplier") or 0)),
        } for p in state.squad])
        st.caption(
            "`Scored` applies the multiplier straight from your picks - "
            "3 under Triple Captain, 2 for a normal captain, 0 on the "
            "bench. It is never re-derived from the captain flag.")

with bps_tab, error_boundary("Provisional bonus", quality=quality):
    contenders = sorted(
        (p for p in state.players.values() if p.bps > 0),
        key=lambda p: p.bps, reverse=True)[:30]
    if not contenders:
        st.info("No BPS recorded for this gameweek yet.")
    else:
        owned = {p.player_id for p in state.squad}
        dataframe([{
            "Player": p.name, "Team": p.team, "BPS": p.bps,
            # Both bonus columns stay integer-typed for the same Arrow reason
            # as the squad table above.
            "Provisional": int(p.provisional_bonus or 0),
            "Awarded": int(p.bonus_awarded or 0),
            "Mine": "yes" if p.player_id in owned else "",
            "Status": "final" if p.fixture_finished else "live",
        } for p in contenders])
        st.caption(
            "Ties take the higher award and consume the ranks below, which "
            "is FPL's actual rule: two players tied on top BPS both take 3, "
            "and the next takes 1.")

with subs_tab, error_boundary("Auto-subs", quality=quality):
    if state.active_chip == "bboost":
        st.info("Bench Boost is active - every pick scores, so no "
                "auto-substitution can occur.")
    elif not state.subs:
        st.success("No auto-substitutions pending. Every starter played.")
    else:
        for sub in state.subs:
            st.markdown(
                f"<div style='border-left:3px solid #d4a72c;padding:8px 12px;"
                f"margin:6px 0;background:rgba(212,167,44,0.08);"
                f"border-radius:4px'>"
                f"<b style='color:#b3211f'>OUT</b> {sub.out_name} "
                f"({sub.out_position}) &nbsp;→&nbsp; "
                f"<b style='color:#1a7f37'>IN</b> {sub.in_name} "
                f"({sub.in_position}) &nbsp;&nbsp;"
                f"<b>+{sub.points_gained} pts</b><br>"
                f"<span style='opacity:.7;font-size:.85rem'>{sub.reason}"
                f"</span></div>", unsafe_allow_html=True)
        st.caption(
            "Simulated under FPL's rules: only a starter on zero minutes is "
            "replaced, a goalkeeper only by the bench goalkeeper, and the "
            "replacement must leave a legal formation (1 GKP, 3+ DEF, "
            "2+ MID, 1+ FWD).")

with threat_tab, error_boundary("Rank threat", quality=quality):
    if not state.threats:
        st.info(
            "No rival data. Open **Leagues & Rivals**, discover your "
            "mini-leagues and save a rival set - their squads are frozen "
            "after each deadline and populate this threat meter.")
    else:
        st.caption(
            "Swing is your multiplier minus the rivals' effective ownership; "
            "Net is that swing times the points actually scored. Positive is "
            "rank you are **winning**, negative is rank **bleeding away** -- "
            "and it applies whether or not you own the player.")

        bleeding = [t for t in state.threats if t.verdict == "bleeding"]
        gaining = [t for t in state.threats if t.verdict == "gaining"]

        def _threat_rows(rows, colour):
            for t in rows[:6]:
                st.markdown(
                    f"<div style='padding:3px 12px;font-size:.9rem'>"
                    f"<b>{t.name}</b> "
                    f"<span style='opacity:.65'>{t.team}</span>&nbsp; "
                    f"{t.live_points} pts &nbsp;"
                    f"<b style='color:{colour}'>{t.net_swing:+.1f}</b>"
                    f"</div>", unsafe_allow_html=True)

        hurt_col, gain_col = st.columns(2)
        with hurt_col:
            st.markdown(
                "<div style='border-left:4px solid #b3211f;padding:8px 12px;"
                "background:rgba(179,33,31,0.10);border-radius:4px'>"
                "<b style='color:#b3211f'>\U0001F534 COSTING YOU RANK</b><br>"
                "<span style='opacity:.75;font-size:.83rem'>Rival-owned assets "
                "scoring that you are under-exposed to.</span></div>",
                unsafe_allow_html=True)
            if bleeding:
                _threat_rows(bleeding, "#b3211f")
            else:
                st.caption("Nothing is costing you rank right now.")

        with gain_col:
            st.markdown(
                "<div style='border-left:4px solid #1a7f37;padding:8px 12px;"
                "background:rgba(26,127,55,0.10);border-radius:4px'>"
                "<b style='color:#1a7f37'>\U0001F7E2 WINNING YOU RANK</b><br>"
                "<span style='opacity:.75;font-size:.83rem'>Assets you are "
                "over-exposed to, scoring against the field.</span></div>",
                unsafe_allow_html=True)
            if gaining:
                _threat_rows(gaining, "#1a7f37")
            else:
                st.caption("No differential is paying off yet.")

        st.markdown("##### Full threat table")
        dataframe([{
            "": ("\U0001F534" if t.verdict == "bleeding"
                 else ("\U0001F7E2" if t.verdict == "gaining" else "⚪")),
            "Player": t.name, "Team": t.team, "Live pts": t.live_points,
            "My mult": t.my_multiplier, "Rival EO": t.ileo,
            "Swing": t.swing, "Net rank pts": t.net_swing,
        } for t in state.threats[:25]])
