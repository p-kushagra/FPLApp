import pandas as pd
import streamlit as st

from fpl_assistant import analytics
from fpl_assistant.ui import boot

st.set_page_config(page_title="Captaincy", page_icon="©️", layout="wide")
cfg, conn = boot()

st.title("©️ Captaincy Helper")
st.caption("Ranked by form, next-fixture difficulty, home advantage and points-per-game.")

only_squad = st.checkbox("Only my squad", value=cfg.fpl_team_id is not None)

candidates = analytics.captaincy(conn, limit=60)

if only_squad and cfg.fpl_team_id is not None:
    gw = conn.execute("SELECT value FROM meta WHERE key='current_gw'").fetchone()
    squad_ids = {r["player_id"] for r in conn.execute(
        "SELECT player_id FROM my_picks WHERE gw = (SELECT value FROM meta WHERE key='current_gw')"
    )}
    candidates = [c for c in candidates if c["id"] in squad_ids]

if not candidates:
    st.info("No candidates. Refresh **FPL data** (and **My squad** for the squad filter).")
    st.stop()

st.dataframe(pd.DataFrame([{
    "Player": c["web_name"], "Team": c["team_short"], "Pos": c["position"],
    "Opponent": c["opponent"], "FDR": c["fdr"], "Form": c["form"],
    "PPG": c["points_per_game"], "Captain score": c["cap_score"],
} for c in candidates[:20]]), use_container_width=True, hide_index=True)

best = candidates[0]
st.success(f"Suggested captain: **{best['web_name']}** ({best['team_short']}) "
           f"vs {best['opponent']} — score {best['cap_score']}")
