import pandas as pd
import streamlit as st

from fpl_assistant import search
from fpl_assistant.insights import (cache_stats, get_provider, import_exports,
                                    latest_insight, save_insight, summarise_cached)
from fpl_assistant.ui import boot

st.set_page_config(page_title="News Feed", page_icon="📰", layout="wide")
cfg, conn = boot()

st.title("📰 News Feed")
st.caption(f"Insights provider: **{cfg.insights_provider}** "
           f"({cfg.claude_mode} mode)" if cfg.insights_provider == "claude"
           else f"Insights provider: **{cfg.insights_provider}**")

players = [dict(r) for r in conn.execute(
    """SELECT p.id, p.web_name, t.short_name AS team_short, p.status,
              p.chance_of_playing_next_round, p.news
       FROM players p JOIN teams t ON t.id = p.team_id
       ORDER BY p.web_name"""
)]

mode = st.radio("Search by", ["Player", "Free text"], horizontal=True)

if mode == "Free text":
    query = st.text_input("Search news", placeholder="e.g. hamstring, illness, rotation")
    if query:
        results = search.search_text(conn, query, limit=40)
        st.write(f"{len(results)} results")
        for r in results:
            st.markdown(f"**{r['source']}** · {(r['published_at'] or '')[:10]}  \n"
                        f"{r['text']}  \n[source]({r['url']})")
            st.divider()
    st.stop()

if not players:
    st.info("No players loaded. Refresh **FPL data** on the home page.")
    st.stop()

label_to_id = {f"{p['web_name']} ({p['team_short']})": p["id"] for p in players}
choice = st.selectbox("Player", list(label_to_id.keys()))
player_id = label_to_id[choice]
player = next(p for p in players if p["id"] == player_id)

news = search.search_player_news(conn, player_id, limit=30)

left, right = st.columns([2, 1])

with left:
    st.subheader("Recent chatter")
    if not news:
        st.info("No tagged news yet. Refresh **News** on the home page.")
    for n in news:
        st.markdown(f"**{n['source']}** · {(n['published_at'] or '')[:10]}  \n"
                    f"{n['text']}  \n[source]({n['url']})")
        st.divider()

with right:
    st.subheader("Insight")
    existing = latest_insight(conn, player_id)
    if existing:
        st.markdown(f"**{existing['status']}**")
        st.caption(f"{existing['signal_type']} · confidence {existing['confidence']} "
                   f"· {existing['provider']}")
        st.write(existing["summary"])

    if st.button("Generate insight", width="stretch"):
        provider = get_provider(cfg)
        insight, from_cache = summarise_cached(conn, cfg, provider, player, news)
        save_insight(conn, insight)
        st.success("Loaded from cache (no tokens used)." if from_cache
                   else "Insight generated.")
        st.rerun()

    stats = cache_stats(conn)
    st.caption(f"Cache: {stats['entries']} stored, {stats['hits']} calls avoided.")

    if cfg.insights_provider == "claude" and cfg.claude_mode == "bundle":
        st.caption("Bundle mode: a briefing file is written to the briefings/ folder. "
                   "Run it through Claude, save the JSON to exports/, then import below.")
        if st.button("Import Claude results", width="stretch"):
            count = import_exports(cfg, conn)
            st.success(f"Imported {count} insight(s).")
            st.rerun()
