import pandas as pd
import streamlit as st

from fpl_assistant import planner
from fpl_assistant.ui import boot

st.set_page_config(page_title="Fixture Planner", page_icon="🗓️", layout="wide")
cfg, conn = boot()

st.title("🗓️ Fixture Planner & Chip Strategy")
st.caption("Blank and double gameweeks, fixture runs and chip timing — so squad "
           "changes can be planned weeks ahead rather than the night before a deadline.")

gw = planner.next_gw(conn)
horizon = st.slider("Gameweeks to look ahead", 4, 20, 10)

shape = planner.gameweek_shape(conn, horizon=horizon)
if not shape:
    st.info("No fixtures loaded. Refresh **FPL data** on the Refresh Config page.")
    st.stop()

chips = planner.chip_plan(conn, cfg, horizon=horizon)

# --- headline -------------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Next gameweek", f"GW{gw}")
c2.metric("Confirmed doubles", len(chips["confirmed_doubles"]))
c3.metric("Confirmed blanks", len(chips["confirmed_blanks"]))
c4.metric("Projected blank risks", len(chips["projected"]))

tab_shape, tab_chips, tab_alerts, tab_runs, tab_captain = st.tabs(
    ["Gameweek shape", "Chip strategy", "Squad alerts", "Fixture runs", "Captain"])

# --- gameweek shape -------------------------------------------------------
with tab_shape:
    st.subheader("What each gameweek looks like")
    st.dataframe(pd.DataFrame([{
        "GW": g["gw"],
        "Starts": str(g["start_date"] or "—"),
        "Fixtures": g["fixtures"],
        "Shape": {"normal": "Normal", "double": "🟢 Double",
                  "blank": "🔴 Blank", "mixed": "🟠 Blank + double"}[g["kind"]],
        "Doubling": ", ".join(g["double_teams"]) or "—",
        "Blanking": ", ".join(g["blank_teams"]) or "—",
    } for g in shape]), use_container_width=True, hide_index=True)

    st.subheader("Projected disruption")
    st.caption("The FPL fixture list stays clean until a cup tie is actually "
               "rescheduled. A gameweek sitting on a cup round is a blank waiting to "
               "happen for every club still in that competition — which is the part "
               "worth knowing early.")
    if chips["projected"]:
        st.dataframe(pd.DataFrame([{
            "GW": p["gw"],
            "Starts": str(p["start_date"]),
            "Collides with": p["reason"],
            "Notice": f"{p['weeks_notice']} week(s)",
            "Already blank in the API": "yes" if p["already_blank"] else "not yet",
        } for p in chips["projected"]]), use_container_width=True, hide_index=True)
    else:
        st.success("No cup rounds collide with the next "
                   f"{horizon} gameweeks. Cup dates live in `config/calendar.yaml`.")

    if chips["pending_reschedule"]:
        st.warning(f"{len(chips['pending_reschedule'])} fixture(s) have been "
                   "postponed and stripped of a gameweek. Each one must be replayed, "
                   "and lands as a double gameweek for both clubs.")
        st.dataframe(pd.DataFrame([{
            "Home": f["home"], "Away": f["away"],
        } for f in chips["pending_reschedule"]]), use_container_width=True, hide_index=True)

# --- chips ----------------------------------------------------------------
with tab_chips:
    st.subheader("Chip timing")
    st.caption("Deliberately conservative: with no blanks or doubles confirmed yet, "
               "the honest answer is hold, not a target gameweek invented from noise.")
    for name, rec in chips["plan"].items():
        icon = {"play": "🟢", "consider": "🟡", "hold": "⚪"}.get(rec["action"], "⚪")
        target = f" → **GW{rec['target_gw']}**" if rec["target_gw"] else ""
        with st.container(border=True):
            st.markdown(f"{icon} **{name}** — {rec['action'].upper()}{target}")
            st.caption(f"{rec['reason']}  \nConfidence: {rec['confidence']}")

    coverage = planner.squad_gameweek_coverage(conn, horizon=horizon)
    if coverage:
        st.subheader("How much football your 15 play")
        st.caption("Bench Boost wants the peak of this curve; Free Hit wants the trough.")
        df = pd.DataFrame([{
            "GW": c["gw"], "Squad fixtures": c["total_fixtures"],
            "Playing": c["players_playing"], "Blanking": c["players_blank"],
            "Doubling": c["players_doubling"],
        } for c in coverage])
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.bar_chart(df.set_index("GW")["Squad fixtures"])
    else:
        st.info("Refresh **My squad** on the Refresh Config page to get chip advice "
                "tailored to your 15 players.")

# --- alerts ---------------------------------------------------------------
with tab_alerts:
    st.subheader("Act early on these")
    alerts = planner.squad_alerts(conn, cfg, horizon=horizon)
    if not alerts:
        st.success("Nothing in your squad needs action inside the next "
                   f"{horizon} gameweeks.")
    else:
        icon = {"high": "🔴", "opportunity": "🟢", "medium": "🟠", "watch": "🟡"}
        st.dataframe(pd.DataFrame([{
            "": icon.get(a["severity"], "•"),
            "GW": a["gw"],
            "Player": a["player"],
            "Team": a["team"],
            "Issue": a["kind"],
            "Detail": a["detail"],
        } for a in alerts]), use_container_width=True, hide_index=True)

# --- fixture runs ---------------------------------------------------------
with tab_runs:
    st.subheader("Fixture difficulty by club")
    st.caption("Average FDR over the next six gameweeks. Green runs are where you "
               "want your money; brutal runs are where you plan an exit.")
    teams = [dict(r) for r in conn.execute("SELECT id, short_name FROM teams ORDER BY short_name")]
    runs = [planner.fixture_run(conn, t["id"], horizon=6) for t in teams]
    runs.sort(key=lambda r: (r["avg_fdr"] is None, r["avg_fdr"]))
    st.dataframe(pd.DataFrame([{
        "Team": r["team"],
        "Run": r["label"],
        "Avg FDR": r["avg_fdr"],
        "Fixtures": r["count"],
        "Home": r["home_count"],
        "Next six": r["summary"],
    } for r in runs]), use_container_width=True, hide_index=True)

# --- captaincy ------------------------------------------------------------
with tab_captain:
    target = st.selectbox("Gameweek", [g["gw"] for g in shape], index=0)
    squad_only = st.checkbox("Only my squad", value=cfg.fpl_team_id is not None)
    cands = planner.captain_ranking(conn, cfg, gw=target, limit=20, squad_only=squad_only)
    cands = [c for c in cands if c["matches"] > 0]

    if not cands:
        st.info("No candidates for this gameweek. Refresh **FPL data**, and "
                "**My squad** if you are filtering to your own players.")
    else:
        best = cands[0]
        st.success(f"GW{target} pick: **{best['web_name']}** ({best['team_short']}) "
                   f"vs {best['opponent']} — score {best['cap_score']}"
                   + (f" across {best['matches']} fixtures" if best["matches"] > 1 else ""))
        st.dataframe(pd.DataFrame([{
            "Player": c["web_name"], "Team": c["team_short"], "Pos": c["position"],
            "Fixtures": c["matches"], "Opponent": c["opponent"], "FDR": c["fdr"],
            "Score": c["cap_score"], "Per match": c["per_match"],
            "Mins security": c["security"], "Rotation": c["rotation"],
            "Head-to-head": c["h2h_note"] or "—",
        } for c in cands]), use_container_width=True, hide_index=True)
        st.caption("Score is expected points per match multiplied by the number of "
                   "matches that gameweek, so a double gameweek roughly doubles it and "
                   "a blank scores zero. Head-to-head is measured against the player's "
                   "own points-per-game and capped, so it tilts a close call rather "
                   "than deciding one.")
