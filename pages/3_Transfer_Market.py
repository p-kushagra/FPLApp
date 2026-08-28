import pandas as pd
import streamlit as st

from fpl_assistant import analytics
from fpl_assistant.ui import boot

st.set_page_config(page_title="Transfer Market", page_icon="🔁", layout="wide")
cfg, conn = boot()

st.title("🔁 Transfer Market")
st.caption("Net transfers this gameweek drive price changes — watch the extremes.")

rising = analytics.price_watch(conn, rising=True, limit=20)
falling = analytics.price_watch(conn, rising=False, limit=20)

col1, col2 = st.columns(2)

with col1:
    st.subheader("📈 Most transferred IN")
    if rising:
        st.dataframe(pd.DataFrame([{
            "Player": r["web_name"], "Team": r["team_short"], "£": r["now_cost"],
            "Net in": r["net"], "In": r["transfers_in_event"],
            "Out": r["transfers_out_event"], "Own%": r["selected_by_percent"],
        } for r in rising]), use_container_width=True, hide_index=True)
    else:
        st.info("Refresh **FPL data** on the home page.")

with col2:
    st.subheader("📉 Most transferred OUT")
    if falling:
        st.dataframe(pd.DataFrame([{
            "Player": r["web_name"], "Team": r["team_short"], "£": r["now_cost"],
            "Net out": -r["net"], "In": r["transfers_in_event"],
            "Out": r["transfers_out_event"], "Own%": r["selected_by_percent"],
        } for r in falling]), use_container_width=True, hide_index=True)
    else:
        st.info("Refresh **FPL data** on the home page.")
