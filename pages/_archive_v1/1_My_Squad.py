import pandas as pd
import streamlit as st

from fpl_assistant import analytics, congestion
from fpl_assistant.ui import boot

st.set_page_config(page_title="My Squad", page_icon="🧤", layout="wide")
cfg, conn = boot()

st.title("🧤 My Squad")

if cfg.fpl_team_id is None:
    st.warning("Set `FPL_TEAM_ID` in `.env`, then run **My squad** on the home page.")
    st.stop()

squad = analytics.squad_overview(conn)
if not squad:
    st.info("No squad loaded yet. Refresh **FPL data** then **My squad** on the home page.")
    st.stop()

rows = []
for p in squad:
    marker = "©" if p.get("is_captain") else ("Ⓥ" if p.get("is_vice") else "")
    rot = congestion.rotation_risk(conn, cfg, p)
    rows.append({
        "": marker,
        "Player": p["web_name"],
        "Pos": p["position"],
        "Team": p["team_short"],
        "£": p["now_cost"],
        "Own%": p["selected_by_percent"],
        "Form": p["form"],
        "PPG": p["points_per_game"],
        "Next fixtures (venue, FDR)": p["next_fixtures"],
        "Availability": p["risk"],
        "Rotation": rot["band"],
    })
    p["rotation"] = rot

df = pd.DataFrame(rows)
st.dataframe(df, width="stretch", hide_index=True)

flagged = [p for p in squad if not p["risk"].startswith("🟢")]
if flagged:
    st.subheader("⚠️ Availability concerns")
    for p in flagged:
        note = f" — {p['news']}" if p.get("news") else ""
        st.write(f"{p['risk']} **{p['web_name']}** ({p['team_short']}){note}")

rotating = [p for p in squad if p["rotation"]["score"] >= 3]
if rotating:
    st.subheader("🔄 Rotation risk")
    for p in rotating:
        st.write(f"{p['rotation']['band']} **{p['web_name']}** ({p['team_short']}) — "
                 f"{'; '.join(p['rotation']['reasons'])}")
    st.caption("Full detail on the **Rotation & Congestion** page.")
