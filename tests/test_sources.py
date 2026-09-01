"""Source adapters: rate limiting, HTTP fault injection, Understat extraction.

Covers the fallback and error-handling wrappers -- the promise that a page can
always render something, whatever the network did.
"""
from __future__ import annotations

import datetime as dt

import pytest
import requests

from fpl_assistant.sources import http, ratelimit, understat
from fpl_assistant.sources.base import (
    Malformed,
    NotFound,
    Quality,
    RateLimited,
    SourceResult,
    Unavailable,
)
from fpl_assistant.sources.fpl import FplSource

from .conftest import FakeResponse, FakeSession

HOST = "fantasy.premierleague.com"


@pytest.fixture(autouse=True)
def _quiet_backoff(monkeypatch):
    """Backoff must not make the suite slow."""
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "_backoff", lambda attempt: 0.0)


# --------------------------------------------------------------------------
class TestTokenBucket:
    def test_burst_then_empty(self, db):
        cap = int(ratelimit.bucket_for(HOST).capacity)
        assert all(ratelimit.try_acquire(db, HOST) for _ in range(cap))
        assert not ratelimit.try_acquire(db, HOST), "bucket should be drained"

    def test_refills_over_time(self, db, clock):
        cap = int(ratelimit.bucket_for(HOST).capacity)
        for _ in range(cap):
            ratelimit.try_acquire(db, HOST, now=clock())
        assert not ratelimit.try_acquire(db, HOST, now=clock())
        # 60/min -> one token per second
        clock.advance(2)
        assert ratelimit.try_acquire(db, HOST, now=clock())

    def test_never_refills_past_capacity(self, db, clock):
        spec = ratelimit.bucket_for(HOST)
        ratelimit.try_acquire(db, HOST, now=clock())
        clock.advance(10_000)
        ratelimit.try_acquire(db, HOST, now=clock())
        assert ratelimit.budget_state(db, HOST)["tokens"] <= spec.capacity

    def test_state_survives_a_reconnect(self, db_path):
        """Streamlit restarts constantly; the budget must not reset with it."""
        from fpl_assistant import db as db_module
        db_module.init_db(db_path)

        conn = db_module.connect(db_path)
        for _ in range(int(ratelimit.bucket_for(HOST).capacity)):
            ratelimit.try_acquire(conn, HOST)
        conn.close()

        conn2 = db_module.connect(db_path)
        assert not ratelimit.try_acquire(conn2, HOST), "budget reset on reconnect"
        conn2.close()

    def test_acquire_gives_up_rather_than_blocking_forever(self, db):
        for _ in range(int(ratelimit.bucket_for(HOST).capacity)):
            ratelimit.try_acquire(db, HOST)
        slept: list[float] = []
        got = ratelimit.acquire(db, HOST, max_wait=0.0, sleep=slept.append)
        assert got is False

    def test_429_halves_the_bucket(self, db):
        before = ratelimit.budget_state(db, HOST)["tokens"]
        ratelimit.record_429(db, HOST)
        after = ratelimit.budget_state(db, HOST)
        assert after["tokens"] <= before / 2 + 0.01
        assert after["total_429"] == 1

    def test_hosts_have_independent_budgets(self, db):
        for _ in range(int(ratelimit.bucket_for(HOST).capacity)):
            ratelimit.try_acquire(db, HOST)
        assert ratelimit.try_acquire(db, "understat.com")

    def test_understat_is_throttled_harder_than_fpl(self):
        assert (ratelimit.bucket_for("understat.com").refill_per_sec
                < ratelimit.bucket_for(HOST).refill_per_sec)


# --------------------------------------------------------------------------
class TestHttpFaultInjection:
    def _call(self, db, session):
        return http.request_json(db, session, f"https://{HOST}/api/x", "fpl",
                                 sleep=lambda s: None)

    def test_success(self, db):
        session = FakeSession([FakeResponse(200, json_data={"ok": True})])
        assert self._call(db, session) == {"ok": True}

    def test_429_retries_then_raises_rate_limited(self, db):
        session = FakeSession([FakeResponse(429)] * 3)
        with pytest.raises(RateLimited):
            self._call(db, session)
        assert len(session.calls) == 3, "should exhaust MAX_ATTEMPTS"

    def test_429_then_success_recovers(self, db):
        session = FakeSession([FakeResponse(429),
                               FakeResponse(200, json_data={"ok": True})])
        assert self._call(db, session) == {"ok": True}

    def test_honours_retry_after(self, db):
        slept: list[float] = []
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}),
                               FakeResponse(200, json_data={})])
        http.request_json(db, session, f"https://{HOST}/api/x", "fpl",
                          sleep=slept.append)
        assert 7.0 in slept

    def test_500_raises_unavailable(self, db):
        session = FakeSession([FakeResponse(500)] * 3)
        with pytest.raises(Unavailable):
            self._call(db, session)

    def test_404_is_not_retried(self, db):
        session = FakeSession([FakeResponse(404)])
        with pytest.raises(NotFound):
            self._call(db, session)
        assert len(session.calls) == 1

    def test_timeout_raises_unavailable(self, db):
        session = FakeSession([requests.Timeout()] * 3)
        with pytest.raises(Unavailable):
            self._call(db, session)

    def test_connection_error_raises_unavailable(self, db):
        session = FakeSession([requests.ConnectionError("dns")] * 3)
        with pytest.raises(Unavailable):
            self._call(db, session)

    def test_non_json_body_raises_malformed(self, db):
        session = FakeSession([FakeResponse(200, json_data=None, text="<html>")])
        with pytest.raises(Malformed):
            self._call(db, session)

    def test_health_records_success_and_failure(self, db):
        http.record_health(db, "fpl", ok=True, latency_ms=120)
        row = db.execute("SELECT * FROM source_health WHERE source='fpl'").fetchone()
        assert row["quality"] == "ok"

        for _ in range(3):
            http.record_health(db, "fpl", ok=False, error="boom")
        row = db.execute("SELECT * FROM source_health WHERE source='fpl'").fetchone()
        assert row["quality"] == "down"
        assert row["consecutive_failures"] == 3

    def test_success_resets_the_failure_streak(self, db):
        for _ in range(3):
            http.record_health(db, "fpl", ok=False, error="boom")
        http.record_health(db, "fpl", ok=True, latency_ms=50)
        row = db.execute("SELECT * FROM source_health WHERE source='fpl'").fetchone()
        assert row["consecutive_failures"] == 0
        assert row["quality"] == "ok"


# --------------------------------------------------------------------------
class TestFplSourceDegrades:
    def test_returns_result_not_exception_on_total_failure(self, db):
        src = FplSource(db, FakeSession([FakeResponse(500)] * 9))
        res = src.bootstrap()
        assert isinstance(res, SourceResult)
        assert res.quality is Quality.UNAVAILABLE
        assert res.unwrap(default={}) == {}

    def test_serves_cache_when_upstream_dies(self, db):
        good = FplSource(db, FakeSession([FakeResponse(200, json_data={"elements": [1]})]))
        assert good.bootstrap().quality is Quality.FRESH

        db.execute("UPDATE cache_entry SET soft_expires_at = ?, hard_expires_at = ?",
                   ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"))
        db.commit()

        bad = FplSource(db, FakeSession([FakeResponse(500)] * 9))
        res = bad.bootstrap()
        assert res.quality is Quality.DEGRADED
        assert res.data == {"elements": [1]}
        assert res.badge() and "cached" in res.badge()

    def test_frozen_picks_use_the_write_once_tier(self, db):
        src = FplSource(db, FakeSession([FakeResponse(200, json_data={"picks": [1]})]))
        src.picks(team_id=99, gw=14, frozen=True)
        row = db.execute(
            "SELECT tier, frozen FROM cache_entry WHERE cache_key='fpl:picks:99:14'"
        ).fetchone()
        assert row["tier"] == "ml_picks"
        assert row["frozen"] == 1

    def test_partial_league_walk_returns_what_it_got(self, db):
        page1 = FakeResponse(200, json_data={
            "standings": {"results": [{"entry": 1}, {"entry": 2}], "has_next": True}})
        src = FplSource(db, FakeSession([page1] + [FakeResponse(500)] * 9))
        res = src.league_entries(league_id=123, limit=50)
        assert len(res.data) == 2, "keep the page we did get"
        assert res.quality.is_degraded, "but say the set is incomplete"


# --------------------------------------------------------------------------
class TestUnderstatExtraction:
    HTML = (
        "<html><script>\n"
        "var playersData = JSON.parse('\\x5B\\x7B\\x22id\\x22\\x3A\\x22100\\x22"
        "\\x2C\\x22player_name\\x22\\x3A\\x22Erling Haaland\\x22\\x7D\\x5D');\n"
        "var teamsData = JSON.parse('\\x7B\\x22a\\x22\\x3A1\\x7D');\n"
        "</script></html>"
    )

    def test_extracts_and_decodes(self):
        data = understat.extract(self.HTML, "playersData")
        assert data == [{"id": "100", "player_name": "Erling Haaland"}]

    def test_picks_the_named_variable(self):
        assert understat.extract(self.HTML, "teamsData") == {"a": 1}

    def test_missing_variable_raises_malformed(self):
        """A markup change must fail loudly, never return empty stats."""
        with pytest.raises(Malformed, match="not found"):
            understat.extract(self.HTML, "shotsData")

    def test_error_names_what_it_did_find(self):
        with pytest.raises(Malformed) as exc:
            understat.extract(self.HTML, "shotsData")
        assert "playersData" in str(exc.value)

    def test_empty_body_raises_malformed(self):
        with pytest.raises(Malformed):
            understat.extract("", "playersData")

    def test_undecodable_payload_raises_malformed(self):
        bad = "var playersData = JSON.parse('{not valid json}');"
        with pytest.raises(Malformed):
            understat.extract(bad, "playersData")

    def test_available_variables_lists_them(self):
        assert understat.available_variables(self.HTML) == ["playersData", "teamsData"]

    def test_disabled_source_reports_unavailable_without_a_request(self, db):
        session = FakeSession()
        src = understat.UnderstatSource(db, session, enabled=False)
        res = src.league_players(2025)
        assert res.quality is Quality.UNAVAILABLE
        assert session.calls == [], "must not hit the network when disabled"

    def test_markup_change_degrades_rather_than_crashes(self, db):
        """The R1 scenario end to end."""
        session = FakeSession([FakeResponse(200, text="<html>redesigned</html>")] * 3)
        src = understat.UnderstatSource(db, session)
        res = src.league_players(2025)
        assert res.quality is Quality.UNAVAILABLE
        assert res.data is None

    def test_successful_scrape_is_cached(self, db):
        session = FakeSession([FakeResponse(200, text=self.HTML)])
        src = understat.UnderstatSource(db, session)
        assert src.league_players(2025).quality is Quality.FRESH
        assert src.league_players(2025).quality is Quality.FRESH
        assert len(session.calls) == 1, "second read must come from cache"


# --------------------------------------------------------------------------
class TestQualitySemantics:
    def test_severity_orders_by_badness_not_alphabet(self):
        assert (Quality.FRESH.severity < Quality.STALE.severity
                < Quality.DEGRADED.severity < Quality.UNAVAILABLE.severity)

    def test_only_bad_states_ask_for_a_badge(self):
        assert SourceResult(None, Quality.FRESH, "x").badge() is None
        assert SourceResult(None, Quality.DEGRADED, "understat").badge()
        assert SourceResult(None, Quality.UNAVAILABLE, "understat").badge()

    def test_unwrap_returns_default_when_empty(self):
        assert SourceResult.unavailable("x", "err").unwrap(default=[]) == []

    def test_ok_helper_is_fresh_and_timestamped(self):
        res = SourceResult.ok({"a": 1}, "fpl")
        assert res.quality is Quality.FRESH
        assert isinstance(res.fetched_at, dt.datetime)
