"""Positional arbitrage: players deployed in a role that does not match their
FPL classification.

The edge exists because FPL scores by *listed* position, not by where a player
actually plays:

    Goal        GKP/DEF 6   MID 5   FWD 4
    Clean sheet GKP/DEF 4   MID 1   FWD 0
    Assist                  3 for everyone

So a defender playing as a winger banks 6 points per goal instead of 5, stays
eligible for 4-point clean sheets, and is usually priced as a defender. That is a
genuine mispricing — and a temporary one, because it normally exists only while
the first-choice attacker is injured.

Detection is inferred from output, since the API never states where a player lined
up. A player deployed further forward than their listing shows two things at once:
attacking output far above their positional peers, AND defensive workload far
below. Requiring both is what separates a converted winger from a centre-back who
merely had a good attacking game.
"""
from __future__ import annotations

import sqlite3
import statistics

from .config import Config
from .db import current_gw

MIN_MINUTES = 60          # a substitute cameo says nothing about role
BASELINE_MIN_SAMPLE = 8
CREDIBLE_RETURNERS = 4    # how many of a club's top-priced attackers count as first choice

# FPL points by listed position.
GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3

# 'u' means unavailable in the sense of loaned out, unregistered or departed.
# Those players are not coming back to reclaim a role; 'i' and 'd' are.
RETURNING_STATUSES = ("i", "d")


def _per90(value, minutes) -> float:
    return (float(value or 0) * 90.0 / minutes) if minutes else 0.0


def _safe_ratio(value: float, baseline: float) -> float:
    return value / baseline if baseline else 0.0


# ---------------------------------------------------------------------------
# Positional baselines
# ---------------------------------------------------------------------------
def position_baselines(conn: sqlite3.Connection) -> dict[str, dict]:
    """Median per-90 attacking and defensive output for each FPL position.

    Defensive workload uses clearances, blocks and interceptions rather than
    tackles. CBI is positional - it happens defending your own box - whereas
    tackles occur all over the pitch, so including them blurs exactly the
    distinction being measured.
    """
    rows = conn.execute(
        """SELECT p.position, g.minutes, g.threat, g.expected_goal_involvements AS xgi,
                  g.clearances_blocks_interceptions AS cbi
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE g.minutes >= ?""",
        (MIN_MINUTES,),
    ).fetchall()

    buckets: dict[str, dict[str, list]] = {}
    for r in rows:
        b = buckets.setdefault(r["position"], {"threat": [], "xgi": [], "def": []})
        b["threat"].append(_per90(r["threat"], r["minutes"]))
        b["xgi"].append(_per90(r["xgi"], r["minutes"]))
        b["def"].append(_per90(r["cbi"], r["minutes"]))

    out = {}
    for pos, b in buckets.items():
        if len(b["threat"]) < BASELINE_MIN_SAMPLE:
            continue
        out[pos] = {
            "threat": statistics.median(b["threat"]),
            "xgi": statistics.median(b["xgi"]),
            "defensive": statistics.median(b["def"]),
            "sample": len(b["threat"]),
        }
    return out


# ---------------------------------------------------------------------------
# Role inference
# ---------------------------------------------------------------------------
def role_profile(conn: sqlite3.Connection, player_id: int,
                 baselines: dict | None = None) -> dict:
    """Compare a player's output to the median for their listed position."""
    baselines = baselines if baselines is not None else position_baselines(conn)

    row = conn.execute(
        """SELECT p.id, p.web_name, p.position, p.now_cost, p.selected_by_percent,
                  p.status, p.team_id, p.corners_order, p.freekicks_order,
                  p.penalties_order, t.short_name AS team_short,
                  SUM(g.minutes) mins, SUM(g.threat) threat,
                  SUM(g.expected_goal_involvements) xgi,
                  SUM(g.expected_goals) xg, SUM(g.expected_assists) xa,
                  SUM(g.clearances_blocks_interceptions) cbi, SUM(g.tackles) tackles,
                  SUM(g.total_points) pts, COUNT(*) apps
           FROM players p
           JOIN teams t ON t.id = p.team_id
           JOIN player_gw g ON g.player_id = p.id
           WHERE p.id = ? AND g.minutes >= ?
           GROUP BY p.id""",
        (player_id, MIN_MINUTES),
    ).fetchone()

    if not row or not row["mins"]:
        return {"player_id": player_id, "sample": 0, "role": "unknown"}

    pos = row["position"]
    base = baselines.get(pos)
    mins = row["mins"]

    threat90 = _per90(row["threat"], mins)
    xgi90 = _per90(row["xgi"], mins)
    def90 = _per90(row["cbi"], mins)

    if not base:
        return {"player_id": player_id, "sample": row["apps"], "role": "unknown"}

    attack_ratio = max(_safe_ratio(threat90, base["threat"]),
                       _safe_ratio(xgi90, base["xgi"]))
    defence_ratio = _safe_ratio(def90, base["defensive"])

    # Advanced deployment = attacking well above peers AND defending well below.
    if attack_ratio >= 2.5 and defence_ratio <= 0.7:
        role = "advanced"
    elif attack_ratio <= 0.5 and defence_ratio >= 1.4:
        role = "deeper"
    else:
        role = "as listed"

    return {
        "player_id": player_id,
        "player": row["web_name"],
        "team": row["team_short"],
        "team_id": row["team_id"],
        "position": pos,
        "cost": row["now_cost"],
        "ownership": row["selected_by_percent"],
        "status": row["status"],
        "minutes": mins,
        "sample": row["apps"],
        "threat_per90": round(threat90, 1),
        "xgi_per90": round(xgi90, 3),
        "defensive_per90": round(def90, 1),
        "attack_ratio": round(attack_ratio, 1),
        "defence_ratio": round(defence_ratio, 2),
        "role": role,
        "on_corners": row["corners_order"] is not None,
        "on_freekicks": row["freekicks_order"] is not None,
        "on_penalties": row["penalties_order"] is not None,
        "points": row["pts"],
    }


# ---------------------------------------------------------------------------
# Value of the mismatch
# ---------------------------------------------------------------------------
def points_premium(profile: dict, clean_sheet_rate: float = 0.30) -> dict:
    """Extra points per 90 earned purely from the position classification.

    Compares what this player banks as their listed position against what the same
    output would score if they were classified where they actually play.
    """
    pos = profile.get("position")
    if profile.get("role") != "advanced" or pos not in GOAL_POINTS:
        return {"premium_per90": 0.0, "compared_to": None}

    # A defender playing as a winger would otherwise be listed MID; a MID playing
    # as a striker would otherwise be FWD.
    equivalent = {"DEF": "MID", "MID": "FWD"}.get(pos)
    if not equivalent:
        return {"premium_per90": 0.0, "compared_to": None}

    xgi90 = profile.get("xgi_per90", 0.0)
    # Split expected involvement between goals and assists at the usual ~60/40.
    xg90, xa90 = xgi90 * 0.6, xgi90 * 0.4

    actual = xg90 * GOAL_POINTS[pos] + xa90 * ASSIST_POINTS \
        + clean_sheet_rate * CLEAN_SHEET_POINTS[pos]
    alternative = xg90 * GOAL_POINTS[equivalent] + xa90 * ASSIST_POINTS \
        + clean_sheet_rate * CLEAN_SHEET_POINTS[equivalent]

    return {
        "premium_per90": round(actual - alternative, 2),
        "compared_to": equivalent,
        "goal_points": GOAL_POINTS[pos],
        "clean_sheet_points": CLEAN_SHEET_POINTS[pos],
    }


# ---------------------------------------------------------------------------
# How long the window stays open
# ---------------------------------------------------------------------------
def window_risk(conn: sqlite3.Connection, profile: dict) -> dict:
    """Team-mates whose return would likely close this arbitrage window.

    The role is usually only available because someone ahead of them is missing.
    Only a club's genuine first-choice attackers count: a fringe player returning
    will not reclaim the role, so ranking by price avoids flagging every squad
    member who happens to be unavailable.
    """
    gw = current_gw(conn)
    attackers = conn.execute(
        """SELECT p.id, p.web_name, p.position, p.status,
                  p.chance_of_playing_next_round AS chance, p.news,
                  p.selected_by_percent, p.now_cost
           FROM players p
           WHERE p.team_id = ? AND p.id != ? AND p.element_type IN (3, 4)
           ORDER BY p.now_cost DESC""",
        (profile["team_id"], profile["player_id"]),
    ).fetchall()

    first_choice = {r["id"] for r in attackers[:CREDIBLE_RETURNERS]}

    returning, back_now = [], []
    for r in attackers:
        if r["id"] not in first_choice:
            continue
        recent = conn.execute(
            """SELECT COALESCE(SUM(minutes), 0) mins FROM player_gw
               WHERE player_id = ? AND gw > ?""",
            (r["id"], gw - 3),
        ).fetchone()["mins"]

        entry = {
            "player": r["web_name"],
            "position": r["position"],
            "status": r["status"],
            "chance": r["chance"],
            "cost": r["now_cost"],
            "news": (r["news"] or "")[:120],
            "recent_minutes": recent,
        }
        out_now = r["status"] in RETURNING_STATUSES or (r["chance"] or 100) < 100
        if out_now and recent == 0:
            returning.append(entry)
        elif not out_now and recent > 0:
            back_now.append(entry)

    if back_now and not returning:
        verdict = "closed"
        note = (f"{back_now[0]['player']} is fit and playing again — "
                "the advanced role has probably gone.")
    elif returning:
        verdict = "closing"
        names = ", ".join(t["player"] for t in returning[:2])
        note = (f"{names} out but expected back. Value lasts only until they return.")
    else:
        verdict = "open"
        note = "No first-choice attacker is missing — the role may be permanent."

    return {"verdict": verdict, "note": note,
            "returning": returning[:5], "back_now": back_now[:3]}


# ---------------------------------------------------------------------------
# The ranked opportunity list
# ---------------------------------------------------------------------------
def arbitrage_candidates(conn: sqlite3.Connection, cfg: Config | None = None,
                         limit: int = 20) -> list[dict]:
    """Players whose deployed role is more advanced than their FPL listing."""
    baselines = position_baselines(conn)
    if not baselines:
        return []

    ids = [r["player_id"] for r in conn.execute(
        """SELECT g.player_id, SUM(g.minutes) m
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE g.minutes >= ? AND p.element_type IN (2, 3) AND p.status = 'a'
           GROUP BY g.player_id HAVING m >= ?""",
        (MIN_MINUTES, MIN_MINUTES),
    )]

    out = []
    for pid in ids:
        prof = role_profile(conn, pid, baselines)
        if prof.get("role") != "advanced":
            continue
        prof["premium"] = points_premium(prof)
        prof["window"] = window_risk(conn, prof)

        # A shut window is not an opportunity, however good the output looked.
        if prof["window"]["verdict"] == "closed":
            continue

        # Cheap, high-output, still-open windows are worth the most.
        score = min(prof["attack_ratio"], 10.0) * 2
        score += prof["premium"]["premium_per90"] * 3
        score += 2.0 if prof["window"]["verdict"] == "open" else 0.0
        score += 1.0 if prof["on_corners"] or prof["on_freekicks"] else 0.0
        score += 2.0 if prof["on_penalties"] else 0.0
        score += max(0.0, (6.0 - prof["cost"]))          # reward cheap enablers
        score -= min(prof["ownership"] / 10.0, 3.0)       # less edge if widely owned
        prof["arbitrage_score"] = round(score, 1)
        out.append(prof)

    out.sort(key=lambda p: p["arbitrage_score"], reverse=True)
    return out[:limit]


def squad_arbitrage(conn: sqlite3.Connection) -> list[dict]:
    """Role check for players already in your squad."""
    gw = current_gw(conn)
    baselines = position_baselines(conn)
    ids = [r["player_id"] for r in conn.execute(
        "SELECT player_id FROM my_picks WHERE gw = ?", (gw,))]
    out = []
    for pid in ids:
        prof = role_profile(conn, pid, baselines)
        if prof.get("sample"):
            prof["premium"] = points_premium(prof)
            out.append(prof)
    return out
