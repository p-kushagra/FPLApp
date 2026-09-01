"""Effective ownership, global and intra-league.

Global EO answers "what is the field doing". ILEO answers the question that
actually decides a mini-league: what are the eleven people I am racing doing.

    ILEO_p = (1/|R|) * sum over rivals r of multiplier(r, p)

and the quantity everything else is built on, the swing:

    swing_p = my_multiplier_p - ILEO_p        (points gained per point p scores)

Reading the sign is the whole feature. A large fraction of a typical squad has
swing == 0 -- shared holdings that literally cannot move your rank however many
points they score -- and no v1 surface makes that visible.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum


class Exposure(str, Enum):
    OVER = "over"              # you own more than the field: needs to haul
    UNDER = "under"            # the field owns more: needs to blank
    NEUTRALISED = "neutral"    # shared holding: cannot move your rank
    IRRELEVANT = "irrelevant"  # nobody owns it


@dataclass(frozen=True)
class SwingRow:
    player_id: int
    web_name: str
    team_short: str
    position: str
    my_multiplier: float
    ileo: float
    swing: float
    rival_count: int
    owned_by: dict[int, float] = field(default_factory=dict)
    points: float = 0.0
    xp: float = 0.0

    @property
    def exposure(self) -> Exposure:
        if abs(self.swing) < 1e-9:
            return (Exposure.IRRELEVANT if self.my_multiplier == 0
                    else Exposure.NEUTRALISED)
        return Exposure.OVER if self.swing > 0 else Exposure.UNDER

    @property
    def realised_swing(self) -> float:
        """Points actually gained on the average rival from this player."""
        return round(self.swing * self.points, 2)

    @property
    def expected_swing(self) -> float:
        return round(self.swing * self.xp, 2)


@dataclass
class SwingMatrix:
    gw: int
    league_id: int
    rival_ids: list[int]
    rows: list[SwingRow] = field(default_factory=list)
    rivals_requested: int = 0
    frozen: bool = False

    @property
    def partial(self) -> bool:
        """True when fewer rivals were retrieved than were asked for."""
        return len(self.rival_ids) < self.rivals_requested

    @property
    def coverage_note(self) -> str | None:
        if not self.partial:
            return None
        return f"ILEO over {len(self.rival_ids)} of {self.rivals_requested} rivals"

    def needs_haul(self) -> list[SwingRow]:
        return sorted([r for r in self.rows if r.exposure is Exposure.OVER],
                      key=lambda r: -r.swing)

    def needs_blank(self) -> list[SwingRow]:
        return sorted([r for r in self.rows if r.exposure is Exposure.UNDER],
                      key=lambda r: r.swing)

    def neutralised(self) -> list[SwingRow]:
        return [r for r in self.rows if r.exposure is Exposure.NEUTRALISED]

    def net_realised(self) -> float:
        return round(sum(r.realised_swing for r in self.rows), 2)

    def net_expected(self) -> float:
        return round(sum(r.expected_swing for r in self.rows), 2)


# --------------------------------------------------------------------------
def global_eo(conn: sqlite3.Connection, gw: int) -> dict[int, float]:
    """EO over the sampled top-manager pool, from the v1 `top_owned` table."""
    rows = conn.execute(
        "SELECT player_id, ownership_pct, captain_pct FROM top_owned WHERE gw = ?",
        (gw,),
    ).fetchall()
    # EO = start ownership + captain ownership (the captain's second copy).
    return {
        r["player_id"]: round(
            (float(r["ownership_pct"] or 0) + float(r["captain_pct"] or 0)) / 100.0, 4
        )
        for r in rows
    }


def rival_multipliers(conn: sqlite3.Connection, gw: int,
                      rival_ids: list[int]) -> dict[int, dict[int, float]]:
    """{player_id: {entry_id: multiplier}} from frozen rival picks."""
    if not rival_ids:
        return {}
    marks = ",".join("?" * len(rival_ids))
    rows = conn.execute(
        f"""SELECT entry_id, player_id, multiplier FROM league_rival_pick
            WHERE gw = ? AND entry_id IN ({marks})""",
        [gw, *rival_ids],
    ).fetchall()

    out: dict[int, dict[int, float]] = {}
    for r in rows:
        out.setdefault(r["player_id"], {})[r["entry_id"]] = float(r["multiplier"] or 0)
    return out


def my_multipliers(conn: sqlite3.Connection, gw: int) -> dict[int, float]:
    rows = conn.execute(
        "SELECT player_id, multiplier FROM my_picks WHERE gw = ?", (gw,)
    ).fetchall()
    return {r["player_id"]: float(r["multiplier"] or 0) for r in rows}


def ileo(conn: sqlite3.Connection, gw: int, rival_ids: list[int]) -> dict[int, float]:
    """Intra-league effective ownership over the given rival set."""
    if not rival_ids:
        return {}
    # The denominator is the rivals actually retrieved, not the number asked
    # for -- a partial freeze must not silently deflate everyone's ILEO.
    present = sorted({
        r["entry_id"] for r in conn.execute(
            f"""SELECT DISTINCT entry_id FROM league_rival_pick
                WHERE gw = ? AND entry_id IN ({','.join('?' * len(rival_ids))})""",
            [gw, *rival_ids],
        )
    })
    if not present:
        return {}

    mults = rival_multipliers(conn, gw, present)
    n = len(present)
    return {pid: round(sum(m.values()) / n, 4) for pid, m in mults.items()}


def swing_matrix(conn: sqlite3.Connection, gw: int, rival_ids: list[int],
                 league_id: int = 0,
                 include_points: bool = True) -> SwingMatrix:
    """Full per-player swing analysis against a rival set."""
    present = sorted({
        r["entry_id"] for r in conn.execute(
            f"""SELECT DISTINCT entry_id FROM league_rival_pick
                WHERE gw = ? AND entry_id IN ({','.join('?' * len(rival_ids))})""",
            [gw, *rival_ids],
        )
    }) if rival_ids else []

    matrix = SwingMatrix(gw=gw, league_id=league_id, rival_ids=present,
                         rivals_requested=len(rival_ids))
    if not present:
        return matrix

    mults = rival_multipliers(conn, gw, present)
    mine = my_multipliers(conn, gw)
    n = len(present)

    frozen = conn.execute(
        "SELECT COUNT(*) c FROM league_rival_pick WHERE gw = ? AND frozen = 1", (gw,)
    ).fetchone()
    matrix.frozen = bool(frozen and frozen["c"])

    points: dict[int, float] = {}
    if include_points:
        points = {
            r["player_id"]: float(r["total_points"] or 0)
            for r in conn.execute(
                "SELECT player_id, total_points FROM player_gw WHERE gw = ?", (gw,)
            )
        }

    xp = {
        r["player_id"]: float(r["xp_total"] or 0)
        for r in conn.execute(
            """SELECT player_id, xp_total FROM xp_projection
               WHERE gw = ? AND run_id = (SELECT run_id FROM xp_projection
                                          WHERE gw = ? ORDER BY computed_at DESC
                                          LIMIT 1)""",
            (gw, gw),
        )
    }

    meta = {
        r["id"]: dict(r) for r in conn.execute(
            """SELECT p.id, p.web_name, p.position, t.short_name AS team_short
               FROM players p LEFT JOIN teams t ON t.id = p.team_id"""
        )
    }

    for pid in sorted(set(mults) | set(mine)):
        owned = mults.get(pid, {})
        league_eo = sum(owned.values()) / n
        mine_mult = mine.get(pid, 0.0)
        info = meta.get(pid, {})
        matrix.rows.append(SwingRow(
            player_id=pid,
            web_name=info.get("web_name") or str(pid),
            team_short=info.get("team_short") or "?",
            position=info.get("position") or "?",
            my_multiplier=mine_mult,
            ileo=round(league_eo, 4),
            swing=round(mine_mult - league_eo, 4),
            rival_count=n,
            owned_by=owned,
            points=points.get(pid, 0.0),
            xp=xp.get(pid, 0.0),
        ))

    matrix.rows.sort(key=lambda r: -abs(r.expected_swing or r.realised_swing))
    return matrix


def persist_ileo(conn: sqlite3.Connection, matrix: SwingMatrix) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.executemany(
        """INSERT OR REPLACE INTO ileo_cache
             (league_id, gw, player_id, rival_count, ileo, my_multiplier,
              swing_per_point, owned_by, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (matrix.league_id, matrix.gw, r.player_id, r.rival_count, r.ileo,
             r.my_multiplier, r.swing, json.dumps(r.owned_by), now)
            for r in matrix.rows
        ],
    )
    conn.commit()
    return len(matrix.rows)


def captain_ileo(conn: sqlite3.Connection, gw: int,
                 rival_ids: list[int]) -> dict[int, float]:
    """Fraction of the rival set captaining each player. Feeds Shield/Sword."""
    if not rival_ids:
        return {}
    marks = ",".join("?" * len(rival_ids))
    rows = conn.execute(
        f"""SELECT player_id, COUNT(*) n FROM league_rival_pick
            WHERE gw = ? AND entry_id IN ({marks}) AND is_captain = 1
            GROUP BY player_id""",
        [gw, *rival_ids],
    ).fetchall()
    present = conn.execute(
        f"""SELECT COUNT(DISTINCT entry_id) n FROM league_rival_pick
            WHERE gw = ? AND entry_id IN ({marks})""",
        [gw, *rival_ids],
    ).fetchone()
    n = int(present["n"]) if present and present["n"] else 0
    if not n:
        return {}
    return {r["player_id"]: round(float(r["n"]) / n, 4) for r in rows}
