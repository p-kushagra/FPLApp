"""Thin, polite client for the public FPL API (no auth, rate-limited)."""
from __future__ import annotations

import time

import requests

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "fpl-squad-assistant/1.0 (personal, local use)"}


class FplClient:
    def __init__(self, min_interval: float = 1.0):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.min_interval = min_interval
        self._last = 0.0

    def _get(self, path: str) -> dict:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(f"{BASE}/{path}", timeout=30)
        self._last = time.time()
        resp.raise_for_status()
        return resp.json()

    def bootstrap(self) -> dict:
        return self._get("bootstrap-static/")

    def fixtures(self) -> list:
        return self._get("fixtures/")

    def entry(self, team_id: int) -> dict:
        return self._get(f"entry/{team_id}/")

    def picks(self, team_id: int, gw: int) -> dict:
        return self._get(f"entry/{team_id}/event/{gw}/picks/")

    def element_summary(self, player_id: int) -> dict:
        return self._get(f"element-summary/{player_id}/")

    def league_standings(self, league_id: int, page: int = 1) -> dict:
        return self._get(f"leagues-classic/{league_id}/standings/?page_standings={page}")

    def live(self, gw: int) -> dict:
        return self._get(f"event/{gw}/live/")
