"""Refresh Config — the control panel: pull fresh data and check every source.

This is the Streamlit entry point. Its filename sets the sidebar label, so renaming
the file is what renames the page.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from fpl_assistant.db import get_meta
from fpl_assistant.freshness import manual_sources, refresh_prompt, stale_sources
from fpl_assistant.ui import boot
from fpl_assistant import news_fetch, pipeline

st.set_page_config(page_title="Refresh Config", page_icon="⚽", layout="wide")

cfg, conn = boot()

st.title("⚽ Refresh Config")
st.caption("Control panel for the FPL Squad Assistant — pull fresh data, then check "
           "that every source behind it is still alive. Data stays on this machine.")

# --- status ---------------------------------------------------------------
feed_count = len(news_fetch.normalize_sources(cfg.sources.get("rss", []) or []))

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Current GW", get_meta(conn, "current_gw", "—"))
col2.metric("FPL updated", (get_meta(conn, "fpl_last_ingest", "never") or "never")[:16])
col3.metric("News updated", (get_meta(conn, "news_last_ingest", "never") or "never")[:16])
col4.metric("News sources", feed_count)
col5.metric("Insights", cfg.insights_provider or "null")

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

# --- news source health ----------------------------------------------------
st.subheader("News sources")
st.caption("Every feed is named in `config/sources.yaml`. Names come from that file "
           "rather than the feed's own title, because publishers ship empty and "
           "garbled titles often enough to leave the source column blank.")

srcs = news_fetch.normalize_sources(cfg.sources.get("rss", []) or [])
ingested = {r["source"]: {"n": r["n"], "latest": r["latest"]} for r in conn.execute(
    """SELECT source, COUNT(*) n, MAX(published_at) latest
       FROM news_articles GROUP BY source""")}

st.dataframe(pd.DataFrame([{
    "Source": s["name"] or "(unnamed — will fall back to the feed title)",
    "Tier": s["tier"] or "—",
    "Articles stored": ingested.get(s["name"] or "", {}).get("n", 0),
    "Newest stored": (ingested.get(s["name"] or "", {}).get("latest") or "—")[:16],
    "URL": s["url"],
} for s in srcs]), use_container_width=True, hide_index=True)

c1, c2 = st.columns(2)
if c1.button("🔍 Test every feed now", use_container_width=True):
    with st.spinner(f"Probing {len(srcs)} feeds…"):
        probed = news_fetch.probe_sources(cfg.sources.get("rss", []) or [])
    healthy = [p for p in probed if p["ok"] and not p["note"]]
    if len(healthy) == len(probed):
        st.success(f"All {len(probed)} feeds returned fresh items.")
    else:
        st.warning(f"{len(probed) - len(healthy)} of {len(probed)} feeds need attention.")
    st.dataframe(pd.DataFrame([{
        "Source": p["name"],
        "Status": "✅ OK" if p["ok"] and not p["note"] else ("⚠️ " + (p["note"] or "check")),
        "Items": p["items"],
        "Newest item": p["newest"] or "—",
        "Age (days)": p["age_days"],
    } for p in probed]), use_container_width=True, hide_index=True)

if c2.button("🧹 Tidy stored news", use_container_width=True,
             help="Repair garbled source names and delete articles from feeds that "
                  "are no longer configured."):
    result = pipeline.tidy_news(cfg)
    st.success(f"Repaired {result['renamed_sources']} source name(s), "
               f"removed {result['removed_articles']} orphaned article(s).")

st.divider()
st.subheader("Pages")
st.markdown(
    "- **My Squad** — ownership, fixtures, availability and rotation badges.\n"
    "- **News Feed** — per-player chatter with optional Claude insight.\n"
    "- **Transfer Market** — most transferred in/out, price watch.\n"
    "- **Template & Differentials** — what the elite own vs low-owned form picks.\n"
    "- **Captaincy** — ranked captain options: expected points per match times the "
    "number of matches, adjusted for head-to-head record and rotation risk.\n"
    "- **Rotation & Congestion** — fixture pile-ups, AFCON, European midweeks.\n"
    "- **Squad Briefing** — one batched AI request for the whole squad.\n"
    "- **Squad Intelligence** — predicted XI, key-player impact, injury knock-on, "
    "comebacks and new signings, learned from gameweek history.\n"
    "- **Role Arbitrage** — players deployed further forward than FPL lists them, "
    "and how long that window stays open.\n"
    "- **Fixture Planner** — blank and double gameweeks (confirmed and projected "
    "from the cup calendar), fixture runs and chip timing."
)

st.info(
    "Tip: run everything at once from a terminal with "
    "`python -m fpl_assistant.ingest --all`."
)

st.divider()

# --- manual config freshness ---------------------------------------------
st.subheader("Manual config freshness")
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
