"""Learned squad intelligence.

Everything here is derived from `player_gw` — the per-gameweek record of who
actually started, how long they played and what they produced. Nothing is guessed
and no AI is involved: these are empirical signals that improve as the season runs.

Sparse early-season data is expected. Every function reports the sample size it
used so the UI can show confidence honestly rather than implying false precision.
"""
from __future__ import annotations

import sqlite3

from .config import Config
from .db import current_gw

RECENCY_DECAY = 0.85          # weight applied per gameweek of age
MIN_SAMPLE_FOR_CONFIDENCE = 4


def _confidence(sample: int) -> str:
    if sample >= 8:
        return "high"
    if sample >= MIN_SAMPLE_FOR_CONFIDENCE:
        return "medium"
    if sample > 0:
        return "low"
    return "none"


# ---------------------------------------------------------------------------
# Starter prediction
# ---------------------------------------------------------------------------
def start_probability(conn: sqlite3.Connection, player_id: int,
                      lookback: int = 8) -> dict:
    """Recency-weighted probability that a player starts the next match."""
    gw = current_gw(conn)
    rows = conn.execute(
        """SELECT gw, minutes, starts FROM player_gw
           WHERE player_id = ? AND gw <= ? AND gw > ?
           ORDER BY gw DESC""",
        (player_id, gw, gw - lookback),
    ).fetchall()

    if not rows:
        return {"probability": None, "confidence": "none", "sample": 0,
                "avg_minutes": None, "streak": 0, "trend": "unknown"}

    weighted_starts = 0.0
    weight_total = 0.0
    minutes_total = 0
    for i, r in enumerate(rows):
        w = RECENCY_DECAY ** i
        weighted_starts += w * (1 if r["starts"] else 0)
        weight_total += w
        minutes_total += r["minutes"] or 0

    streak = 0
    for r in rows:
        if r["starts"]:
            streak += 1
        else:
            break

    recent = [r["minutes"] or 0 for r in rows[:3]]
    older = [r["minutes"] or 0 for r in rows[3:6]]
    trend = "steady"
    if recent and older:
        ra, oa = sum(recent) / len(recent), sum(older) / len(older)
        if ra > oa + 15:
            trend = "rising"
        elif ra < oa - 15:
            trend = "falling"

    return {
        "probability": round(weighted_starts / weight_total, 2) if weight_total else None,
        "confidence": _confidence(len(rows)),
        "sample": len(rows),
        "avg_minutes": round(minutes_total / len(rows), 1),
        "streak": streak,
        "trend": trend,
    }


def predicted_xi(conn: sqlite3.Connection, team_id: int, limit: int = 11) -> list[dict]:
    """Most likely starting XI for a club, ranked by learned start probability."""
    players = [dict(r) for r in conn.execute(
        """SELECT id, web_name, position, element_type, status
           FROM players WHERE team_id = ? AND status != 'u'""",
        (team_id,),
    )]
    out = []
    for p in players:
        sp = start_probability(conn, p["id"])
        if sp["probability"] is None:
            continue
        p["start"] = sp
        out.append(p)
    out.sort(key=lambda x: (x["start"]["probability"], x["start"]["avg_minutes"]),
             reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Team rotation behaviour (an empirical manager profile)
# ---------------------------------------------------------------------------
def team_rotation_profile(conn: sqlite3.Connection, team_id: int) -> dict:
    """Measure how much a club changes its starting XI between gameweeks.

    This is the manager's rotation tendency observed from behaviour, rather than a
    hard-coded opinion about the manager.
    """
    rows = conn.execute(
        """SELECT g.gw, g.player_id, g.starts
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE p.team_id = ? AND g.starts = 1
           ORDER BY g.gw""",
        (team_id,),
    ).fetchall()

    by_gw: dict[int, set[int]] = {}
    for r in rows:
        by_gw.setdefault(r["gw"], set()).add(r["player_id"])

    gws = sorted(by_gw)
    changes = []
    for a, b in zip(gws, gws[1:]):
        if len(by_gw[a]) >= 9 and len(by_gw[b]) >= 9:
            changes.append(len(by_gw[b] - by_gw[a]))

    if not changes:
        return {"avg_changes": None, "label": "unknown", "sample": 0,
                "confidence": "none"}

    avg = sum(changes) / len(changes)
    if avg >= 4:
        label = "heavy rotator"
    elif avg >= 2.5:
        label = "moderate rotator"
    else:
        label = "settled XI"
    return {"avg_changes": round(avg, 1), "label": label, "sample": len(changes),
            "confidence": _confidence(len(changes))}


# ---------------------------------------------------------------------------
# Individual impact on attack / defence
# ---------------------------------------------------------------------------
def impact_share(conn: sqlite3.Connection, player_id: int) -> dict:
    """A player's share of their team's attacking and defensive output.

    Shares are computed only over gameweeks the player actually appeared in, so a
    rotated player is not penalised for matches they never played.
    """
    row = conn.execute("SELECT team_id, position FROM players WHERE id = ?",
                       (player_id,)).fetchone()
    if not row:
        return {}
    team_id = row["team_id"]

    appearances = [r["gw"] for r in conn.execute(
        "SELECT gw FROM player_gw WHERE player_id = ? AND minutes > 0", (player_id,))]
    if not appearances:
        return {"sample": 0, "confidence": "none"}

    marks = ",".join("?" * len(appearances))
    team_tot = conn.execute(
        f"""SELECT COALESCE(SUM(g.expected_goals),0) xg,
                   COALESCE(SUM(g.expected_assists),0) xa,
                   COALESCE(SUM(g.expected_goal_involvements),0) xgi,
                   COALESCE(SUM(g.tackles + g.recoveries
                                + g.clearances_blocks_interceptions),0) def_actions,
                   COALESCE(SUM(g.total_points),0) pts
            FROM player_gw g JOIN players p ON p.id = g.player_id
            WHERE p.team_id = ? AND g.gw IN ({marks})""",
        (team_id, *appearances),
    ).fetchone()

    mine = conn.execute(
        f"""SELECT COALESCE(SUM(expected_goals),0) xg,
                   COALESCE(SUM(expected_assists),0) xa,
                   COALESCE(SUM(expected_goal_involvements),0) xgi,
                   COALESCE(SUM(tackles + recoveries
                                + clearances_blocks_interceptions),0) def_actions,
                   COALESCE(SUM(total_points),0) pts,
                   COALESCE(SUM(minutes),0) mins
            FROM player_gw WHERE player_id = ? AND gw IN ({marks})""",
        (player_id, *appearances),
    ).fetchone()

    def share(num, den):
        return round(100.0 * num / den, 1) if den else 0.0

    return {
        "sample": len(appearances),
        "confidence": _confidence(len(appearances)),
        "attack_share": share(mine["xg"], team_tot["xg"]),
        "creative_share": share(mine["xa"], team_tot["xa"]),
        "goal_involvement_share": share(mine["xgi"], team_tot["xgi"]),
        "defensive_share": share(mine["def_actions"], team_tot["def_actions"]),
        "points_share": share(mine["pts"], team_tot["pts"]),
        "minutes": mine["mins"],
    }


def key_players(conn: sqlite3.Connection, team_id: int, limit: int = 5) -> list[dict]:
    """Players a club most depends on, by combined attacking and defensive share."""
    players = [dict(r) for r in conn.execute(
        "SELECT id, web_name, position FROM players WHERE team_id = ?", (team_id,))]
    out = []
    for p in players:
        imp = impact_share(conn, p["id"])
        if not imp or not imp.get("sample"):
            continue
        p["impact"] = imp
        p["dependency"] = round(
            imp["goal_involvement_share"] * 0.6 + imp["defensive_share"] * 0.4, 1)
        out.append(p)
    out.sort(key=lambda x: x["dependency"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Absence effects: who benefits when a key player is missing
# ---------------------------------------------------------------------------
def absence_effect(conn: sqlite3.Connection, player_id: int,
                   limit: int = 8) -> dict:
    """Compare team-mates' output in gameweeks the player missed vs started.

    This answers "if he is injured, who steps up and how does the shape change?"
    """
    row = conn.execute("SELECT team_id, web_name FROM players WHERE id = ?",
                       (player_id,)).fetchone()
    if not row:
        return {}
    team_id = row["team_id"]

    present = [r["gw"] for r in conn.execute(
        "SELECT gw FROM player_gw WHERE player_id = ? AND starts = 1", (player_id,))]
    absent = [r["gw"] for r in conn.execute(
        "SELECT gw FROM player_gw WHERE player_id = ? AND minutes = 0", (player_id,))]

    if not present or not absent:
        return {"player": row["web_name"], "present_gws": len(present),
                "absent_gws": len(absent), "beneficiaries": [],
                "confidence": "none",
                "note": "Needs gameweeks both with and without this player."}

    def avg_stats(gws):
        marks = ",".join("?" * len(gws))
        return {r["player_id"]: dict(r) for r in conn.execute(
            f"""SELECT g.player_id, AVG(g.minutes) mins, AVG(g.total_points) pts,
                       AVG(g.expected_goal_involvements) xgi
                FROM player_gw g JOIN players p ON p.id = g.player_id
                WHERE p.team_id = ? AND g.gw IN ({marks}) AND g.player_id != ?
                GROUP BY g.player_id""",
            (team_id, *gws, player_id))}

    with_p = avg_stats(present)
    without_p = avg_stats(absent)

    names = {r["id"]: r["web_name"] for r in conn.execute(
        "SELECT id, web_name FROM players WHERE team_id = ?", (team_id,))}

    deltas = []
    for pid, wo in without_p.items():
        wi = with_p.get(pid)
        if not wi:
            continue
        deltas.append({
            "player": names.get(pid, str(pid)),
            "minutes_delta": round(wo["mins"] - wi["mins"], 1),
            "points_delta": round(wo["pts"] - wi["pts"], 2),
            "xgi_delta": round((wo["xgi"] or 0) - (wi["xgi"] or 0), 3),
        })
    deltas.sort(key=lambda d: d["minutes_delta"], reverse=True)

    sample = min(len(present), len(absent))
    return {
        "player": row["web_name"],
        "present_gws": len(present),
        "absent_gws": len(absent),
        "confidence": _confidence(sample),
        "beneficiaries": deltas[:limit],
        "displaced": sorted(deltas, key=lambda d: d["minutes_delta"])[:3],
    }


# ---------------------------------------------------------------------------
# Comeback watch
# ---------------------------------------------------------------------------
def comeback_watch(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Players whose minutes are ramping back up after an absence.

    These are the buy-low windows: back in the squad, not yet fully trusted, and
    usually still cheap because the market has not reacted.
    """
    gw = current_gw(conn)
    rows = conn.execute(
        """SELECT p.id, p.web_name, p.position, p.now_cost, p.selected_by_percent,
                  p.status, p.chance_of_playing_next_round, t.short_name AS team_short
           FROM players p JOIN teams t ON t.id = p.team_id
           WHERE p.status IN ('a', 'd')"""
    ).fetchall()

    out = []
    for r in rows:
        hist = conn.execute(
            """SELECT gw, minutes, starts FROM player_gw
               WHERE player_id = ? AND gw <= ? ORDER BY gw DESC LIMIT 6""",
            (r["id"], gw),
        ).fetchall()
        if len(hist) < 3:
            continue
        mins = [h["minutes"] or 0 for h in hist]
        recent, earlier = mins[:2], mins[2:]
        blank_run = sum(1 for m in earlier if m == 0)
        # Was out for a stretch, now getting minutes again.
        if blank_run >= 2 and recent[0] > 0 and recent[0] >= (recent[1] if len(recent) > 1 else 0):
            out.append({
                "player": r["web_name"],
                "team": r["team_short"],
                "position": r["position"],
                "cost": r["now_cost"],
                "ownership": r["selected_by_percent"],
                "last_minutes": recent[0],
                "blank_gws": blank_run,
                "status": r["status"],
                "chance": r["chance_of_playing_next_round"],
            })
    out.sort(key=lambda x: (x["last_minutes"], -x["ownership"]), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Substitute impact
# ---------------------------------------------------------------------------
def sub_impact(conn: sqlite3.Connection, player_id: int) -> dict:
    """How productive a player is off the bench versus starting."""
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN starts = 1 THEN minutes ELSE 0 END) start_mins,
             SUM(CASE WHEN starts = 1 THEN total_points ELSE 0 END) start_pts,
             SUM(CASE WHEN starts = 1 THEN 1 ELSE 0 END) start_apps,
             SUM(CASE WHEN starts = 0 AND minutes > 0 THEN minutes ELSE 0 END) sub_mins,
             SUM(CASE WHEN starts = 0 AND minutes > 0 THEN total_points ELSE 0 END) sub_pts,
             SUM(CASE WHEN starts = 0 AND minutes > 0 THEN 1 ELSE 0 END) sub_apps
           FROM player_gw WHERE player_id = ?""",
        (player_id,),
    ).fetchone()
    if not row or not (row["start_apps"] or row["sub_apps"]):
        return {"confidence": "none", "sample": 0}

    def per90(pts, mins):
        return round(90.0 * pts / mins, 2) if mins else None

    return {
        "start_apps": row["start_apps"] or 0,
        "sub_apps": row["sub_apps"] or 0,
        "start_p90": per90(row["start_pts"], row["start_mins"]),
        "sub_p90": per90(row["sub_pts"], row["sub_mins"]),
        "sample": (row["start_apps"] or 0) + (row["sub_apps"] or 0),
        "confidence": _confidence((row["start_apps"] or 0) + (row["sub_apps"] or 0)),
    }


# ---------------------------------------------------------------------------
# Head to head and new signings
# ---------------------------------------------------------------------------
def head_to_head(conn: sqlite3.Connection, player_id: int,
                 opponent_team: int) -> dict:
    """A player's record against a specific opponent, from stored gameweek history."""
    rows = conn.execute(
        """SELECT gw, minutes, total_points, goals_scored, assists, was_home
           FROM player_gw
           WHERE player_id = ? AND opponent_team = ? AND minutes > 0
           ORDER BY gw DESC""",
        (player_id, opponent_team),
    ).fetchall()
    if not rows:
        return {"sample": 0, "confidence": "none"}
    pts = [r["total_points"] for r in rows]
    return {
        "sample": len(rows),
        "confidence": _confidence(len(rows)),
        "avg_points": round(sum(pts) / len(pts), 2),
        "goals": sum(r["goals_scored"] for r in rows),
        "assists": sum(r["assists"] for r in rows),
        "meetings": [{"gw": r["gw"], "points": r["total_points"],
                      "home": bool(r["was_home"])} for r in rows[:5]],
    }


def new_signings(conn: sqlite3.Connection, days: int = 120,
                 limit: int = 25) -> list[dict]:
    """Recent arrivals and how quickly they are being integrated."""
    rows = conn.execute(
        """SELECT p.id, p.web_name, p.position, p.now_cost, p.selected_by_percent,
                  p.team_join_date, t.short_name AS team_short
           FROM players p JOIN teams t ON t.id = p.team_id
           WHERE p.team_join_date IS NOT NULL
           ORDER BY p.team_join_date DESC LIMIT ?""",
        (limit * 3,),
    ).fetchall()

    out = []
    for r in rows:
        sp = start_probability(conn, r["id"])
        out.append({
            "player": r["web_name"],
            "team": r["team_short"],
            "position": r["position"],
            "cost": r["now_cost"],
            "ownership": r["selected_by_percent"],
            "joined": r["team_join_date"],
            "start_prob": sp["probability"],
            "avg_minutes": sp["avg_minutes"],
            "trend": sp["trend"],
        })
    return out[:limit]


def team_style(conn: sqlite3.Connection, cfg: Config, team_id: int) -> dict:
    """Strength ratings plus observed output, with any curated notes from config."""
    t = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not t:
        return {}
    agg = conn.execute(
        """SELECT COALESCE(SUM(g.expected_goals),0) xg,
                  COALESCE(SUM(g.expected_goals_conceded),0) xgc,
                  COUNT(DISTINCT g.gw) gws
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE p.team_id = ?""",
        (team_id,),
    ).fetchone()

    profiles = (cfg.managers or {}).get("teams", {}) or {}
    curated = profiles.get(t["short_name"], {})

    gws = agg["gws"] or 0
    return {
        "team": t["short_name"],
        "name": t["name"],
        "strength_attack_home": t["strength_attack_home"],
        "strength_attack_away": t["strength_attack_away"],
        "strength_defence_home": t["strength_defence_home"],
        "strength_defence_away": t["strength_defence_away"],
        "xg_per_gw": round(agg["xg"] / gws, 2) if gws else None,
        "xgc_per_gw": round(agg["xgc"] / gws, 2) if gws else None,
        "gameweeks": gws,
        "manager": curated.get("manager"),
        "style": curated.get("style"),
        "set_piece_coach": curated.get("set_piece_coach"),
        "notes": curated.get("notes"),
        "rotation": team_rotation_profile(conn, team_id),
    }
