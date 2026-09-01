import pandas as pd
import streamlit as st

from fpl_assistant import squad_intel as si
from fpl_assistant.db import get_meta
from fpl_assistant.ui import boot

st.set_page_config(page_title="Squad Intelligence", page_icon="🧠", layout="wide")
cfg, conn = boot()

st.title("🧠 Squad Intelligence")
st.caption("Signals learned from actual week-on-week line-ups. No AI, no guesswork — "
           "everything below is measured from the gameweek history.")

gws = conn.execute("SELECT COUNT(DISTINCT gw) n FROM player_gw").fetchone()["n"]
if not gws:
    st.warning("No gameweek history yet. Run **History** on the home page, or "
               "`python -m fpl_assistant.ingest --history`.")
    st.stop()

if gws < 4:
    st.info(f"Only **{gws}** gameweek(s) of history so far. Signals are shown with a "
            "confidence rating and will sharpen as the season progresses.")

teams = [dict(r) for r in conn.execute(
    "SELECT id, short_name, name FROM teams ORDER BY name")]
team_labels = {f"{t['name']} ({t['short_name']})": t["id"] for t in teams}

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Predicted XI", "Key players & impact", "Injury impact", "Comebacks", "New signings"])

# ---------------------------------------------------------------- predicted XI
with tab1:
    choice = st.selectbox("Club", list(team_labels.keys()), key="xi_team")
    team_id = team_labels[choice]

    style = si.team_style(conn, cfg, team_id)
    c1, c2, c3 = st.columns(3)
    c1.metric("Observed rotation", style["rotation"]["label"],
              f"{style['rotation']['avg_changes'] or '—'} changes/GW")
    c2.metric("xG per GW", style["xg_per_gw"] or "—")
    c3.metric("xGC per GW", style["xgc_per_gw"] or "—")

    if style.get("manager") or style.get("style"):
        st.markdown(f"**Manager:** {style.get('manager') or '—'}  \n"
                    f"**Style:** {style.get('style') or '—'}  \n"
                    f"**Set-piece coach:** {style.get('set_piece_coach') or '—'}")
        if style.get("notes"):
            st.caption(style["notes"])
    else:
        st.caption("No curated profile for this club — add one in `config/managers.yaml`.")

    xi = si.predicted_xi(conn, team_id)
    if xi:
        st.dataframe(pd.DataFrame([{
            "Player": p["web_name"], "Pos": p["position"],
            "Start prob": p["start"]["probability"],
            "Avg mins": p["start"]["avg_minutes"],
            "Start streak": p["start"]["streak"],
            "Trend": p["start"]["trend"],
            "Confidence": p["start"]["confidence"],
        } for p in xi]), width="stretch", hide_index=True)
    else:
        st.info("Not enough history for this club yet.")

# ------------------------------------------------------- key players & impact
with tab2:
    choice2 = st.selectbox("Club", list(team_labels.keys()), key="imp_team")
    team_id2 = team_labels[choice2]
    st.caption("Dependency = 60% share of team goal involvement + 40% share of "
               "defensive actions, over the gameweeks each player appeared in.")
    kp = si.key_players(conn, team_id2, limit=10)
    if kp:
        st.dataframe(pd.DataFrame([{
            "Player": p["web_name"], "Pos": p["position"],
            "Dependency": p["dependency"],
            "Attack share %": p["impact"]["attack_share"],
            "Creative share %": p["impact"]["creative_share"],
            "Defensive share %": p["impact"]["defensive_share"],
            "Points share %": p["impact"]["points_share"],
            "Confidence": p["impact"]["confidence"],
        } for p in kp]), width="stretch", hide_index=True)
    else:
        st.info("Not enough history yet.")

# ---------------------------------------------------------------- injury impact
with tab3:
    st.caption("If this player is missing, who gains minutes and output — and who "
               "loses out when he returns.")
    players = [dict(r) for r in conn.execute(
        """SELECT p.id, p.web_name, t.short_name AS ts FROM players p
           JOIN teams t ON t.id = p.team_id ORDER BY p.total_points DESC LIMIT 300""")]
    plabels = {f"{p['web_name']} ({p['ts']})": p["id"] for p in players}
    pchoice = st.selectbox("Player", list(plabels.keys()))
    eff = si.absence_effect(conn, plabels[pchoice])

    if eff.get("note"):
        st.info(eff["note"] + f" (started {eff['present_gws']}, missed {eff['absent_gws']})")
    else:
        st.caption(f"Based on {eff['present_gws']} GW(s) started vs "
                   f"{eff['absent_gws']} missed — confidence **{eff['confidence']}**.")
        st.markdown("**Steps up in his absence**")
        st.dataframe(pd.DataFrame(eff["beneficiaries"]),
                     width="stretch", hide_index=True)
        st.markdown("**Loses out when he returns**")
        st.dataframe(pd.DataFrame(eff["displaced"]),
                     width="stretch", hide_index=True)

# -------------------------------------------------------------------- comebacks
with tab4:
    st.caption("Players whose minutes are ramping back up after a run of blanks — "
               "often available before the market reacts.")
    cw = si.comeback_watch(conn)
    if cw:
        st.dataframe(pd.DataFrame(cw), width="stretch", hide_index=True)
    else:
        st.info("No comeback patterns detected yet (needs several gameweeks of history).")

# ---------------------------------------------------------------- new signings
with tab5:
    st.caption("Recent arrivals and how quickly they are being trusted with minutes.")
    ns = si.new_signings(conn)
    if ns:
        st.dataframe(pd.DataFrame(ns), width="stretch", hide_index=True)
    else:
        st.info("No join-date data available.")
