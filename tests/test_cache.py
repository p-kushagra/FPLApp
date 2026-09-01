"""SWR cache: the six states, and the promise that a read never raises.

The contract under test (design doc 5.2):

    MISS      -> blocking fetch, FRESH
    FRESH     -> serve cached
    STALE     -> serve cached NOW + request revalidation
    EXPIRED   -> blocking fetch
    fail+cache-> DEGRADED (serve whatever we have, however old)
    fail+none -> UNAVAILABLE (consumer falls back)
"""
from __future__ import annotations

import datetime as dt

import pytest

from fpl_assistant import cache
from fpl_assistant.cache import store, tiers
from fpl_assistant.sources.base import Malformed, Quality, Unavailable

TIER = "fpl_static"          # soft 24h, hard 72h
LIVE = "fpl_live"            # soft 60s, hard 5m
FROZEN = "ml_picks"          # write-once


@pytest.fixture(autouse=True)
def _no_revalidator():
    """Each test installs its own hook; never leak one between tests."""
    cache.clear_revalidator()
    yield
    cache.clear_revalidator()


def _counter(payload=None, fail=None):
    """A fetch_fn that counts calls, so 'did it refetch?' is observable."""
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if fail is not None:
            raise fail
        return payload if payload is not None else {"v": calls["n"]}

    fetch.calls = calls
    return fetch


class TestFreshnessStates:
    def test_miss_fetches_and_returns_fresh(self, db, clock):
        fetch = _counter({"hello": "world"})
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.FRESH
        assert res.data == {"hello": "world"}
        assert fetch.calls["n"] == 1

    def test_second_read_inside_soft_ttl_serves_cache(self, db, clock):
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        clock.advance(3600)  # 1h, well inside the 24h soft TTL
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.FRESH
        assert fetch.calls["n"] == 1, "must not refetch inside soft TTL"
        assert res.age_seconds == pytest.approx(3600, abs=1)

    def test_past_soft_ttl_serves_stale_without_blocking(self, db, clock):
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        clock.advance(tiers.TIERS[TIER].soft_ttl + 1)
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.STALE
        assert res.data is not None
        assert fetch.calls["n"] == 1, "STALE must serve immediately, not refetch"

    def test_stale_read_requests_revalidation_exactly_once(self, db, clock):
        seen = []
        cache.set_revalidator(lambda key, tier: seen.append((key, tier)))
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        clock.advance(tiers.TIERS[TIER].soft_ttl + 1)
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert seen == [("k", TIER)]

    def test_past_hard_ttl_blocks_and_refetches(self, db, clock):
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        clock.advance(tiers.TIERS[TIER].hard_ttl + 1)
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.FRESH
        assert fetch.calls["n"] == 2

    def test_force_bypasses_freshness(self, db, clock):
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        res = cache.get_or_revalidate(db, "k", TIER, fetch, force=True, now=clock())
        assert fetch.calls["n"] == 2
        assert res.quality is Quality.FRESH

    def test_live_tier_expires_in_60_seconds(self, db, clock):
        """The live tier is the one where an off-by-one TTL is visible."""
        fetch = _counter()
        cache.get_or_revalidate(db, "live", LIVE, fetch, now=clock())
        clock.advance(59)
        assert cache.get_or_revalidate(db, "live", LIVE, fetch,
                                       now=clock()).quality is Quality.FRESH
        clock.advance(2)  # 61s total
        assert cache.get_or_revalidate(db, "live", LIVE, fetch,
                                       now=clock()).quality is Quality.STALE


class TestDegradation:
    def test_fetch_failure_with_cache_serves_degraded(self, db, clock):
        cache.get_or_revalidate(db, "k", TIER, _counter({"good": 1}), now=clock())
        clock.advance(tiers.TIERS[TIER].hard_ttl + 1)
        failing = _counter(fail=Unavailable("upstream 503"))
        res = cache.get_or_revalidate(db, "k", TIER, failing, now=clock())
        assert res.quality is Quality.DEGRADED
        assert res.data == {"good": 1}, "must serve the old value"
        assert "503" in (res.error or "")

    def test_fetch_failure_without_cache_is_unavailable(self, db, clock):
        failing = _counter(fail=Unavailable("upstream 503"))
        res = cache.get_or_revalidate(db, "k", TIER, failing, now=clock())
        assert res.quality is Quality.UNAVAILABLE
        assert res.data is None
        assert res.unwrap(default={}) == {}

    @pytest.mark.parametrize("boom", [
        Unavailable("timeout"),
        Malformed("markup changed"),
        ValueError("something totally unexpected"),
        KeyError("missing"),
        RuntimeError("boom"),
    ])
    def test_no_exception_ever_escapes(self, db, clock, boom):
        """The load-bearing guarantee: a cache read never raises."""
        res = cache.get_or_revalidate(db, "k", TIER, _counter(fail=boom), now=clock())
        assert res.quality is Quality.UNAVAILABLE
        assert res.error

    def test_revalidator_failure_does_not_break_the_read(self, db, clock):
        def broken(key, tier):
            raise RuntimeError("job queue is down")

        cache.set_revalidator(broken)
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        clock.advance(tiers.TIERS[TIER].soft_ttl + 1)
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.STALE
        assert res.data is not None

    def test_stale_read_works_with_no_revalidator_installed(self, db, clock):
        """Phase 1 ships no job runner; STALE must still serve."""
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        clock.advance(tiers.TIERS[TIER].soft_ttl + 1)
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.STALE


class TestFrozenTier:
    def test_frozen_entry_never_expires(self, db, clock):
        fetch = _counter({"picks": [1, 2, 3]})
        cache.get_or_revalidate(db, "ml:picks:1:14", FROZEN, fetch, now=clock())
        clock.advance(365 * 24 * 3600)
        res = cache.get_or_revalidate(db, "ml:picks:1:14", FROZEN, fetch, now=clock())
        assert res.quality is Quality.FRESH
        assert fetch.calls["n"] == 1

    def test_frozen_entry_cannot_be_overwritten(self, db, clock):
        cache.get_or_revalidate(db, "f", FROZEN, _counter({"v": "first"}), now=clock())
        res = cache.get_or_revalidate(db, "f", FROZEN, _counter({"v": "second"}),
                                      force=True, now=clock())
        assert res.data == {"v": "first"}, "ADR-005: frozen means frozen"

    def test_store_write_itself_refuses_to_overwrite_frozen(self, db, clock):
        """Guard the WRITE path directly, not just the read path.

        get_or_revalidate short-circuits on a frozen record before it ever
        fetches, so it exercises the read-side guard only. A caller reaching
        store.write directly -- a backfill, a job, a future adapter -- must hit
        an independent guard, or immutability holds by accident.
        """
        tier = tiers.get_tier(FROZEN)
        store.write(db, "direct", tier, {"v": "first"}, now=clock())
        clock.advance(3600)
        store.write(db, "direct", tier, {"v": "second"}, now=clock())
        assert store.read(db, "direct").data == {"v": "first"}

    def test_non_frozen_tier_does_overwrite(self, db, clock):
        """The mirror of the above: normal tiers must still update."""
        tier = tiers.get_tier(TIER)
        store.write(db, "normal", tier, {"v": "first"}, now=clock())
        store.write(db, "normal", tier, {"v": "second"}, now=clock())
        assert store.read(db, "normal").data == {"v": "second"}

    def test_invalidate_skips_frozen_entries(self, db, clock):
        cache.get_or_revalidate(db, "ml:picks:1:14", FROZEN, _counter(), now=clock())
        cache.get_or_revalidate(db, "ml:standings:1", "ml_standings",
                                _counter(), now=clock())
        removed = cache.invalidate(db, "ml:")
        assert removed == 1
        assert store.read(db, "ml:picks:1:14") is not None


class TestStorage:
    def test_roundtrip_preserves_structure(self, db, clock):
        payload = {"a": [1, 2, {"b": None}], "c": "unicode: Håland ⚽", "d": 3.14}
        cache.get_or_revalidate(db, "k", TIER, _counter(payload), now=clock())
        assert store.read(db, "k").data == payload

    def test_payload_is_compressed(self, db, clock):
        payload = {"players": [{"id": i, "name": "x" * 40} for i in range(600)]}
        cache.get_or_revalidate(db, "big", TIER, _counter(payload), now=clock())
        stored = db.execute(
            "SELECT bytes FROM cache_entry WHERE cache_key = 'big'"
        ).fetchone()["bytes"]
        import json
        raw = len(json.dumps(payload).encode())
        assert stored < raw / 3, f"gzip should shrink this a lot ({stored} vs {raw})"

    def test_corrupt_payload_is_treated_as_a_miss(self, db, clock):
        cache.get_or_revalidate(db, "k", TIER, _counter(), now=clock())
        db.execute("UPDATE cache_entry SET payload = ? WHERE cache_key = 'k'",
                   (b"not gzip",))
        db.commit()
        assert store.read(db, "k") is None
        fetch = _counter({"recovered": True})
        res = cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert res.quality is Quality.FRESH
        assert res.data == {"recovered": True}

    def test_hits_are_counted(self, db, clock):
        fetch = _counter()
        cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        for _ in range(3):
            cache.get_or_revalidate(db, "k", TIER, fetch, now=clock())
        assert store.read(db, "k").hits == 3

    def test_purge_expired_leaves_live_entries(self, db, clock):
        cache.get_or_revalidate(db, "old", LIVE, _counter(), now=clock())
        clock.advance(tiers.TIERS[LIVE].hard_ttl + 1)
        cache.get_or_revalidate(db, "new", TIER, _counter(), now=clock())
        removed = store.purge_expired(db, now=clock())
        assert removed == 1
        assert store.read(db, "new") is not None

    def test_stats_report_totals(self, db, clock):
        cache.get_or_revalidate(db, "a", TIER, _counter(), now=clock())
        cache.get_or_revalidate(db, "b", LIVE, _counter(), now=clock())
        s = cache.stats(db)
        assert s["entries"] == 2
        assert s["bytes"] > 0
        assert set(s["by_tier"]) == {TIER, LIVE}


class TestTierDefinitions:
    def test_every_tier_is_internally_consistent(self):
        for name, tier in tiers.TIERS.items():
            assert tier.hard_ttl >= tier.soft_ttl, name
            assert tier.soft_ttl > 0, name

    def test_unknown_tier_fails_loudly(self):
        with pytest.raises(KeyError, match="unknown cache tier"):
            tiers.get_tier("no_such_tier")

    def test_inconsistent_tier_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="hard_ttl must be >= soft_ttl"):
            tiers.Tier("bad", soft_ttl=100, hard_ttl=10)

    def test_naive_timestamps_from_older_rows_still_parse(self, db, clock):
        """v1 rows and hand-edited rows may lack a timezone."""
        cache.get_or_revalidate(db, "k", TIER, _counter(), now=clock())
        db.execute(
            "UPDATE cache_entry SET fetched_at = ? WHERE cache_key = 'k'",
            (dt.datetime(2026, 1, 15, 12, 0, 0).isoformat(),),  # noqa: DTZ001 - naive on purpose
        )
        db.commit()
        record = store.read(db, "k")
        assert record is not None
        assert record.fetched_at.tzinfo is not None
