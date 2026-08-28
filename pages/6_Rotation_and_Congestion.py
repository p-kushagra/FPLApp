import datetime as dt

import pandas as pd
import streamlit as st

from fpl_assistant import congestion
from fpl_assistant.ui import boot

st.set_page_config(page_title="Rotation & Congestion", page_icon="🔄", layout="wide")
cfg, conn = boot()

st.title("🔄 Rotation & Congestion")
st.caption("Fixture pile-ups, international breaks and tournaments — computed locally, "
           "no AI calls.")

# --- upcoming calendar events --------------------------------------------
events = congestion.active_events(cfg, horizon_days=60)
if events:
    st.subheader("Upcoming calendar events (next 60 days)")
    st.dataframe(pd.DataFrame([{
        "Event": e["name"],
        "Type": "Tournament" if e["kind"] == "tournaments" else "Intl break",
        "Start": e["start"].isoformat(),
        "End": e["end"].isoformat(),
        "Impact": e["impact"],
        "Removes player": "Yes" if e["removes_player"] else "No",
        "Status": "In progress" if e["in_progress"] else f"in {e['days_until']}d",
    } for e in events]), use_container_width=True, hide_index=True)
else:
    st.info("No international breaks or tournaments in the next 60 days. "
            "Update `config/calendar.yaml` when new dates are confirmed.")

st.divider()

# --- squad rotation risk --------------------------------------------------
st.subheader("Squad rotation risk")
report = congestion.squad_rotation_report(conn, cfg)
if not report:
    st.info("Load your squad first (set `FPL_TEAM_ID`, then **My squad** on the home page).")
else:
    st.dataframe(pd.DataFrame([{
        "Player": p["web_name"],
        "Team": p["team_short"],
        "Pos": p["position"],
        "Risk": p["risk"]["band"],
        "Score": p["risk"]["score"],
        "Matches/14d": p["risk"]["load"]["matches_in_window"],
        "Min rest (d)": p["risk"]["load"]["min_gap_days"] or "—",
        "Why": "; ".join(p["risk"]["reasons"]) or "no congestion signals",
    } for p in report]), use_container_width=True, hide_index=True)

    high = [p for p in report if p["risk"]["score"] >= 3]
    if high:
        st.subheader("⚠️ Worth checking before the deadline")
        for p in high:
            st.write(f"{p['risk']['band']} **{p['web_name']}** ({p['team_short']}) — "
                     f"{'; '.join(p['risk']['reasons'])}")

st.divider()

# --- team-level load ------------------------------------------------------
with st.expander("Team fixture load (all clubs)"):
    teams = [dict(r) for r in conn.execute(
        "SELECT id, short_name, name FROM teams ORDER BY short_name")]
    rows = []
    for t in teams:
        load = congestion.team_fixture_load(conn, cfg, t["id"])
        comps = congestion.team_competitions(cfg, t["short_name"])
        rows.append({
            "Team": t["short_name"],
            "Matches/14d": load["matches_in_window"],
            "Min rest (d)": load["min_gap_days"] or "—",
            "Congested": "Yes" if load["congested"] else "No",
            "Midweek comps": ", ".join(c["name"] for c in comps
                                       if c["midweek"] and not c["all_clubs"]) or "—",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# --- config health --------------------------------------------------------
unmapped = congestion.unmapped_regions(conn, cfg)
if unmapped:
    with st.expander(f"Unmapped nationality regions ({len(unmapped)}) — optional"):
        st.caption("Add these to `config/regions.yaml` to enable tournament flags "
                   "for these players.")
        st.dataframe(pd.DataFrame([{
            "Region id": u["region"], "Players": u["count"], "Sample": u["sample"],
        } for u in unmapped]), use_container_width=True, hide_index=True)
