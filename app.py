"""FPL Squad Assistant — Streamlit dashboard (home / control panel)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from fpl_assistant.db import get_meta
from fpl_assistant.freshness import manual_sources, refresh_prompt, stale_sources
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

c1, c2, c3, c4, c5 = st.columns(5)

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

if c4.button("④ History (learning)", use_container_width=True):
    with st.spinner("Building per-gameweek history…"):
        gws, rows = pipeline.ingest_history(cfg)
    st.success(f"History updated: {gws} gameweek(s), {rows} rows.")

if c5.button("⑤ News", use_container_width=True):
    with st.spinner("Fetching and indexing news…"):
        articles, chunks, errors = pipeline.ingest_news(cfg)
    st.success(f"News updated: {articles} new articles, {chunks} chunks.")
    if errors:
        with st.expander(f"{len(errors)} source warning(s)"):
            for err in errors:
                st.text(err)

st.divider()
st.subheader("Pages")
st.markdown(
    "- **My Squad** — ownership, fixtures, availability and rotation badges.\n"
    "- **News Feed** — per-player chatter with optional Claude insight.\n"
    "- **Transfer Market** — most transferred in/out, price watch.\n"
    "- **Template & Differentials** — what the elite own vs low-owned form picks.\n"
    "- **Captaincy** — ranked captain options, rotation-adjusted.\n"
    "- **Rotation & Congestion** — fixture pile-ups, AFCON, European midweeks.\n"
    "- **Squad Briefing** — one batched AI request for the whole squad.\n"
    "- **Squad Intelligence** — predicted XI, key-player impact, injury knock-on, "
    "comebacks and new signings, learned from gameweek history."
)

st.info(
    "Tip: run everything at once from a terminal with "
    "`python -m fpl_assistant.ingest --all`."
)

st.divider()

# --- manual config freshness ---------------------------------------------
st.subheader("Source freshness")
st.caption("Some facts have no free machine-readable feed — European qualifiers, "
           "managers, cup dates. They are tracked here so they cannot go stale "
           "unnoticed. Sources and cadence live in `config/references.yaml`.")

sources = manual_sources(cfg)
stale = stale_sources(cfg)

if stale:
    st.warning(f"{len(stale)} config(s) due for review: "
               + ", ".join(s["name"] for s in stale))
else:
    st.success("All manually maintained configs are within their review window.")

st.dataframe(pd.DataFrame([{
    "Config": s["name"],
    "File": s["config_file"],
    "Last verified": s["last_verified"],
    "Age (days)": s["age_days"],
    "Review every": s["review_every_days"],
    "Status": s["status"],
} for s in sources]), use_container_width=True, hide_index=True)

if stale and st.button("📝 Write config-refresh briefing for Claude",
                       use_container_width=True):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = cfg.briefings_dir / f"config-refresh-{stamp}.md"
    path.write_text(refresh_prompt(cfg), encoding="utf-8")
    st.success(f"Saved to `{path}` — run it through Claude, then apply the edits.")

with st.expander("Sources for the stale entries"):
    for s in stale or sources:
        st.markdown(f"**{s['name']}** — `{s['config_file']}`")
        for url in s["sources"]:
            st.markdown(f"- {url}")
        if s["check"]:
            st.caption(s["check"])

st.caption("Automate this: `.\\scripts\\weekly_refresh.ps1 -Register` on Windows, "
           "or `./scripts/weekly_refresh.sh --install-cron` on macOS/Linux.")
