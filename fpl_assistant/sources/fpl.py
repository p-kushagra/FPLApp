"""FPL API adapter. Every method returns a SourceResult; none raise.

Supersedes `fpl_assistant.fpl_client`, which stays as a deprecation shim for one
release. The behavioural differences from v1:

  * requests go through the token bucket rather than a fixed 1s sleep
  * responses are cached with per-endpoint TTLs
  * failures return DEGRADED/UNAVAILABLE instead of raising
"""
from __future__ import annotations

import sqlite3

import requests

from .. import cache
from .base import SourceResult
from .http import request_json

BASE = "https://fantasy.premierleague.com/api"
SOURCE = "fpl"
LEAGUE_SOURCE = "fpl_league"
HEADERS = {"User-Agent": "fpl-squad-assistant/2.0 (personal, local use)"}

OVERALL_LEAGUE_ID = 314  # the global "Overall" league, used as a top-50k proxy


class FplSource:
    """Cached, rate-limited, non-raising FPL client."""

    def __init__(self, conn: sqlite3.Connection, session: requests.Session | None = None):
        self.conn = conn
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    # -- internals ---------------------------------------------------------
    def _get(self, path: str, key: str, tier: str, *, source: str = SOURCE,
             force: bool = False) -> SourceResult:
        return cache.get_or_revalidate(
            self.conn,
            key=key,
            tier=tier,
            fetch_fn=lambda: request_json(
                self.conn, self.session, f"{BASE}/{path}", source
            ),
            source=source,
            force=force,
        )

    # -- reference data ----------------------------------------------------
    def bootstrap(self, force: bool = False) -> SourceResult:
        """Players, teams, events. The 2.5 MB response behind most of the app."""
        return self._get("bootstrap-static/", "fpl:bootstrap", "fpl_static", force=force)

    def fixtures(self, force: bool = False) -> SourceResult:
        return self._get("fixtures/", "fpl:fixtures", "fpl_fixtures", force=force)

    def element_summary(self, player_id: int) -> SourceResult:
        return self._get(f"element-summary/{player_id}/",
                         f"fpl:element:{player_id}", "fpl_static")

    # -- live scoring ------------------------------------------------------
    def live(self, gw: int, force: bool = False) -> SourceResult:
        """One request covers every player for the gameweek."""
        return self._get(f"event/{gw}/live/", f"fpl:live:{gw}", "fpl_live", force=force)

    # -- entries -----------------------------------------------------------
    def entry(self, team_id: int) -> SourceResult:
        return self._get(f"entry/{team_id}/", f"fpl:entry:{team_id}", "fpl_entry")

    def entry_history(self, team_id: int) -> SourceResult:
        """Per-GW history incl. event_transfers_cost -- reconciles the FT bank."""
        return self._get(f"entry/{team_id}/history/",
                         f"fpl:entry_history:{team_id}", "fpl_entry")

    def picks(self, team_id: int, gw: int, frozen: bool = False) -> SourceResult:
        """A squad for one gameweek.

        `frozen=True` routes to the write-once `ml_picks` tier, which is how a
        rival's post-deadline squad becomes immutable (ADR-005). Use it only
        after the deadline has actually passed -- a squad cached as frozen
        before then is wrong forever.
        """
        tier = "ml_picks" if frozen else "fpl_entry"
        return self._get(f"entry/{team_id}/event/{gw}/picks/",
                         f"fpl:picks:{team_id}:{gw}", tier)

    # -- leagues -----------------------------------------------------------
    def league_standings(self, league_id: int, page: int = 1) -> SourceResult:
        return self._get(
            f"leagues-classic/{league_id}/standings/?page_standings={page}",
            f"ml:standings:{league_id}:{page}", "ml_standings", source=LEAGUE_SOURCE,
        )

    def league_entries(self, league_id: int, limit: int = 50) -> SourceResult:
        """Walk standings pages until `limit` entries are collected.

        Partial results are returned rather than discarded: eight of twelve
        rivals still yields a usable ILEO with an adjusted denominator, which is
        exactly what the degradation matrix asks for.
        """
        entries: list[dict] = []
        page = 1
        worst: SourceResult | None = None

        while len(entries) < limit:
            result = self.league_standings(league_id, page)
            if worst is None or result.quality.severity > worst.quality.severity:
                worst = result
            if not result.usable:
                break

            standings = (result.data or {}).get("standings", {})
            rows = standings.get("results") or []
            if not rows:
                break
            entries.extend(rows)
            if not standings.get("has_next"):
                break
            page += 1

        if worst is None:
            return SourceResult.unavailable(LEAGUE_SOURCE, "no pages fetched")

        return SourceResult(
            data=entries[:limit],
            quality=worst.quality,
            source=LEAGUE_SOURCE,
            fetched_at=worst.fetched_at,
            age_seconds=worst.age_seconds,
            error=worst.error,
        )
