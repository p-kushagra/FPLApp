"""Pre-deadline projection freeze.

The one write-once table in the projection stack. `xp_projection` is rewritten
on every `recompute_xp`; this is captured an hour before the deadline and never
revised, which is what lets Page 1 plot Process (underlying vs *what we forecast
before kickoff*) instead of the luck axis alone.

Two rules make it trustworthy:

* **Write-once.** A second capture for the same gameweek is refused, not merged.
  A snapshot that can be rewritten after the whistle is not evidence.
* **Pre-deadline only.** Capture is refused once the deadline has passed unless
  the caller explicitly forces it, and a forced late capture is recorded as such
  in `deadline_source` so no downstream consumer mistakes it for a clean freeze.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass, field

from . import xp as xp_mod

# Freeze at deadline minus one hour: late enough that press conferences and
# most price changes have landed, early enough to survive a slow scrape.
SNAPSHOT_LEAD_MINUTES = 60.0

# How early a capture is allowed. Anything before this is refused as premature,
# because team news that lands inside the window is exactly what makes the
# snapshot worth taking.
DUE_WINDOW_MINUTES = 180.0

# FPL deadlines sit 90 minutes before the first kickoff of the gameweek. Used
# only when `gw_state` has not been synced and we must estimate.
FIXTURE_DEADLINE_OFFSET_MINUTES = 90.0

DEADLINE_OFFICIAL = "gw_state"
DEADLINE_ESTIMATED = "fixture_estimate"
DEADLINE_UNKNOWN = "unknown"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# Deadlines
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Deadline:
    gw: int
    when: dt.datetime | None
    source: str

    @property
    def known(self) -> bool:
        return self.when is not None

    def freeze_at(self) -> dt.datetime | None:
        """The instant the snapshot is meant to be taken."""
        if self.when is None:
            return None
        return self.when - dt.timedelta(minutes=SNAPSHOT_LEAD_MINUTES)


def deadline_for(conn: sqlite3.Connection, gw: int) -> Deadline:
    """Official deadline if `gw_state` has been synced, else an estimate.

    Estimating from the first kickoff is deliberately preferred over refusing:
    a snapshot taken against a 90-minute estimate is worth far more than no
    snapshot at all, and `deadline_source` keeps the difference visible to
    everything downstream.
    """
    row = conn.execute(
        "SELECT deadline_time FROM gw_state WHERE gw = ?", (gw,)).fetchone()
    if row is not None:
        official = _parse(row[0])
        if official is not None:
            return Deadline(gw, official, DEADLINE_OFFICIAL)

    row = conn.execute(
        "SELECT MIN(kickoff_time) FROM fixtures WHERE event = ?", (gw,)
    ).fetchone()
    first = _parse(row[0] if row is not None else None)
    if first is not None:
        return Deadline(
            gw, first - dt.timedelta(minutes=FIXTURE_DEADLINE_OFFSET_MINUTES),
            DEADLINE_ESTIMATED)

    return Deadline(gw, None, DEADLINE_UNKNOWN)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def has_snapshot(conn: sqlite3.Connection, gw: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM projection_snapshot_meta WHERE gw = ? AND rows > 0",
        (gw,)).fetchone()
    return row is not None


def snapshot_meta(conn: sqlite3.Connection, gw: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projection_snapshot_meta WHERE gw = ?", (gw,)).fetchone()
    return dict(row) if row is not None else None


def frozen_gws(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT gw FROM projection_snapshot_meta WHERE rows > 0 ORDER BY gw")]


def load(conn: sqlite3.Connection, gw: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM projection_snapshot WHERE gw = ? ORDER BY xp_total DESC",
        (gw,))]


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DueCheck:
    gw: int
    due: bool
    reason: str
    deadline: Deadline
    minutes_to_deadline: float | None


def check_due(conn: sqlite3.Connection, gw: int,
              now: dt.datetime | None = None) -> DueCheck:
    """Whether `gw` should be frozen right now, and why not if it should not."""
    now = now or _utcnow()
    line = deadline_for(conn, gw)

    if has_snapshot(conn, gw):
        return DueCheck(gw, False, "already frozen", line, None)
    if line.when is None:
        return DueCheck(gw, False, "no deadline known for this gameweek",
                        line, None)

    minutes = (line.when - now).total_seconds() / 60.0
    if minutes <= 0:
        return DueCheck(gw, False,
                        f"deadline passed {abs(minutes):.0f}m ago", line, minutes)
    if minutes > DUE_WINDOW_MINUTES:
        return DueCheck(gw, False,
                        f"too early - deadline is {minutes / 60:.1f}h away",
                        line, minutes)
    return DueCheck(gw, True, f"deadline in {minutes:.0f}m", line, minutes)


def due(conn: sqlite3.Connection, now: dt.datetime | None = None,
        lookahead: int = 3) -> list[DueCheck]:
    """Gameweeks a scheduler should freeze on this tick."""
    now = now or _utcnow()
    row = conn.execute("SELECT MAX(gw) FROM player_gw").fetchone()
    played = int(row[0] or 0) if row is not None else 0
    horizon = range(played + 1, played + 1 + lookahead)
    return [c for c in (check_due(conn, gw, now) for gw in horizon) if c.due]


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------
@dataclass
class SnapshotResult:
    gw: int
    rows: int = 0
    frozen: bool = False
    reason: str = ""
    run_id: str = ""
    frozen_at: str = ""
    deadline_time: str | None = None
    deadline_source: str = DEADLINE_UNKNOWN
    lead_minutes: float | None = None
    understat_ok: bool = True
    notes: list[str] = field(default_factory=list)


_COLUMNS = 30


def capture(conn: sqlite3.Connection, gw: int, *,
            now: dt.datetime | None = None,
            force: bool = False,
            understat_ok: bool | None = None,
            rules: dict | None = None) -> SnapshotResult:
    """Freeze the current projection for `gw`. Idempotent; never raises.

    Returns a result describing what happened rather than signalling refusal by
    exception: the caller is normally a background job whose only responsibility
    is to record the outcome, and a refusal is a normal outcome here, not a
    fault.
    """
    now = now or _utcnow()
    line = deadline_for(conn, gw)
    result = SnapshotResult(
        gw=gw,
        deadline_time=line.when.isoformat() if line.when else None,
        deadline_source=line.source,
    )

    late = False

    if has_snapshot(conn, gw) and not force:
        result.reason = "already frozen - snapshots are write-once"
        return result

    if not force:
        check = check_due(conn, gw, now)
        if not check.due:
            result.reason = check.reason
            return result
    elif line.when is not None and now > line.when:
        # A forced post-deadline capture is still useful -- better than nothing
        # for a gameweek that was missed -- but it is not a clean freeze, and
        # the provenance column has to say so out loud.
        result.deadline_source = f"{line.source}+late"
        result.notes.append(
            "forced after the deadline - lineups may already be public")
        late = True

    if understat_ok is None:
        from ..jobs import tasks as tasks_mod
        understat_ok = not tasks_mod.understat_offline(conn)
    result.understat_ok = bool(understat_ok)

    # On time, `as_of` is left to its default: the newest played gameweek
    # genuinely is gw-1, so the default is already the right cut-off.
    #
    # On a forced catch-up the gameweek has already been played, and the
    # default would let the "forecast" read the very results it exists to be
    # judged against -- a snapshot that scores itself. Pinning as_of=gw-1 and
    # blanking the live injury fields makes a late capture a faithful replay of
    # what the model could have known, which is the weakest claim that is still
    # true. It is not as good as a real freeze, and `deadline_source` still
    # says so.
    breakdowns = xp_mod.project(
        conn, [gw], rules=rules, understat_ok=bool(understat_ok), persist=False,
        as_of=(gw - 1) if late else None,
        neutralise_availability=late)
    if not breakdowns:
        result.reason = "projection produced no rows"
        _write_meta(conn, result)
        return result

    market = {
        int(r["id"]): dict(r) for r in conn.execute(
            """SELECT id, ep_next, now_cost, selected_by_percent, status,
                      chance_of_playing_next_round
               FROM players""")
    }

    run_id = uuid.uuid4().hex[:12]
    frozen_at = now.isoformat()
    lead = (line.when - now).total_seconds() / 60.0 if line.when else None

    payload = []
    for (pid, bd_gw), b in breakdowns.items():
        if bd_gw != gw:
            continue
        m = market.get(pid) or {}
        payload.append((
            gw, pid, b.fixtures, b.exp_minutes, b.p_start, b.p_60,
            b.appearance, b.goals, b.assists, b.clean_sheet, b.saves, b.defcon,
            b.bonus, b.conceded, b.cards, b.total, b.variance,
            b.p_haul_12, b.p_floor_5, b.source,
            _num(m.get("ep_next")), _num(m.get("now_cost")),
            _num(m.get("selected_by_percent")), m.get("status"),
            _int(m.get("chance_of_playing_next_round")),
            run_id, result.deadline_time, frozen_at, lead,
            result.deadline_source,
        ))

    # IGNORE, not REPLACE: the write-once guarantee has to hold at the row
    # level too, or a partial re-run silently rewrites half a gameweek.
    verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
    placeholders = ", ".join("?" * _COLUMNS)
    with conn:
        conn.executemany(
            f"""{verb} INTO projection_snapshot
                 (gw, player_id, fixtures, exp_minutes, p_start, p_60,
                  xp_appearance, xp_goals, xp_assists, xp_clean_sheet, xp_saves,
                  xp_defcon, xp_bonus, xp_conceded, xp_cards, xp_total,
                  xp_variance, p_haul_12, p_floor_5, source,
                  ep_next, now_cost, selected_by_pct, status, chance_of_playing,
                  run_id, deadline_time, frozen_at, lead_minutes,
                  deadline_source)
               VALUES ({placeholders})""",
            payload)

    stored = conn.execute(
        "SELECT COUNT(*) FROM projection_snapshot WHERE gw = ?", (gw,)).fetchone()

    result.rows = int(stored[0]) if stored else 0
    result.frozen = result.rows > 0
    result.run_id = run_id
    result.frozen_at = frozen_at
    result.lead_minutes = lead
    result.reason = f"froze {result.rows} projections"
    if not understat_ok:
        result.notes.append("captured on FPL baseline - Understat offline")
    _write_meta(conn, result)
    return result


def _write_meta(conn: sqlite3.Connection, r: SnapshotResult) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO projection_snapshot_meta
                 (gw, run_id, rows, deadline_time, deadline_source, frozen_at,
                  lead_minutes, understat_ok, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.gw, r.run_id, r.rows, r.deadline_time, r.deadline_source,
             r.frozen_at, r.lead_minutes, int(r.understat_ok),
             json.dumps(r.notes) if r.notes else None))


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
