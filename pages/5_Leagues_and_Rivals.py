"""Page 5 - Mini-Leagues & Rivals.

The sourcing surface for every ILEO-driven panel in the app. The analytics were
already built -- swing matrix, threat meter, rival radar, captaincy Shield/Sword
-- but no code path ever produced a league id, so they all rendered their empty
state permanently. This page closes that loop: discover the leagues from the
manager id already in `.env`, choose which are worth racing, and name the
rivals.

Rival picks are readable only *after* a deadline locks, so nothing here can
back-fill a gameweek that has already gone by. Choosing a rival set is a
forward-looking act; the daemon captures it from the next deadline onwards.
"""
from __future__ import annotations

import streamlit as st

from fpl_assistant import leagues as leagues_mod
from fpl_assistant.jobs import tasks
from fpl_assistant.ui import boot_full
from fpl_assistant.ui.components import (
    dataframe,
    empty_state,
    error_boundary,
    metric_card,
    panel_badge,
    quality_bar,
)

st.set_page_config(page_title="Leagues & Rivals", page_icon="\U0001F465",
                   layout="wide")

cfg, conn, quality = boot_full()

st.title("\U0001F465 Mini-Leagues & Rivals")
st.caption(
    "Global ownership is the wrong denominator when you are racing named "
    "people. Everything on this page feeds ILEO — the swing matrix, the live "
    "threat meter and the Shield/Sword captaincy call.")

quality_bar(quality)

if cfg.fpl_team_id is None:
    empty_state(
        "No FPL team id",
        "Set `FPL_TEAM_ID` in `.env` and restart. Your leagues are read "
        "straight from your own entry — there is nothing to type in by hand.",
        icon="\U0001F6A6")
    st.stop()

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
with error_boundary("League discovery", quality=quality, fatal=True):
    head, action = st.columns([3, 1])
    head.markdown(
        f"Reading leagues for entry **{cfg.fpl_team_id}** from the FPL API.")

    if action.button("\U0001F504 Discover my leagues", type="primary",
                     width="stretch"):
        with st.spinner("Reading your entry..."):
            found = leagues_mod.discover(conn, cfg.fpl_team_id)
        if found.get("ok"):
            with st.spinner("Fetching standings..."):
                ingested = tasks.ingest_mini_league(conn)
            st.success(
                f"Found {found['leagues']} league(s); "
                f"{found['newly_tracked']} newly tracked. "
                f"Loaded {ingested.get('entries', 0)} standing(s) across "
                f"{ingested.get('leagues', 0)} tracked league(s).")
        else:
            st.error(f"Discovery failed: {found.get('reason')}")

    all_leagues = leagues_mod.all_leagues(conn)

if not all_leagues:
    empty_state(
        "No leagues discovered yet",
        "Press **Discover my leagues** above. This reads `leagues.classic` "
        "from your own FPL entry — every mini-league you are in appears "
        "automatically.",
        icon="\U0001F50D")
    st.stop()

private = [lg for lg in all_leagues if lg["league_type"] == "x"]
tracked = [lg for lg in all_leagues if lg["tracked"]]

c1, c2, c3, c4 = st.columns(4)
metric_card(c1, "Leagues found", len(all_leagues),
            caption=f"{len(private)} private")
metric_card(c2, "Tracked", len(tracked), caption="feeding ILEO")
metric_card(c3, "Rivals selected", len(leagues_mod.rival_ids(conn)),
            caption="across tracked leagues")
frozen = conn.execute(
    "SELECT COUNT(DISTINCT entry_id) n FROM league_rival_pick "
    "WHERE frozen = 1").fetchone()
metric_card(c4, "Rival squads frozen", int(frozen["n"]) if frozen else 0,
            caption="captured after deadlines")

st.divider()

# ---------------------------------------------------------------------------
# Which leagues to track
# ---------------------------------------------------------------------------
with error_boundary("Tracked leagues", quality=quality):
    st.subheader("1. Which leagues are worth racing")
    st.caption(
        "FPL enrols everyone in Overall, a country league, a region league and "
        "a club league. Their ILEO is indistinguishable from global ownership, "
        "so only the private leagues someone actually invited you to are "
        "tracked by default.")

    for lg in all_leagues:
        lid = int(lg["league_id"])
        size = lg["entry_count"]
        kind = "private" if lg["league_type"] == "x" else "general"
        rank = lg["my_rank"]
        detail = " · ".join(filter(None, [
            kind,
            f"you are #{rank:,}" if rank else None,
            f"{size:,} entries loaded" if size else None,
        ]))
        row, toggle = st.columns([5, 1])
        row.markdown(f"**{lg['name']}**  \n<span style='opacity:.7;"
                     f"font-size:.85rem'>{detail}</span>",
                     unsafe_allow_html=True)
        new_value = toggle.toggle("Track", value=bool(lg["tracked"]),
                                  key=f"track_{lid}",
                                  label_visibility="collapsed")
        if new_value != bool(lg["tracked"]):
            leagues_mod.set_tracked(conn, lid, new_value)
            st.rerun()

if not tracked:
    st.info("Track at least one league above to choose rivals.")
    st.stop()

st.divider()

# ---------------------------------------------------------------------------
# Rival selection
# ---------------------------------------------------------------------------
with error_boundary("Rival selection", quality=quality):
    st.subheader("2. Who you are actually racing")

    labels = {int(lg["league_id"]): lg["name"] for lg in tracked}
    league_id = st.selectbox(
        "League", options=list(labels), format_func=lambda i: labels[i])

    table = leagues_mod.standings(conn, league_id)
    if not table:
        empty_state(
            "No standings loaded for this league",
            "Press **Discover my leagues** above, or wait for the daemon's "
            "six-hourly league refresh.")
    else:
        st.caption(
            f"{len(table)} entries loaded. The managers *below* you cannot be "
            "caught up to, so the default set is the top of the table — the "
            "people you can still overtake.")

        current = {int(r["entry_id"]) for r in table if r["is_rival"]}
        options = [int(r["entry_id"]) for r in table
                   if int(r["entry_id"]) != int(cfg.fpl_team_id)]
        names = {
            int(r["entry_id"]):
                f"#{r['rank'] or '?'}  {r['player_name'] or r['entry_id']}"
                f"  ({r['entry_name'] or '—'}, {r['total'] or 0} pts)"
            for r in table
        }

        default = sorted(current) or options[:cfg.default_rival_count]
        chosen = st.multiselect(
            "Rival set", options=options,
            default=[e for e in default if e in options],
            format_func=lambda e: names.get(e, str(e)),
            help="Each rival is one squad fetched per gameweek freeze. "
                 f"Capped at {cfg.max_rivals} to stay inside the request "
                 "budget.")

        if len(chosen) > cfg.max_rivals:
            panel_badge(
                f"{len(chosen)} rivals selected; only the first "
                f"{cfg.max_rivals} will be frozen each gameweek.", "warn")

        b1, b2 = st.columns(2)
        if b1.button("Save rival set", type="primary", width="stretch"):
            saved = leagues_mod.set_rivals(conn, league_id,
                                           chosen[:cfg.max_rivals])
            st.success(f"Saved {saved} rival(s) for **{labels[league_id]}**.")
            st.rerun()

        if b2.button("Auto-select top " + str(cfg.default_rival_count),
                     width="stretch"):
            picked = leagues_mod.auto_select_rivals(
                conn, league_id, cfg.default_rival_count,
                exclude_entry=cfg.fpl_team_id)
            st.success(f"Selected {len(picked)} rival(s) by rank.")
            st.rerun()

        dataframe(
            [{"Rank": r["rank"], "Manager": r["player_name"],
              "Team": r["entry_name"], "GW": r["event_total"],
              "Total": r["total"],
              "Rival": "✅" if r["is_rival"] else ""}
             for r in table],
            columns=["Rank", "Manager", "Team", "GW", "Total", "Rival"],
            height=340)

st.divider()

# ---------------------------------------------------------------------------
# Freeze status
# ---------------------------------------------------------------------------
with error_boundary("Freeze status", quality=quality):
    st.subheader("3. Rival squad capture")
    st.caption(
        "A rival's picks are hidden until the deadline locks, so they are "
        "snapshotted once *after* it passes and then never rewritten. A "
        "gameweek whose deadline has already gone by without a capture cannot "
        "be recovered — which is why the daemon does this on a timer.")

    rows = [dict(r) for r in conn.execute(
        """SELECT gw, COUNT(DISTINCT entry_id) entries, MAX(frozen_at) at
           FROM league_rival_pick WHERE frozen = 1
           GROUP BY gw ORDER BY gw DESC LIMIT 10""")]

    if rows:
        dataframe([{"GW": r["gw"], "Rival squads": r["entries"],
                    "Captured": (r["at"] or "—")[:16]} for r in rows],
                  columns=["GW", "Rival squads", "Captured"])
    else:
        empty_state(
            "No rival squads captured yet",
            "The daemon freezes them within 20 minutes of each deadline. "
            "Run one now with the button below if a deadline has already "
            "passed this gameweek.")

    if st.button("Freeze rival squads now", width="stretch"):
        with st.spinner("Fetching rival squads..."):
            result = tasks.freeze_rivals(conn)
        if result.get("ok"):
            st.success(
                f"GW{result['gw']}: froze {result['frozen']} squad(s), "
                f"skipped {result['skipped']} already captured, "
                f"{result['failed']} failed. "
                f"{result['ileo_rows']} ILEO rows written.")
            st.rerun()
        else:
            st.info(f"Nothing to freeze: {result.get('reason')}.")
