"""Whole-squad briefing: one AI request instead of fifteen."""
import datetime as dt

import streamlit as st

from fpl_assistant import congestion, search
from fpl_assistant.db import current_gw
from fpl_assistant.insights import cache_stats, import_exports
from fpl_assistant.insights.claude_provider import build_squad_briefing
from fpl_assistant.ui import boot

st.set_page_config(page_title="Squad Briefing", page_icon="📋", layout="wide")
cfg, conn = boot()

st.title("📋 Squad Briefing")
st.caption("Bundles your whole squad into a single prompt. One request covers 15 "
           "players, instead of 15 separate requests.")

stats = cache_stats(conn)
c1, c2, c3 = st.columns(3)
c1.metric("Cached answers", stats["entries"])
c2.metric("Cache hits (calls avoided)", stats["hits"])
c3.metric("Provider", cfg.insights_provider)

st.divider()

gw = current_gw(conn)
squad = [dict(r) for r in conn.execute(
    """SELECT p.*, t.short_name AS team_short
       FROM my_picks mp
       JOIN players p ON p.id = mp.player_id
       JOIN teams t ON t.id = p.team_id
       WHERE mp.gw = ?""",
    (gw,),
)]

if not squad:
    st.info("Load your squad first: set `FPL_TEAM_ID` in `.env`, then click "
            "**My squad** on the home page.")
    st.stop()

only_flagged = st.checkbox(
    "Only include players with an availability or rotation flag (recommended)",
    value=True,
    help="Fewer players means a shorter prompt and fewer tokens.")

selected = []
for p in squad:
    rot = congestion.rotation_risk(conn, cfg, p)
    flagged = (p.get("status") != "a"
               or (p.get("chance_of_playing_next_round") or 100) < 100
               or bool(p.get("news"))
               or rot["score"] >= 3)
    if not only_flagged or flagged:
        selected.append((p, search.search_player_news(conn, p["id"], limit=6)))

st.write(f"**{len(selected)}** of {len(squad)} players will be included.")

if not selected:
    st.success("No flagged players — nothing needs AI analysis this week. "
               "That is the cheapest possible outcome.")
    st.stop()

briefing = build_squad_briefing(selected)
approx_tokens = len(briefing) // 4
st.caption(f"Prompt size ≈ {len(briefing):,} characters (~{approx_tokens:,} tokens).")

with st.expander("Preview briefing"):
    st.code(briefing, language="markdown")

col1, col2 = st.columns(2)

if col1.button("💾 Save briefing to file", use_container_width=True):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = cfg.briefings_dir / f"squad-briefing-{stamp}.md"
    path.write_text(briefing, encoding="utf-8")
    st.success(f"Saved to `{path}`")
    st.caption("Open it in Claude, then save the JSON array reply into the "
               "`exports/` folder and click Import.")

if col2.button("📥 Import Claude results", use_container_width=True):
    count = import_exports(cfg, conn)
    st.success(f"Imported {count} insight(s).")

st.divider()
st.markdown(
    "**How this keeps costs down**\n\n"
    "- One prompt for the whole squad, not one per player.\n"
    "- Only flagged players are included by default.\n"
    "- Answers are cached against a hash of the exact news used, so unchanged "
    "news is never re-analysed.\n"
    "- All statistics (ownership, fixtures, congestion) are computed in Python and "
    "never sent to the model for calculation."
)
