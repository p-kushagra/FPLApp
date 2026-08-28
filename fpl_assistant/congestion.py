"""Fixture congestion, international duty and rotation risk.

Everything here is deterministic Python driven by config/calendar.yaml and
config/regions.yaml. It costs nothing to run and makes NO AI calls — the LLM is
only ever asked to interpret news text, never to compute these numbers.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from .config import Config
from .db import current_gw

_IMPACT_WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}


def _parse_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


# --------------------------------------------------------------------------
# Calendar lookups
# --------------------------------------------------------------------------
def active_events(cfg: Config, on: dt.date | None = None,
                  horizon_days: int = 21) -> list[dict]:
    """Calendar events overlapping the window [on, on + horizon_days]."""
    on = on or dt.date.today()
    window_end = on + dt.timedelta(days=horizon_days)
    cal = cfg.calendar or {}
    found: list[dict] = []

    for kind in ("international_breaks", "tournaments"):
        for ev in cal.get(kind) or []:
            start = _parse_date(ev.get("start"))
            end = _parse_date(ev.get("end"))
            if not start or not end:
                continue
            if start <= window_end and end >= on:
                found.append({
                    "kind": kind,
                    "name": ev.get("name", "Unnamed"),
                    "start": start,
                    "end": end,
                    "impact": ev.get("impact", "medium"),
                    "removes_player": bool(ev.get("removes_player")),
                    "nations": [str(n).lower() for n in (ev.get("nations") or [])],
                    "confederation": ev.get("confederation"),
                    "in_progress": start <= on <= end,
                    "days_until": max(0, (start - on).days),
                    "note": ev.get("note", ""),
                })
    return sorted(found, key=lambda e: e["start"])


def team_competitions(cfg: Config, team_short: str) -> list[dict]:
    """Extra midweek competitions a club is involved in.

    An empty `teams` list without `all_clubs` means the entry is unconfigured and is
    skipped entirely — guessing would flag all 20 clubs for a competition six enter.
    """
    out = []
    for comp in (cfg.calendar or {}).get("club_competitions") or []:
        teams = comp.get("teams") or []
        all_clubs = bool(comp.get("all_clubs"))
        if not (all_clubs or (teams and team_short in teams)):
            continue
        out.append({
            "name": comp.get("name", "Competition"),
            "impact": comp.get("impact", "low"),
            "midweek": bool(comp.get("midweek")),
            "all_clubs": all_clubs,
        })
    return out


def player_tournament_risk(cfg: Config, region_id: int | None,
                           on: dt.date | None = None,
                           horizon_days: int = 45) -> list[dict]:
    """Tournaments that would remove this player from their club squad."""
    if region_id is None:
        return []
    region = (cfg.regions or {}).get(region_id)
    if not region:
        return []
    country = str(region.get("country", "")).lower()
    confed = region.get("confederation")

    risks = []
    for ev in active_events(cfg, on=on, horizon_days=horizon_days):
        if ev["kind"] != "tournaments":
            continue
        nations = ev["nations"]
        # No nation list = tournament applies to everyone in the confederation.
        applies = (country in nations) if nations else (ev["confederation"] in (confed, "FIFA"))
        if applies:
            risks.append(ev)
    return risks


# --------------------------------------------------------------------------
# Fixture congestion (from the FPL fixture list only)
# --------------------------------------------------------------------------
def team_fixture_load(conn: sqlite3.Connection, cfg: Config, team_id: int,
                      window_days: int | None = None) -> dict:
    """Count upcoming league matches and the shortest gap between them."""
    thresholds = (cfg.calendar or {}).get("thresholds") or {}
    window_days = window_days or int(thresholds.get("congested_window_days", 14))
    short_rest = int(thresholds.get("short_rest_days", 3))
    congested_count = int(thresholds.get("congested_match_count", 4))

    today = dt.date.today()
    horizon = today + dt.timedelta(days=window_days)

    rows = conn.execute(
        """SELECT event, kickoff_time, team_h, team_a
           FROM fixtures
           WHERE finished = 0 AND kickoff_time IS NOT NULL
             AND (team_h = ? OR team_a = ?)
           ORDER BY kickoff_time""",
        (team_id, team_id),
    ).fetchall()

    dates = []
    for r in rows:
        d = _parse_date(r["kickoff_time"])
        if d and today <= d <= horizon:
            dates.append(d)

    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    min_gap = min(gaps) if gaps else None

    return {
        "matches_in_window": len(dates),
        "window_days": window_days,
        "min_gap_days": min_gap,
        "short_rest": min_gap is not None and min_gap <= short_rest,
        "congested": len(dates) >= congested_count,
        "next_dates": dates[:5],
    }


def upcoming_cup_rounds(cfg: Config, on: dt.date | None = None,
                        horizon_days: int = 14) -> list[dict]:
    """Domestic cup rounds close enough to affect league team selection."""
    on = on or dt.date.today()
    out = []
    for r in (cfg.calendar or {}).get("cup_rounds") or []:
        date = _parse_date(r.get("date"))
        if date and on <= date <= on + dt.timedelta(days=horizon_days):
            out.append({
                "competition": r.get("competition", "Cup"),
                "round": r.get("round", ""),
                "date": date,
                "days_until": (date - on).days,
            })
    return sorted(out, key=lambda r: r["date"])


def _team_rotation_label(conn: sqlite3.Connection, team_id: int | None) -> str:
    """Observed rotation tendency; imported lazily to avoid a circular import."""
    if team_id is None:
        return "unknown"
    from .squad_intel import team_rotation_profile
    return team_rotation_profile(conn, team_id)["label"]


def rotation_risk(conn: sqlite3.Connection, cfg: Config, player: dict) -> dict:
    """Composite rotation/availability risk for one player.

    Combines league fixture congestion, midweek European load, international
    windows and tournament absence into a single 0-10 score with reasons.
    """
    reasons: list[str] = []
    score = 0.0

    load = team_fixture_load(conn, cfg, player["team_id"])
    if load["congested"]:
        score += 2.0
        reasons.append(
            f"{load['matches_in_window']} matches in {load['window_days']} days")
    if load["short_rest"]:
        score += 1.5
        reasons.append(f"only {load['min_gap_days']} days between matches")

    comps = [c for c in team_competitions(cfg, player.get("team_short", "")) if c["midweek"]]
    for comp in comps:
        if comp["all_clubs"]:
            continue
        score += _IMPACT_WEIGHT.get(comp["impact"], 1.0)
        reasons.append(f"midweek {comp['name']}")

    for ev in active_events(cfg, horizon_days=21):
        if ev["kind"] != "international_breaks":
            continue
        weight = 1.0 if ev["in_progress"] else 0.5
        score += weight
        when = "now" if ev["in_progress"] else f"in {ev['days_until']}d"
        reasons.append(f"{ev['name']} ({when})")

    for ev in player_tournament_risk(cfg, player.get("region")):
        score += _IMPACT_WEIGHT.get(ev["impact"], 2.0) + (2.0 if ev["removes_player"] else 0.0)
        when = "in progress" if ev["in_progress"] else f"starts in {ev['days_until']}d"
        reasons.append(f"⚠ {ev['name']} — {when}")

    minutes = player.get("minutes") or 0
    starts = player.get("starts") or 0
    if starts and minutes / max(starts, 1) < 60:
        score += 1.0
        reasons.append("averaging under 60 mins per start")

    for cup in upcoming_cup_rounds(cfg, horizon_days=10):
        score += 1.0
        reasons.append(f"{cup['competition']} {cup['round']} in {cup['days_until']}d")

    # Clubs that demonstrably churn their XI carry extra risk for every player.
    profile = _team_rotation_label(conn, player.get("team_id"))
    if profile == "heavy rotator":
        score += 1.5
        reasons.append("club rotates heavily (observed)")
    elif profile == "moderate rotator":
        score += 0.5

    score = min(score, 10.0)
    if score >= 6:
        band = "🔴 High"
    elif score >= 3:
        band = "🟠 Medium"
    elif score > 0:
        band = "🟡 Low"
    else:
        band = "🟢 Minimal"

    return {
        "score": round(score, 1),
        "band": band,
        "reasons": reasons,
        "load": load,
    }


def squad_rotation_report(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    gw = current_gw(conn)
    rows = conn.execute(
        """SELECT p.*, t.short_name AS team_short
           FROM my_picks mp
           JOIN players p ON p.id = mp.player_id
           JOIN teams t ON t.id = p.team_id
           WHERE mp.gw = ?""",
        (gw,),
    ).fetchall()
    report = []
    for r in rows:
        player = dict(r)
        player["risk"] = rotation_risk(conn, cfg, player)
        report.append(player)
    report.sort(key=lambda p: p["risk"]["score"], reverse=True)
    return report


def unmapped_regions(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Region ids present in the data but missing from config/regions.yaml."""
    known = set((cfg.regions or {}).keys())
    rows = conn.execute(
        """SELECT region, COUNT(*) n, GROUP_CONCAT(web_name) names
           FROM players WHERE region IS NOT NULL
           GROUP BY region ORDER BY n DESC"""
    ).fetchall()
    out = []
    for r in rows:
        if r["region"] not in known:
            out.append({
                "region": r["region"],
                "count": r["n"],
                "sample": ", ".join((r["names"] or "").split(",")[:4]),
            })
    return out
