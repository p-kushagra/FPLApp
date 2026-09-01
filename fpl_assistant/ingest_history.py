"""Seed `historical_player_baselines` from FPL history and Understat.

Three passes, in descending order of evidence quality:

1. **FPL `element-summary` / `history_past`** - one request per player, served
   through the cached, rate-limited `FplSource` adapter so a re-run costs
   nothing and a 429 degrades instead of raising. Each past season becomes one
   row. FPL's `expected_goals` includes penalties, so the stored npxG is an
   over-estimate for penalty takers; the Understat pass overwrites it with the
   true penalty-free rate wherever the player is resolved.
2. **Understat season groups** - for players carrying an `understat_id`, the
   per-season aggregate (npxG, xA, minutes) from the player page. Stored as a
   separate source row; the read path in `models.priors` prefers it.
3. **Imputation** - any player left with no usable seeded season gets one
   matrix row from `models.priors.imputed_prior`, keyed by position and price.
   Materialising the imputed row (rather than imputing at read time) keeps the
   whole prior population auditable with one SELECT and means an unseeded
   database changes nothing.

No pass raises on upstream failure: sources return `SourceResult` and this
module counts what it could not get. Championship seasons cannot be ingested
automatically - neither FPL nor Understat covers the competition - so rows
with `competition='CHAMPIONSHIP'` only ever arrive by hand; the 0.68 haircut
in `models.priors.translate` is applied to them at read time.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

from .models import priors as priors_mod
from .rules import ELEMENT_TYPE_TO_POS
from .sources.fpl import FplSource
from .sources.understat import UnderstatSource

# How many past seasons per player are worth keeping. Rates from three years
# ago describe a different player; they only matter when nothing newer exists.
MAX_SEASONS = 3


@dataclass
class SeedReport:
    players: int = 0
    fetched: int = 0
    history_rows: int = 0
    understat_players: int = 0
    understat_rows: int = 0
    imputed_rows: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"players considered      {self.players}",
            f"element summaries read  {self.fetched}",
            f"fpl_history rows        {self.history_rows}",
            (f"understat rows          {self.understat_rows}"
             f" (from {self.understat_players} mapped players)"),
            f"imputed matrix rows     {self.imputed_rows}",
        ]
        if self.failures:
            lines.append(f"failures ({len(self.failures)}):")
            lines.extend(f"  - {f}" for f in self.failures[:10])
            if len(self.failures) > 10:
                lines.append(f"  ... and {len(self.failures) - 10} more")
        return "\n".join(lines)


def _per90(total, minutes: float) -> float:
    if minutes <= 0:
        return 0.0
    return 90.0 * float(total or 0.0) / minutes


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _store(conn: sqlite3.Connection, player_id: int, season: str, source: str,
           minutes: float, npxg90: float, xa90: float, xcs: float,
           defcon: float, competition: str = priors_mod.COMP_PL) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO historical_player_baselines
             (player_id, season_name, source, competition, total_minutes,
              npxg90_prior, xa90_prior, xcs_rate_prior, defcon_rate_prior,
              ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (player_id, season, source, competition, round(minutes, 1),
         round(npxg90, 4), round(xa90, 4), round(xcs, 4), round(defcon, 4),
         _now()))


# --------------------------------------------------------------------------
# Pass 1: FPL history_past
# --------------------------------------------------------------------------
def seed_fpl_history(conn: sqlite3.Connection, player_id: int,
                     seasons: list[dict], report: SeedReport) -> int:
    """Store the most recent `MAX_SEASONS` past seasons for one player."""
    stored = 0
    ordered = sorted(seasons, key=lambda s: str(s.get("season_name") or ""),
                     reverse=True)
    for season in ordered[:MAX_SEASONS]:
        name = str(season.get("season_name") or "").strip()
        minutes = float(season.get("minutes") or 0)
        if not name or minutes <= 0:
            continue
        _store(
            conn, player_id, name, priors_mod.SOURCE_HISTORY,
            minutes=minutes,
            npxg90=_per90(season.get("expected_goals"), minutes),
            xa90=_per90(season.get("expected_assists"), minutes),
            xcs=_per90(season.get("clean_sheets"), minutes),
            defcon=_per90(season.get("defensive_contribution"), minutes),
        )
        stored += 1
    report.history_rows += stored
    return stored


# --------------------------------------------------------------------------
# Pass 2: Understat season aggregates
# --------------------------------------------------------------------------
def _understat_season_name(start_year: str | int) -> str:
    try:
        y = int(start_year)
    except (TypeError, ValueError):
        return str(start_year)
    return f"{y}/{(y + 1) % 100:02d}"


def seed_understat(conn: sqlite3.Connection, player_id: int,
                   groups: dict, report: SeedReport) -> int:
    """Store per-season npxG/xA rows from an Understat groupsData payload."""
    seasons = groups.get("season") or []
    stored = 0
    for season in sorted(seasons, key=lambda s: str(s.get("season") or ""),
                         reverse=True)[:MAX_SEASONS]:
        minutes = float(season.get("time") or 0)
        if minutes <= 0:
            continue
        _store(
            conn, player_id,
            _understat_season_name(season.get("season")),
            priors_mod.SOURCE_UNDERSTAT,
            minutes=minutes,
            npxg90=_per90(season.get("npxG"), minutes),
            xa90=_per90(season.get("xA"), minutes),
            # Understat has no clean-sheet or DefCon columns; leave those to
            # the FPL row for the same season (the read path merges by
            # preferring this row only for what it carries -- see priors).
            xcs=0.0, defcon=0.0,
        )
        stored += 1
    report.understat_rows += stored
    return stored


# --------------------------------------------------------------------------
# Pass 3: imputation for zero-history assets
# --------------------------------------------------------------------------
def seed_imputed(conn: sqlite3.Connection, report: SeedReport) -> int:
    """One matrix row for every player with no usable seeded season."""
    covered = {int(r["player_id"]) for r in conn.execute(
        """SELECT DISTINCT player_id FROM historical_player_baselines
           WHERE source != ? AND total_minutes >= ?""",
        (priors_mod.SOURCE_IMPUTED, priors_mod.MIN_PRIOR_MINUTES))}

    # A player seeded with real history no longer needs the matrix row; drop
    # it so `SELECT ... WHERE source='imputed'` stays an exact census of who
    # is running on an imputed prior.
    if covered:
        conn.execute(
            f"""DELETE FROM historical_player_baselines
                WHERE source = ? AND player_id IN
                  ({','.join('?' * len(covered))})""",
            [priors_mod.SOURCE_IMPUTED, *covered])

    stored = 0
    for player in conn.execute(
            "SELECT id, element_type, now_cost FROM players"):
        pid = int(player["id"])
        if pid in covered:
            continue
        pos = ELEMENT_TYPE_TO_POS.get(player["element_type"], "MID")
        prior = priors_mod.imputed_prior(pos, player["now_cost"])
        _store(conn, pid, "imputed", priors_mod.SOURCE_IMPUTED,
               minutes=0.0, npxg90=prior.npxg90, xa90=prior.xa90,
               xcs=prior.xcs_rate, defcon=prior.defcon90)
        stored += 1
    report.imputed_rows = stored
    return stored


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def seed(conn: sqlite3.Connection, *, limit: int | None = None,
         understat: bool = True, network: bool = True,
         progress=None) -> SeedReport:
    """Run all three passes. Idempotent; safe to re-run any time."""
    report = SeedReport()
    players = [dict(r) for r in conn.execute(
        "SELECT id, web_name, understat_id FROM players ORDER BY id")]
    if limit:
        players = players[:limit]
    report.players = len(players)

    if network:
        fpl = FplSource(conn)
        for i, player in enumerate(players, start=1):
            if progress and (i % 25 == 0 or i == len(players)):
                progress(i, len(players), str(player["web_name"]))
            result = fpl.element_summary(int(player["id"]))
            if not result.usable or not isinstance(result.data, dict):
                report.failures.append(
                    f"element-summary {player['web_name']}: "
                    f"{result.error or result.quality}")
                continue
            report.fetched += 1
            seed_fpl_history(conn, int(player["id"]),
                             result.data.get("history_past") or [], report)
        conn.commit()

        if understat:
            us = UnderstatSource(conn)
            mapped = [p for p in players if p.get("understat_id")]
            report.understat_players = len(mapped)
            for player in mapped:
                result = us.player_groups(str(player["understat_id"]))
                if not result.usable or not isinstance(result.data, dict):
                    report.failures.append(
                        f"understat groups {player['web_name']}: "
                        f"{result.error or result.quality}")
                    continue
                seed_understat(conn, int(player["id"]), result.data, report)
            conn.commit()

    seed_imputed(conn, report)
    conn.commit()
    return report
