"""Understat adapter. Enrichment only -- never a hard dependency (ADR-004).

Understat has no API. Its data sits in the page HTML as hex-escaped payloads:

    var playersData = JSON.parse('\\x5B\\x7B\\x22id\\x22...');

`extract` pulls one named variable out and decodes it. It raises `Malformed`
rather than returning empty data when a variable is missing, because a silent
markup change that yielded zeros would poison the xP model far more damagingly
than an outage -- nothing would look wrong.

Every consumer of this module must have an FPL-native fallback. The season
league page is one request for all ~700 players, so it is always tried before
any per-player fetch.
"""
from __future__ import annotations

import json
import re
import sqlite3

import requests

from .. import cache
from .base import Malformed, SourceResult
from .http import request_text

BASE = "https://understat.com"
SOURCE = "understat"
HEADERS = {
    "User-Agent": "fpl-squad-assistant/2.0 (personal, local use)",
    "Accept-Language": "en-GB,en;q=0.9",
}

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


class UnderstatSource:
    """Cached, rate-limited, non-raising Understat client."""

    def __init__(self, conn: sqlite3.Connection, session: requests.Session | None = None,
                 enabled: bool = True):
        self.conn = conn
        self.enabled = enabled
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def _page(self, path: str, variable: str, key: str, tier: str) -> SourceResult:
        if not self.enabled:
            return SourceResult.unavailable(SOURCE, "disabled by configuration")

        def fetch():
            html = request_text(self.conn, self.session, f"{BASE}/{path}", SOURCE)
            return extract(html, variable)

        return cache.get_or_revalidate(
            self.conn, key=key, tier=tier, fetch_fn=fetch, source=SOURCE
        )

    # -- endpoints ---------------------------------------------------------
    def league_players(self, season: int) -> SourceResult:
        """Season aggregates for every player. ONE request covers the league."""
        return self._page(f"league/EPL/{season}", "playersData",
                          f"us:league:{season}:players", "understat_league")

    def league_teams(self, season: int) -> SourceResult:
        """Team-level xG/xGA, feeding the opponent adjustment."""
        return self._page(f"league/EPL/{season}", "teamsData",
                          f"us:league:{season}:teams", "understat_league")

    def player_matches(self, understat_id: str) -> SourceResult:
        """Per-match history for one player."""
        return self._page(f"player/{understat_id}", "matchesData",
                          f"us:player:{understat_id}:matches", "understat_player")

    def player_groups(self, understat_id: str) -> SourceResult:
        """Season/position/situation splits for one player."""
        return self._page(f"player/{understat_id}", "groupsData",
                          f"us:player:{understat_id}:groups", "understat_player")

    def match_shots(self, match_id: str) -> SourceResult:
        """Shot-level detail. Frozen tier: a finished match never changes."""
        return self._page(f"match/{match_id}", "shotsData",
                          f"us:match:{match_id}:shots", "understat_match")
