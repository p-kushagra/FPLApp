"""Positional arbitrage: players deployed further forward than FPL lists them."""
import pandas as pd
import streamlit as st

from fpl_assistant import role_arbitrage as ra
from fpl_assistant.ui import boot

st.set_page_config(page_title="Role Arbitrage", page_icon="🎭", layout="wide")
cfg, conn = boot()

st.title("🎭 Role Arbitrage")
st.caption("Players banking points at a position they no longer really play. "
           "Computed locally from output — no AI calls.")

gws = conn.execute("SELECT COUNT(DISTINCT gw) n FROM player_gw").fetchone()["n"]
if not gws:
    st.warning("No gameweek history yet. Run **History** on the home page.")
    st.stop()

with st.expander("Why this is an edge", expanded=False):
    st.markdown(
        "FPL scores by **listed** position, not by where a player actually plays:\n\n"
        "| | Goal | Clean sheet |\n|---|---|---|\n"
        "| GKP / DEF | **6** | **4** |\n| MID | 5 | 1 |\n| FWD | 4 | 0 |\n\n"
        "So a defender pushed up to wing banks **6 points per goal instead of 5**, "
        "stays eligible for **4-point clean sheets**, and is usually priced as a "
        "defender. The window normally exists only while the first-choice attacker "
        "is injured, so the timing matters as much as the pick.\n\n"
        "**Detection:** attacking output far above positional peers *and* defensive "
        "workload far below. Both conditions are required — a centre-back who scores "
        "a header still clears his own box, so his CBI stays high and he is excluded."
    )

if gws < 4:
    st.info(f"Only **{gws}** gameweek(s) of data. Role reads are directional; a single "
            "match can look like a role change when it was just a good game.")

baselines = ra.position_baselines(conn)
with st.expander("Positional baselines (median per 90)"):
    st.dataframe(pd.DataFrame([{
        "Position": p, "Threat/90": round(b["threat"], 1),
        "xGI/90": round(b["xgi"], 3), "CBI/90": round(b["defensive"], 1),
        "Sample": b["sample"],
    } for p, b in baselines.items()]), width="stretch", hide_index=True)

tab1, tab2, tab3 = st.tabs(["Opportunities", "My squad", "Player check"])

# ------------------------------------------------------------- opportunities
with tab1:
    only_open = st.checkbox("Only windows that are still open or closing", value=True)
    cands = ra.arbitrage_candidates(conn, cfg, limit=25)
    if only_open:
        cands = [c for c in cands if c["window"]["verdict"] != "closed"]

    if not cands:
        st.info("No positional mismatches detected in the current data.")
    else:
        st.dataframe(pd.DataFrame([{
            "Score": c["arbitrage_score"],
            "Player": c["player"],
            "Team": c["team"],
            "Listed": c["position"],
            "£": c["cost"],
            "Own%": c["ownership"],
            "Attack vs peers": f"{c['attack_ratio']}×",
            "Defending vs peers": f"{c['defence_ratio']}×",
            "Extra pts/90": c["premium"]["premium_per90"],
            "Set pieces": ", ".join(filter(None, [
                "corners" if c["on_corners"] else "",
                "FKs" if c["on_freekicks"] else "",
                "pens" if c["on_penalties"] else ""])) or "—",
            "Window": c["window"]["verdict"],
        } for c in cands]), width="stretch", hide_index=True)

        st.subheader("Best cases")
        for c in cands[:5]:
            icon = {"open": "🟢", "closing": "🟠", "closed": "🔴"}[c["window"]["verdict"]]
            st.markdown(
                f"{icon} **{c['player']}** ({c['team']}, listed **{c['position']}**, "
                f"£{c['cost']}, {c['ownership']}% owned)  \n"
                f"Attacking output **{c['attack_ratio']}×** his position's median while "
                f"defending at **{c['defence_ratio']}×** — worth about "
                f"**+{c['premium']['premium_per90']} pts/90** purely from being listed "
                f"{c['position']} rather than {c['premium']['compared_to']}.  \n"
                f"*{c['window']['note']}*"
            )
            st.divider()

# ------------------------------------------------------------------ my squad
with tab2:
    squad = ra.squad_arbitrage(conn)
    if not squad:
        st.info("Load your squad first (set `FPL_TEAM_ID`, then **My squad** on the "
                "home page).")
    else:
        st.caption("Role check for players you already own. `advanced` means they are "
                   "outperforming their listed position; `deeper` means the opposite.")
        st.dataframe(pd.DataFrame([{
            "Player": p["player"], "Team": p["team"], "Listed": p["position"],
            "Role": p["role"],
            "Attack vs peers": f"{p['attack_ratio']}×",
            "Defending vs peers": f"{p['defence_ratio']}×",
            "Extra pts/90": p["premium"]["premium_per90"],
            "Apps": p["sample"],
        } for p in squad]), width="stretch", hide_index=True)

        deeper = [p for p in squad if p["role"] == "deeper"]
        if deeper:
            st.warning("Playing deeper than listed — attacking returns may dry up: "
                       + ", ".join(p["player"] for p in deeper))

# -------------------------------------------------------------- player check
with tab3:
    players = [dict(r) for r in conn.execute(
        """SELECT p.id, p.web_name, p.position, t.short_name ts
           FROM players p JOIN teams t ON t.id = p.team_id
           JOIN player_gw g ON g.player_id = p.id AND g.minutes >= 60
           GROUP BY p.id ORDER BY p.web_name""")]
    if not players:
        st.info("No players with 60+ minutes yet.")
    else:
        labels = {f"{p['web_name']} ({p['ts']}, {p['position']})": p["id"]
                  for p in players}
        choice = st.selectbox("Player", list(labels.keys()))
        prof = ra.role_profile(conn, labels[choice], baselines)

        if not prof.get("sample"):
            st.info("Not enough minutes to judge a role.")
        else:
            prem = ra.points_premium(prof)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Inferred role", prof["role"])
            c2.metric("Attack vs peers", f"{prof['attack_ratio']}×")
            c3.metric("Defending vs peers", f"{prof['defence_ratio']}×")
            c4.metric("Extra pts/90", prem["premium_per90"])

            if prof["role"] == "advanced":
                win = ra.window_risk(conn, prof)
                st.markdown(f"**Window: {win['verdict']}** — {win['note']}")
                if win["returning"]:
                    st.dataframe(pd.DataFrame(win["returning"]),
                                 width="stretch", hide_index=True)
                if win["back_now"]:
                    st.error("Already back and playing: "
                             + ", ".join(p["player"] for p in win["back_now"]))
            else:
                st.caption("No positional mismatch detected for this player.")
