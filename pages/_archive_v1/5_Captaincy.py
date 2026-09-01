import pandas as pd
import streamlit as st

from fpl_assistant import planner
from fpl_assistant.ui import boot

st.set_page_config(page_title="Captaincy", page_icon="©️", layout="wide")
cfg, conn = boot()

st.title("©️ Captaincy Helper")
st.caption("Expected points per match times the number of matches that gameweek, "
           "adjusted for head-to-head record, minutes security and rotation risk. "
           "For chip timing and future gameweeks, see **Fixture Planner**.")

gw = planner.next_gw(conn)
only_squad = st.checkbox("Only my squad", value=cfg.fpl_team_id is not None)

candidates = planner.captain_ranking(conn, cfg, gw=gw, limit=60,
                                     squad_only=only_squad and cfg.fpl_team_id is not None)
playing = [c for c in candidates if c["matches"] > 0]

if not playing:
    st.info("No candidates. Refresh **FPL data** (and **My squad** for the squad filter) "
            "on the Refresh Config page.")
    st.stop()

best = playing[0]
st.success(f"GW{gw} suggested captain: **{best['web_name']}** ({best['team_short']}) "
           f"vs {best['opponent']} — score {best['cap_score']}")

if best["matches"] > 1:
    st.info(f"{best['web_name']} plays {best['matches']} times in GW{gw} — a double "
            "gameweek is the strongest captaincy signal there is.")

st.dataframe(pd.DataFrame([{
    "Player": c["web_name"], "Team": c["team_short"], "Pos": c["position"],
    "Fixtures": c["matches"], "Opponent": c["opponent"], "FDR": c["fdr"],
    "Form": c["form"], "PPG": c["points_per_game"],
    "Mins security": c["security"], "Rotation": c["rotation"],
    "Captain score": c["cap_score"],
    "Head-to-head": c["h2h_note"] or "—",
} for c in playing[:20]]), width="stretch", hide_index=True)

blanking = [c for c in candidates if c["matches"] == 0]
if blanking:
    with st.expander(f"{len(blanking)} candidate(s) have no fixture in GW{gw}"):
        st.dataframe(pd.DataFrame([{
            "Player": c["web_name"], "Team": c["team_short"], "Pos": c["position"],
        } for c in blanking]), width="stretch", hide_index=True)

with st.expander("How this score is built"):
    st.markdown(
        "- **Expected points** (`ep_next`) carries the most weight — it is FPL's own "
        "per-match projection and already folds in fixture and history.\n"
        "- **Form and points-per-game** are shrunk toward a league prior by how many "
        "appearances they rest on. One 17-point haul in one appearance should not "
        "outrank a proven premium, and after shrinkage it does not.\n"
        "- **Head-to-head** compares a player's record against this specific opponent "
        "to their own points-per-game, capped so it tilts a close call rather than "
        "deciding one.\n"
        "- **Minutes security** scales the score down for players who are routinely "
        "substituted before 85 minutes.\n"
        "- **Rotation risk** from the congestion engine is subtracted.\n"
        "- **Fixture count** multiplies the result, so a double gameweek roughly "
        "doubles the score and a blank scores zero."
    )
