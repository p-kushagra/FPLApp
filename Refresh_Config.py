"""Refresh Config — the control panel: pull fresh data and check every source.

This is the Streamlit entry point. Its filename sets the sidebar label, so renaming
the file is what renames the page.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from fpl_assistant import news_fetch, pipeline
from fpl_assistant.db import get_meta
from fpl_assistant.freshness import manual_sources, refresh_prompt, stale_sources
from fpl_assistant.ui import boot

st.set_page_config(page_title="Refresh Config", page_icon="⚽", layout="wide")

cfg, conn = boot()

st.title("⚽ Refresh Config")
st.caption("Control panel for the FPL Squad Assistant — pull fresh data, then check "
           "that every source behind it is still alive. Data stays on this machine.")

# --- status ---------------------------------------------------------------
# `st.metric` renders its value at ~2.25rem and does not wrap. An ISO timestamp
# truncated to 16 characters ("2026-09-01T12:34") overflows that in a
# five-column row and silently clips mid-value, so the one number the operator
# came to read -- when the data was last pulled -- is the one they cannot see.
# Shrinking the value slot and wrapping it is enough; the timestamps are also
# reformatted into something human below.
st.markdown(
    """<style>
    div[data-testid="stMetricValue"] {
        font-size: 1.35rem;
        line-height: 1.25;
        white-space: normal;
        overflow-wrap: anywhere;
    }
    div[data-testid="stMetricLabel"] { font-size: .82rem; }
    </style>""",
    unsafe_allow_html=True,
)


def _stamp(value: str | None) -> str:
    """ISO timestamp -> '01 Sep 14:32'. Falls back to whatever it was given."""
    if not value:
        return "never"
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    return when.strftime("%d %b %H:%M")


feed_count = len(news_fetch.normalize_sources(cfg.sources.get("rss", []) or []))

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Current GW", get_meta(conn, "current_gw", "—"))
col2.metric("FPL updated", _stamp(get_meta(conn, "fpl_last_ingest", None)))
col3.metric("News updated", _stamp(get_meta(conn, "news_last_ingest", None)))
col4.metric("News sources", feed_count)
col5.metric("Insights", cfg.insights_provider or "null")

if cfg.fpl_team_id is None:
    st.warning("No `FPL_TEAM_ID` set in `.env` — the **My Squad** page needs it. "
               "Other pages work without it.")

st.divider()

# --- data refresh ---------------------------------------------------------
st.subheader("Refresh data")
st.write("Pull the latest from the FPL API and news sources. Safe to run repeatedly.")

c1, c2, c3, c4, c5, c6 = st.columns(6)

if c6.button("⑥ Mini-leagues", width="stretch",
             disabled=cfg.fpl_team_id is None,
             help="Reads every mini-league you are in from your own FPL entry, "
                  "then refreshes their standings. Choose rivals on the "
                  "Leagues & Rivals page."):
    from fpl_assistant import leagues as leagues_mod
    from fpl_assistant.jobs import tasks as jobs_tasks

    with st.spinner("Discovering your mini-leagues…"):
        found = leagues_mod.discover(conn, cfg.fpl_team_id)
        standings = (jobs_tasks.ingest_mini_league(conn)
                     if found.get("ok") else {})
    if found.get("ok"):
        st.success(f"Found {found['leagues']} league(s); loaded "
                   f"{standings.get('entries', 0)} standing(s) from "
                   f"{standings.get('leagues', 0)} tracked league(s).")
    else:
        st.error(f"Could not read your leagues: {found.get('reason')}")

if c1.button("① FPL data", width="stretch"):
    with st.spinner("Fetching players, teams, fixtures…"):
        gw = pipeline.ingest_fpl(cfg)
    st.success(f"FPL data updated (GW {gw}).")

if c2.button("② My squad", width="stretch", disabled=cfg.fpl_team_id is None):
    with st.spinner("Fetching your picks…"):
        pipeline.ingest_my_team(cfg)
    st.success("Squad updated.")

if c3.button("③ Template (top managers)", width="stretch"):
    with st.spinner(f"Sampling top {cfg.top_managers_sample} managers…"):
        sample = pipeline.ingest_top_owned(cfg)
    st.success(f"Template updated from {sample} managers.")

if c4.button("④ History (learning)", width="stretch"):
    with st.spinner("Building per-gameweek history…"):
        gws, rows = pipeline.ingest_history(cfg)
    st.success(f"History updated: {gws} gameweek(s), {rows} rows.")

if c5.button("⑤ News", width="stretch"):
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
} for s in srcs]), width="stretch", hide_index=True)

c1, c2 = st.columns(2)
if c1.button("🔍 Test every feed now", width="stretch"):
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
    } for p in probed]), width="stretch", hide_index=True)

if c2.button("🧹 Tidy stored news", width="stretch",
             help="Repair garbled source names and delete articles from feeds that "
                  "are no longer configured."):
    result = pipeline.tidy_news(cfg)
    st.success(f"Repaired {result['renamed_sources']} source name(s), "
               f"removed {result['removed_articles']} orphaned article(s).")

st.divider()
st.subheader("Pages")
st.markdown(
    "- **Gameweek Summary** — where your rank actually came from: the ILEO "
    "swing matrix against named rivals, luck vs process, bench and auto-subs.\n"
    "- **Schedule & Congestion** — FDR heatmap, budget rotation pairs, and "
    "European fixture pile-ups with under-72h rest.\n"
    "- **Command Center** — prescriptive transfer pathways, the Shield/Sword "
    "captaincy matrix, transfer-market momentum and role arbitrage.\n"
    "- **Live Matchday** — provisional bonus, formation-legal auto-sub "
    "simulation and the live ILEO rank threat meter.\n"
    "- **Squad & News** — interactive formation pitch, shot maps, rival radar "
    "and the curated squad news feed.\n"
    "- **Leagues & Rivals** — discovers every mini-league you are in from your "
    "FPL entry and sets the rival field that all ILEO analysis is measured "
    "against."
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
} for s in sources]), width="stretch", hide_index=True)

if stale and st.button("📝 Write config-refresh briefing for Claude",
                       width="stretch"):
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


st.divider()

# --- background scheduler --------------------------------------------------
# The projection freeze is the one job that genuinely cannot be run on demand:
# it has to capture the forecast an hour before the deadline, every week. This
# runs it from inside the Streamlit process, which costs nothing and needs no
# second service -- at the price of only running while the app is open. Every
# scheduled job is catch-up safe, so a missed tick is recoverable.
st.subheader("Background scheduler")

from fpl_assistant import scheduler as scheduler_mod

status = scheduler_mod.status()

if not status.available:
    st.info(f"{status.error}. The app works fully without it; the pre-deadline "
            "freeze then has to be run by hand with "
            "`python -m fpl_assistant.ingest --freeze`.")
else:
    sc1, sc2, sc3 = st.columns([1, 1, 3])
    if status.running:
        sc1.success("Running")
        if sc2.button("Stop", width="stretch"):
            scheduler_mod.shutdown(wait=False)
            st.rerun()
    else:
        sc1.warning("Stopped")
        if sc2.button("Start", type="primary", width="stretch"):
            scheduler_mod.start(cfg.db_path)
            st.rerun()
    sc3.caption(
        "Freezes pre-deadline projections into `pre_gw_projections` at "
        f"deadline minus {60} minutes, and refreshes prices, reference data "
        "and projections on a slower cadence. Only runs while this app is open.")

    if status.jobs:
        st.dataframe(
            [{"Job": j["name"], "Next run": j["next_run"],
              "In (min)": j["minutes_away"]} for j in status.jobs],
            width="stretch", hide_index=True)

    if st.button("Freeze projections now"):
        with st.spinner("Freezing…"):
            run = scheduler_mod.run_now(cfg.db_path, "freeze_projections")
        if run and run.ok:
            st.success(run.detail)
        else:
            st.error(run.detail if run else "failed")

    if status.history:
        with st.expander("Recent scheduler runs"):
            st.dataframe(
                [{"Job": h.name, "At": h.started_at, "Seconds": h.seconds,
                  "OK": "yes" if h.ok else "NO", "Detail": h.detail}
                 for h in status.history[:15]],
                width="stretch", hide_index=True)
