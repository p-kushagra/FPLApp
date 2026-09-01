"""Shared fixtures.

Every test runs against a real SQLite file built by the real `init_db`, not a
mock. The migration ladder is part of what is under test, and an in-memory
schema that drifts from the shipped one is worse than no test at all.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from fpl_assistant import db as db_module


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.sqlite"


@pytest.fixture
def db(db_path) -> sqlite3.Connection:
    """A fully migrated schema-v2 database."""
    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def clock():
    """A controllable UTC clock for TTL arithmetic.

    Cache expiry is pure arithmetic over timestamps, so tests advance this
    rather than sleeping -- a 24-hour TTL test must not take 24 hours, and a
    60-second one must not be flaky at 59.9.
    """

    class Clock:
        def __init__(self):
            self.now = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

        def advance(self, seconds: float) -> dt.datetime:
            self.now += dt.timedelta(seconds=seconds)
            return self.now

        def __call__(self) -> dt.datetime:
            return self.now

    return Clock()


@pytest.fixture
def no_sleep(monkeypatch):
    """Make backoff instantaneous and record what was slept."""
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("time.sleep", fake_sleep)
    return slept


class FakeResponse:
    """Minimal requests.Response stand-in for fault injection."""

    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        if self._json is None:
            raise ValueError("no JSON in response")
        return self._json


class FakeSession:
    """Scripted session. Each call pops the next queued response or raises it."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[str] = []
        self.headers: dict = {}

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        if not self.responses:
            return FakeResponse(200, json_data={})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def fake_session():
    return FakeSession
