"""Decision analytics derived from stored FPL data."""
from __future__ import annotations

import sqlite3

from .db import current_gw


def _next_fixtures(conn: sqlite3.Connection, team_id: int, gw: int, count: int = 3) -> list[dict]:
    rows = conn.execute(
        """SELECT event, team_h, team_a, team_h_difficulty, team_a_difficulty
           FROM fixtures
           WHERE finished = 0 AND event IS NOT NULL AND event >= ?
             AND (team_h = ? OR team_a = ?)
           ORDER BY event
           LIMIT ?""",
        (gw, team_id, team_id, count),
    ).fetchall()
    out = []
    for r in rows:
        if r["team_h"] == team_id:
            out.append({"opp": r["team_a"], "home": True, "fdr": r["team_h_difficulty"]})
        else:
            out.append({"opp": r["team_h"], "home": False, "fdr": r["team_a_difficulty"]})
    return out


def _team_shorts(conn: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["short_name"] for r in conn.execute("SELECT id, short_name FROM teams")}


def format_fixtures(conn: sqlite3.Connection, team_id: int, gw: int, count: int = 3) -> str:
    shorts = _team_shorts(conn)
    parts = []
    for fx in _next_fixtures(conn, team_id, gw, count):
        venue = "H" if fx["home"] else "A"
        opp = shorts.get(fx["opp"], "?")
        parts.append(f"{opp}({venue},{fx['fdr']})")
    return " ".join(parts) if parts else "—"


def risk_badge(player: dict) -> str:
    status = player.get("status") or "a"
    chance = player.get("chance_of_playing_next_round")
    if status in ("i", "u"):
        return "🔴 Out"
    if status == "s":
        return "🔴 Suspended"
    if status == "d" or (chance is not None and chance < 100):
        if chance is not None and chance <= 25:
            return "🟠 Major doubt"
        return "🟡 Doubt"
    if player.get("news"):
        return "🟡 Watch"
    return "🟢 OK"


def squad_overview(conn: sqlite3.Connection) -> list[dict]:
    gw = current_gw(conn)
    rows = conn.execute(
        """SELECT p.*, t.name AS team_name, t.short_name AS team_short,
                  mp.is_captain, mp.is_vice, mp.multiplier
           FROM my_picks mp
           JOIN players p ON p.id = mp.player_id
           JOIN teams t ON t.id = p.team_id
           WHERE mp.gw = ?
           ORDER BY p.element_type, p.now_cost DESC""",
        (gw,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["next_fixtures"] = format_fixtures(conn, d["team_id"], gw)
        d["risk"] = risk_badge(d)
        out.append(d)
    return out


def differentials(conn: sqlite3.Connection, max_own: float = 10.0,
                  min_form: float = 4.0, limit: int = 25) -> list[dict]:
    gw = current_gw(conn)
    rows = conn.execute(
        """SELECT p.*, t.short_name AS team_short
           FROM players p JOIN teams t ON t.id = p.team_id
           WHERE p.selected_by_percent <= ? AND p.form >= ? AND p.status = 'a'
           ORDER BY p.form DESC, p.selected_by_percent ASC
           LIMIT ?""",
        (max_own, min_form, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["next_fixtures"] = format_fixtures(conn, d["team_id"], gw)
        out.append(d)
    return out


def template(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    gw = current_gw(conn)
    rows = conn.execute(
        """SELECT o.ownership_pct, o.captain_pct, o.sample_size,
                  p.web_name, p.position, p.selected_by_percent AS overall_own,
                  t.short_name AS team_short
           FROM top_owned o
           JOIN players p ON p.id = o.player_id
           JOIN teams t ON t.id = p.team_id
           WHERE o.gw = ?
           ORDER BY o.ownership_pct DESC
           LIMIT ?""",
        (gw, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def captaincy(conn: sqlite3.Connection, limit: int = 15, cfg=None) -> list[dict]:
    gw = current_gw(conn)
    shorts = _team_shorts(conn)
    rows = conn.execute(
        """SELECT p.*, t.short_name AS team_short
           FROM players p JOIN teams t ON t.id = p.team_id
           WHERE p.status = 'a' AND p.form > 0
        """
    ).fetchall()
    scored = []
    for r in rows:
        d = dict(r)
        fixtures = _next_fixtures(conn, d["team_id"], gw, 1)
        fdr = fixtures[0]["fdr"] if fixtures else 3
        home = fixtures[0]["home"] if fixtures else False
        opp = shorts.get(fixtures[0]["opp"], "?") if fixtures else "—"
        score = d["form"] * 2 + (6 - (fdr or 3)) + (0.5 if home else 0.0) + d["points_per_game"]

        rot_band = "—"
        if cfg is not None:
            from . import congestion
            rot = congestion.rotation_risk(conn, cfg, d)
            # Rotation risk directly reduces the chance of a captain returning points.
            score -= rot["score"] * 0.8
            rot_band = rot["band"]

        d.update({
            "cap_score": round(score, 2),
            "fdr": fdr,
            "opponent": f"{opp} ({'H' if home else 'A'})",
            "rotation": rot_band,
        })
        scored.append(d)
    scored.sort(key=lambda x: x["cap_score"], reverse=True)
    return scored[:limit]


def price_watch(conn: sqlite3.Connection, rising: bool = True, limit: int = 20) -> list[dict]:
    order = "DESC" if rising else "ASC"
    rows = conn.execute(
        f"""SELECT p.web_name, t.short_name AS team_short, p.now_cost,
                   p.transfers_in_event, p.transfers_out_event,
                   (p.transfers_in_event - p.transfers_out_event) AS net,
                   p.selected_by_percent, p.price_change_percent
            FROM players p JOIN teams t ON t.id = p.team_id
            ORDER BY net {order}
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
