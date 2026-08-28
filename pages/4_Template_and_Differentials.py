import pandas as pd
import streamlit as st

from fpl_assistant import analytics
from fpl_assistant.ui import boot

st.set_page_config(page_title="Template & Differentials", page_icon="🧩", layout="wide")
cfg, conn = boot()

st.title("🧩 Template & Differentials")

tab1, tab2 = st.tabs(["Elite template", "Differentials"])

with tab1:
    st.caption(f"What the top {cfg.top_managers_sample} overall managers own and captain. "
               "Refresh **Template** on the home page to update.")
    tmpl = analytics.template(conn, limit=30)
    if tmpl:
        st.dataframe(pd.DataFrame([{
            "Player": r["web_name"], "Pos": r["position"], "Team": r["team_short"],
            "Top own%": round(r["ownership_pct"], 1),
            "Top capt%": round(r["captain_pct"], 1),
            "Overall own%": r["overall_own"],
        } for r in tmpl]), use_container_width=True, hide_index=True)
    else:
        st.info("No template data yet. Refresh **Template** on the home page.")

with tab2:
    st.caption("Low-owned players in form — potential differentials.")
    c1, c2 = st.columns(2)
    max_own = c1.slider("Max ownership %", 1.0, 30.0, 10.0, 0.5)
    min_form = c2.slider("Min form", 1.0, 10.0, 4.0, 0.5)
    diffs = analytics.differentials(conn, max_own=max_own, min_form=min_form, limit=30)
    if diffs:
        st.dataframe(pd.DataFrame([{
            "Player": r["web_name"], "Pos": r["position"], "Team": r["team_short"],
            "£": r["now_cost"], "Own%": r["selected_by_percent"], "Form": r["form"],
            "Next fixtures": r["next_fixtures"],
        } for r in diffs]), use_container_width=True, hide_index=True)
    else:
        st.info("No matches — loosen the filters or refresh **FPL data**.")
