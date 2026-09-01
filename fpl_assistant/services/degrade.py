"""Data-quality envelope: what the UI needs to know before it renders anything.

Collected once per page load and passed down. Every panel that is running on a
fallback says so AT THE POINT OF USE, not only in a global banner -- a badge in
the corner is easy to miss, and a variance chart quietly computed from FPL's
coarser xG instead of Understat's shot-level data looks identical to a correct
one.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceState:
    name: str
    quality: str                  # ok | degraded | down | unknown
    last_success: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    @property
    def healthy(self) -> bool:
        return self.quality in ("ok", "unknown")


@dataclass
class DataQuality:
    """Everything the badges, banners and empty states are driven by."""

    sources: dict[str, SourceState] = field(default_factory=dict)
    understat_offline: bool = False
    has_fpl_data: bool = False
    has_squad: bool = False
    has_projections: bool = False
    has_rivals: bool = False
    has_history: bool = False
    fpl_last_ingest: str | None = None
    projection_run_id: str | None = None
    projection_age_hours: float | None = None
    xp_source_mix: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- badges ------------------------------------------------------------
    @property
    def understat_badge(self) -> str | None:
        if not self.understat_offline:
            return None
        return "Understat Offline - Using Baseline Stats"

    @property
    def baseline_share(self) -> float:
        """Fraction of projections running on the FPL fallback."""
        total = sum(self.xp_source_mix.values())
        if not total:
            return 0.0
        return self.xp_source_mix.get("fpl_baseline", 0) / total

    @property
    def on_baseline(self) -> bool:
        """True when the xP engine is mostly not using Understat.

        Distinct from `understat_offline`: the source can be perfectly healthy
        and still unused, because nothing has been ingested or resolved yet.
        Both cases owe the operator the same badge, for different reasons.
        """
        return self.baseline_share > 0.5

    @property
    def stale_projections(self) -> bool:
        return (self.projection_age_hours or 0) > 24

    def blocking_reason(self) -> str | None:
        """Why a decision page cannot render at all, if it cannot."""
        if not self.has_fpl_data:
            return ("No FPL data yet. Open **Refresh Config** and run "
                    "**FPL data**, then **History**.")
        return None


def _scalar(conn: sqlite3.Connection, sql: str, *params) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def collect(conn: sqlite3.Connection) -> DataQuality:
    """Snapshot data availability and source health. Never raises."""
    q = DataQuality()

    try:
        for r in conn.execute(
            """SELECT source, quality, last_success_at, last_error,
                      consecutive_failures FROM source_health"""
        ):
            q.sources[r["source"]] = SourceState(
                name=r["source"], quality=r["quality"] or "unknown",
                last_success=r["last_success_at"], last_error=r["last_error"],
                consecutive_failures=int(r["consecutive_failures"] or 0),
            )
    except sqlite3.Error:
        pass

    understat = q.sources.get("understat")
    q.understat_offline = bool(understat and understat.quality == "down")

    q.has_fpl_data = _scalar(conn, "SELECT COUNT(*) FROM players") > 0
    q.has_history = _scalar(conn, "SELECT COUNT(*) FROM player_gw") > 0
    q.has_squad = _scalar(conn, "SELECT COUNT(*) FROM my_picks") > 0
    q.has_rivals = _scalar(conn, "SELECT COUNT(*) FROM league_rival_pick") > 0

    try:
        row = conn.execute(
            """SELECT run_id, computed_at, COUNT(*) n FROM xp_projection
               GROUP BY run_id ORDER BY computed_at DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:
        row = None

    if row:
        q.has_projections = True
        q.projection_run_id = row["run_id"]
        try:
            when = dt.datetime.fromisoformat(str(row["computed_at"]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            q.projection_age_hours = (
                dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600.0
        except (ValueError, TypeError):
            q.projection_age_hours = None

        try:
            q.xp_source_mix = {
                r["source"]: int(r["n"]) for r in conn.execute(
                    """SELECT source, COUNT(*) n FROM xp_projection
                       WHERE run_id = ? GROUP BY source""",
                    (row["run_id"],),
                )
            }
        except sqlite3.Error:
            pass

    try:
        q.fpl_last_ingest = (conn.execute(
            "SELECT value FROM meta WHERE key = 'fpl_last_ingest'"
        ).fetchone() or [None])[0]
    except sqlite3.Error:
        pass

    if q.on_baseline and not q.understat_offline:
        q.notes.append(
            "Projections are on FPL baseline stats: no Understat data has been "
            "ingested or resolved yet."
        )
    if q.stale_projections:
        q.notes.append(
            f"Projections are {q.projection_age_hours:.0f}h old.")

    return q
