"""Mini-league discovery and rival selection.

The plumbing for ILEO has always existed -- `league_standing`,
`league_rival_pick`, `strategy/eo.py` -- but nothing ever *sourced* a league id,
so every rival-facing surface rendered its empty state forever. The missing
piece is small: `/entry/{id}/` already carries the full list of leagues the
manager is in, under `leagues.classic`. This module reads it, stores it, and
turns a league into a concrete rival set.

Two decisions worth stating, because they are what make the auto-discovery
useful rather than merely complete:

* **General leagues are discovered but not tracked.** FPL enrols every manager
  in "Overall" (7m entries), a country league, a region league and a club
  league. Their ILEO is indistinguishable from global EO and freezing eight
  arbitrary entries out of millions is noise. `league_type == 'x'` -- the
  private leagues someone actually invited you to -- is what gets tracked by
  default. The rest stay listed so they can be opted into.

* **Rivals default to the people above you.** In a mini-league the managers
  behind you cannot be caught up to; the ones you are racing are those at or
  above your rank. Auto-selection takes the top N by rank, which in a small
  league is everyone and in a large one is the people who can actually be
  overtaken this season.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from .sources.fpl import FplSource

# FPL enrols everyone in these without asking. Tracking them by default would
# freeze eight strangers out of several million and call it a rival set.
GENERAL_LEAGUE_TYPE = "s"
PRIVATE_LEAGUE_TYPE = "x"

DEFAULT_RIVAL_COUNT = 8
MAX_RIVALS = 20


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def discover(conn: sqlite3.Connection, team_id: int,
             auto_track: bool = True) -> dict:
    """Read every classic league this manager is in and store them.

    Idempotent, and deliberately non-destructive about `tracked`: a league the
    user has already opted into or out of keeps that choice across refreshes.
    Only leagues seen for the first time get the default.
    """
    if not team_id:
        return {"ok": False, "reason": "no FPL_TEAM_ID set", "leagues": 0}

    result = FplSource(conn).entry(int(team_id))
    if not result.usable:
        return {"ok": False, "reason": result.error or "entry unavailable",
                "quality": result.quality.value, "leagues": 0}

    classic = ((result.data or {}).get("leagues") or {}).get("classic") or []
    known = {
        int(r["league_id"]) for r in conn.execute("SELECT league_id FROM league")
    }

    now = _now()
    discovered = tracked = 0
    for entry in classic:
        league_id = entry.get("id")
        if league_id is None:
            continue
        league_id = int(league_id)
        league_type = entry.get("league_type") or GENERAL_LEAGUE_TYPE
        is_new = league_id not in known

        if is_new:
            default_tracked = int(
                bool(auto_track) and league_type == PRIVATE_LEAGUE_TYPE)
            conn.execute(
                """INSERT INTO league (league_id, name, league_type, my_rank,
                                       my_last_rank, tracked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (league_id, entry.get("name"), league_type,
                 entry.get("entry_rank"), entry.get("entry_last_rank"),
                 default_tracked, now))
            tracked += default_tracked
        else:
            # Refresh the volatile fields only. `tracked` is the user's.
            conn.execute(
                """UPDATE league SET name = ?, league_type = ?, my_rank = ?,
                                     my_last_rank = ?, updated_at = ?
                   WHERE league_id = ?""",
                (entry.get("name"), league_type, entry.get("entry_rank"),
                 entry.get("entry_last_rank"), now, league_id))
        discovered += 1

    conn.commit()
    return {"ok": True, "leagues": discovered, "newly_tracked": tracked,
            "quality": result.quality.value}


def all_leagues(conn: sqlite3.Connection) -> list[dict]:
    """Every discovered league, private ones first, then by rank."""
    try:
        return [dict(r) for r in conn.execute(
            """SELECT league_id, name, league_type, my_rank, my_last_rank,
                      entry_count, tracked
               FROM league
               ORDER BY (league_type = 'x') DESC, tracked DESC,
                        COALESCE(entry_count, 999999999), name""")]
    except sqlite3.Error:
        return []


def tracked_leagues(conn: sqlite3.Connection) -> list[dict]:
    return [lg for lg in all_leagues(conn) if lg.get("tracked")]


def tracked_ids(conn: sqlite3.Connection) -> list[int]:
    return [int(lg["league_id"]) for lg in tracked_leagues(conn)]


def set_tracked(conn: sqlite3.Connection, league_id: int, tracked: bool) -> None:
    conn.execute("UPDATE league SET tracked = ?, updated_at = ? "
                 "WHERE league_id = ?",
                 (int(bool(tracked)), _now(), int(league_id)))
    conn.commit()


# --------------------------------------------------------------------------
# Rival selection
# --------------------------------------------------------------------------
def standings(conn: sqlite3.Connection, league_id: int,
              gw: int | None = None) -> list[dict]:
    """The most recently ingested standings page for one league."""
    if gw is None:
        row = conn.execute(
            "SELECT MAX(gw) g FROM league_standing WHERE league_id = ?",
            (int(league_id),)).fetchone()
        gw = int(row["g"]) if row and row["g"] is not None else None
    if gw is None:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT entry_id, player_name, entry_name, rank, last_rank,
                  event_total, total, is_rival
           FROM league_standing WHERE league_id = ? AND gw = ?
           ORDER BY rank""", (int(league_id), int(gw)))]


def set_rivals(conn: sqlite3.Connection, league_id: int,
               entry_ids: list[int]) -> int:
    """Replace the rival flags for a league. Applies to every stored gameweek.

    Flagging per-league rather than per-gameweek keeps the selection stable as
    standings are re-ingested each week -- otherwise every refresh would silently
    drop the user's choice.
    """
    conn.execute("UPDATE league_standing SET is_rival = 0 WHERE league_id = ?",
                 (int(league_id),))
    chosen = [int(e) for e in entry_ids]
    if chosen:
        marks = ",".join("?" * len(chosen))
        conn.execute(
            f"""UPDATE league_standing SET is_rival = 1
                WHERE league_id = ? AND entry_id IN ({marks})""",
            [int(league_id), *chosen])
    conn.commit()
    return len(chosen)


def auto_select_rivals(conn: sqlite3.Connection, league_id: int,
                       count: int = DEFAULT_RIVAL_COUNT,
                       exclude_entry: int | None = None) -> list[int]:
    """Flag the top `count` entries as rivals, skipping your own team.

    Used when a league is tracked but nobody has curated a rival set. A default
    that produces a usable ILEO immediately is worth more than a correct empty
    state nobody knows how to fill.
    """
    rows = standings(conn, league_id)
    chosen: list[int] = []
    for row in rows:
        entry_id = int(row["entry_id"])
        if exclude_entry and entry_id == int(exclude_entry):
            continue
        chosen.append(entry_id)
        if len(chosen) >= max(1, min(int(count), MAX_RIVALS)):
            break
    set_rivals(conn, league_id, chosen)
    return chosen


def rival_ids(conn: sqlite3.Connection, league_id: int = 0) -> list[int]:
    """Selected rivals, across all tracked leagues when no league is named."""
    sql = ("SELECT DISTINCT s.entry_id FROM league_standing s "
           "JOIN league l ON l.league_id = s.league_id "
           "WHERE s.is_rival = 1")
    params: list = []
    if league_id:
        sql += " AND s.league_id = ?"
        params.append(int(league_id))
    else:
        sql += " AND l.tracked = 1"
    try:
        return [int(r["entry_id"]) for r in conn.execute(sql, params)]
    except sqlite3.Error:
        return []


def ensure_rivals(conn: sqlite3.Connection, league_id: int,
                  count: int = DEFAULT_RIVAL_COUNT,
                  exclude_entry: int | None = None) -> list[int]:
    """Rivals for a league, auto-selecting a default set if none are flagged."""
    existing = rival_ids(conn, league_id)
    if existing:
        return existing
    return auto_select_rivals(conn, league_id, count, exclude_entry)


def default_league(conn: sqlite3.Connection) -> int | None:
    """The league every rival-facing page falls back to.

    The smallest tracked league by entry count: a 12-person work league is where
    ILEO actually changes a decision, whereas a 40,000-entry public league
    behaves like global EO and tells you nothing new.
    """
    tracked = tracked_leagues(conn)
    if not tracked:
        return None
    return int(tracked[0]["league_id"])
