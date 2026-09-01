"""Understat adapter. Enrichment only -- never a hard dependency (ADR-004).

Understat used to embed its data in the page HTML as hex-escaped payloads:

    var playersData = JSON.parse('\\x5B\\x7B\\x22id\\x22...');

It no longer does. The site now renders an 18 KB shell and fetches its data over
AJAX, so the page contains no `playersData` at all and `extract` correctly
reported a structure change. The scraper is therefore replaced by the JSON
endpoints the site's own front-end calls:

    GET getLeagueData/{league}/{season}  -> {teams, players, dates}
    GET getPlayerData/{id}               -> {player, matches, groups, shots, ...}
    GET getMatchData/{id}                -> {rosters, shots, tmpl}

**The load-bearing detail is `X-Requested-With: XMLHttpRequest`.** Without that
header every endpoint returns a 404 HTML error page; with it, any User-Agent
works -- including this project's own. It is an AJAX-only route guard, not an
anti-bot system: the origin is a plain Apache server with no Cloudflare in front
of it, so a browser User-Agent, `cloudscraper` or `curl_cffi` change nothing
here. The realistic UA below is politeness, not a bypass.

Each endpoint returns several payloads at once, so one fetch now serves what
used to take two page loads (`playersData` + `teamsData` came from the same URL
fetched twice). Results are cached whole and sliced per accessor.

`extract` is kept: a few pages still inline small `JSON.parse` payloads
(`player`, `match_info`), and it remains the right tool if the site moves data
back into the markup. It raises `Malformed` rather than returning empty data
when a variable is missing, because a silent markup change that yielded zeros
would poison the xP model far more damagingly than an outage -- nothing would
look wrong.

Every consumer of this module must have an FPL-native fallback. The season
league endpoint is one request for all ~700 players, so it is always tried
before any per-player fetch.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import requests

from .. import cache
from .base import Malformed, Quality, SourceResult
from .http import request_json, request_text

BASE = "https://understat.com"
SOURCE = "understat"

# Understat's own front-end sends this; the endpoints 404 without it. This is
# the entire fix -- see the module docstring.
AJAX_HEADER = "X-Requested-With"
AJAX_VALUE = "XMLHttpRequest"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# Understat is slower than the FPL API and is enrichment, never blocking, so it
# gets a tighter ceiling than the 30s global default: a stalled request here
# should fail over to the FPL baseline rather than hold up an ingest.
TIMEOUT = 15.0

# var <name> = JSON.parse('<payload>');
_PATTERN = re.compile(
    r"var\s+(?P<name>\w+)\s*=\s*JSON\.parse\(\s*'(?P<payload>.*?)'\s*\)\s*;",
    re.DOTALL,
)


def extract(html: str, variable: str):
    """Pull one JSON.parse payload out of an Understat page.

    Raises Malformed if the variable is absent -- see the module docstring for
    why that is deliberately louder than returning None.
    """
    if not html:
        raise Malformed("empty response body")

    for match in _PATTERN.finditer(html):
        if match.group("name") != variable:
            continue
        raw = match.group("payload")
        try:
            # Understat hex-escapes the JSON (\x22 for a quote).
            decoded = raw.encode("utf-8").decode("unicode_escape")
            return json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Malformed(
                f"variable {variable!r} found but did not decode: {exc}"
            ) from exc

    found = sorted({m.group("name") for m in _PATTERN.finditer(html)})
    raise Malformed(
        f"variable {variable!r} not found - page structure changed. "
        f"Variables present: {found or 'none'}"
    )


def available_variables(html: str) -> list[str]:
    """Diagnostic for the Refresh Config page when extraction starts failing."""
    return sorted({m.group("name") for m in _PATTERN.finditer(html)})


def select(payload: Any, field: str) -> Any:
    """Pull one top-level field out of an endpoint payload.

    Missing means the response shape changed, which is exactly the condition
    `extract` used to catch on the HTML. Same reasoning, same loudness.
    """
    if not isinstance(payload, dict):
        raise Malformed(
            f"expected a JSON object, got {type(payload).__name__}")
    if field not in payload:
        raise Malformed(
            f"field {field!r} missing from response - API shape changed. "
            f"Fields present: {sorted(payload) or 'none'}")
    return payload[field]


class UnderstatSource:
    """Cached, rate-limited, non-raising Understat client."""

    def __init__(self, conn: sqlite3.Connection, session: requests.Session | None = None,
                 enabled: bool = True):
        self.conn = conn
        self.enabled = enabled
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    # -- transport ---------------------------------------------------------
    def _api(self, path: str, key: str, tier: str) -> SourceResult:
        """Fetch and cache a whole JSON payload from an AJAX endpoint."""
        if not self.enabled:
            return SourceResult.unavailable(SOURCE, "disabled by configuration")

        def fetch():
            return request_json(
                self.conn, self.session, f"{BASE}/{path}", SOURCE,
                timeout=TIMEOUT, headers={AJAX_HEADER: AJAX_VALUE},
            )

        return cache.get_or_revalidate(
            self.conn, key=key, tier=tier, fetch_fn=fetch, source=SOURCE
        )

    def _page(self, path: str, variable: str, key: str, tier: str) -> SourceResult:
        """Fetch an HTML page and extract an inlined JSON.parse variable.

        Retained for the payloads still delivered in markup, and as the route
        back if Understat inlines its data again.
        """
        if not self.enabled:
            return SourceResult.unavailable(SOURCE, "disabled by configuration")

        def fetch():
            html = request_text(self.conn, self.session, f"{BASE}/{path}",
                                SOURCE, timeout=TIMEOUT)
            return extract(html, variable)

        return cache.get_or_revalidate(
            self.conn, key=key, tier=tier, fetch_fn=fetch, source=SOURCE
        )

    @staticmethod
    def _slice(result: SourceResult, field: str) -> SourceResult:
        """Narrow a cached multi-payload response to one of its fields.

        Quality is carried through unchanged: a field taken from a DEGRADED
        cached payload is still DEGRADED, and the badge must keep saying so.
        """
        if not result.usable:
            return result
        try:
            data = select(result.data, field)
        except Malformed as exc:
            return SourceResult(
                data=None, quality=Quality.UNAVAILABLE, source=result.source,
                fetched_at=result.fetched_at, age_seconds=result.age_seconds,
                error=str(exc))
        return SourceResult(
            data=data, quality=result.quality, source=result.source,
            fetched_at=result.fetched_at, age_seconds=result.age_seconds,
            error=result.error)

    # -- endpoints ---------------------------------------------------------
    def league_data(self, season: int, league: str = "EPL") -> SourceResult:
        """Teams, players and fixtures for a season. ONE request covers all."""
        return self._api(f"getLeagueData/{league}/{season}",
                         f"us:league:{league}:{season}:data", "understat_league")

    def league_players(self, season: int) -> SourceResult:
        """Season aggregates for every player."""
        return self._slice(self.league_data(season), "players")

    def league_teams(self, season: int) -> SourceResult:
        """Team-level xG/xGA, feeding the opponent adjustment."""
        return self._slice(self.league_data(season), "teams")

    def league_fixtures(self, season: int) -> SourceResult:
        """Match list with per-side xG. New: the old scrape never exposed it."""
        return self._slice(self.league_data(season), "dates")

    def player_data(self, understat_id: str) -> SourceResult:
        """Everything the player page shows: matches, groups and shots."""
        return self._api(f"getPlayerData/{understat_id}",
                         f"us:player:{understat_id}:data", "understat_player")

    def player_matches(self, understat_id: str) -> SourceResult:
        """Per-match history for one player."""
        return self._slice(self.player_data(understat_id), "matches")

    def player_groups(self, understat_id: str) -> SourceResult:
        """Season/position/situation splits for one player."""
        return self._slice(self.player_data(understat_id), "groups")

    def player_shots(self, understat_id: str) -> SourceResult:
        """Shot-level detail for one player, across seasons."""
        return self._slice(self.player_data(understat_id), "shots")

    def match_data(self, match_id: str) -> SourceResult:
        """Rosters and shots for one match. Frozen tier: a result never changes."""
        return self._api(f"getMatchData/{match_id}",
                         f"us:match:{match_id}:data", "understat_match")

    def match_shots(self, match_id: str) -> SourceResult:
        """Shot-level detail for one match."""
        return self._slice(self.match_data(match_id), "shots")

    def match_rosters(self, match_id: str) -> SourceResult:
        return self._slice(self.match_data(match_id), "rosters")
