"""FPL Squad Assistant — Streamlit dashboard (home / control panel)."""
from __future__ import annotations

import streamlit as st

from fpl_assistant.db import get_meta
from fpl_assistant.ui import boot
from fpl_assistant import pipeline

st.set_page_config(page_title="FPL Squad Assistant", page_icon="⚽", layout="wide")

cfg, conn = boot()

st.title("⚽ FPL Squad Assistant")
st.caption("Local-first FPL decision helper — data stays on this machine.")

# --- status ---------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Current GW", get_meta(conn, "current_gw", "—"))
col2.metric("FPL updated", (get_meta(conn, "fpl_last_ingest", "never") or "never")[:16])
col3.metric("News updated", (get_meta(conn, "news_last_ingest", "never") or "never")[:16])
col4.metric("Insights", cfg.insights_provider)

if cfg.fpl_team_id is None:
    st.warning("No `FPL_TEAM_ID` set in `.env` — the **My Squad** page needs it. "
               "Other pages work without it.")

st.divider()

# --- data refresh ---------------------------------------------------------
st.subheader("Refresh data")
st.write("Pull the latest from the FPL API and news sources. Safe to run repeatedly.")

c1, c2, c3, c4 = st.columns(4)

if c1.button("① FPL data", use_container_width=True):
    with st.spinner("Fetching players, teams, fixtures…"):
        gw = pipeline.ingest_fpl(cfg)
    st.success(f"FPL data updated (GW {gw}).")

if c2.button("② My squad", use_container_width=True, disabled=cfg.fpl_team_id is None):
    with st.spinner("Fetching your picks…"):
        pipeline.ingest_my_team(cfg)
    st.success("Squad updated.")

if c3.button("③ Template (top managers)", use_container_width=True):
    with st.spinner(f"Sampling top {cfg.top_managers_sample} managers…"):
        sample = pipeline.ingest_top_owned(cfg)
    st.success(f"Template updated from {sample} managers.")

if c4.button("④ News", use_container_width=True):
    with st.spinner("Fetching and indexing news…"):
        articles, chunks = pipeline.ingest_news(cfg)
    st.success(f"News updated: {articles} new articles, {chunks} chunks.")

st.divider()
st.subheader("Pages")
st.markdown(
    "- **My Squad** — ownership, fixtures, and risk badges for your 15.\n"
    "- **News Feed** — per-player chatter with optional Claude insight.\n"
    "- **Transfer Market** — most transferred in/out, price watch.\n"
    "- **Template & Differentials** — what the elite own vs low-owned form picks.\n"
    "- **Captaincy** — ranked captain options for the next gameweek."
)

st.info(
    "Tip: run everything at once from a terminal with "
    "`python -m fpl_assistant.ingest --all`."
)
