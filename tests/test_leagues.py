"""Mini-league discovery and rival selection.

The regression this file exists to prevent is the one that shipped: every ILEO
table, view-model and page was built and correct, but nothing ever *produced* a
league id, so all of it rendered an empty state forever and no test noticed --
because each individual piece passed on hand-supplied ids.
"""
from __future__ import annotations

import pytest

from fpl_assistant import leagues as leagues_mod
from fpl_assistant.jobs import tasks
from fpl_assistant.sources.base import Quality, SourceResult


def _entry_payload(*leagues: dict) -> dict:
    return {"id": 12345, "name": "My Team", "leagues": {"classic": list(leagues)}}


def _league(lid: int, name: str, league_type: str = "x", rank: int = 3) -> dict:
    return {"id": lid, "name": name, "league_type": league_type,
            "entry_rank": rank, "entry_last_rank": rank + 1}


@pytest.fixture
def conn(db):
    return db


@pytest.fixture
def fake_entry(monkeypatch):
    """Stub `/entry/{id}/` so discovery never touches the network."""
    def install(payload, quality=Quality.FRESH):
        def entry(self, team_id):
            return SourceResult(data=payload, quality=quality, source="fpl",
                                fetched_at="now", age_seconds=0)
        monkeypatch.setattr("fpl_assistant.sources.fpl.FplSource.entry", entry)
    return install


class TestDiscovery:
    def test_private_leagues_are_tracked_by_default(self, conn, fake_entry):
        fake_entry(_entry_payload(_league(101, "Work league")))
        result = leagues_mod.discover(conn, 12345)

        assert result["ok"] and result["leagues"] == 1
        assert leagues_mod.tracked_ids(conn) == [101]

    def test_general_leagues_are_listed_but_not_tracked(self, conn, fake_entry):
        """Overall has 7m entries; its ILEO is just global EO with extra steps."""
        fake_entry(_entry_payload(
            _league(314, "Overall", league_type="s", rank=900_000),
            _league(276, "England", league_type="s"),
            _league(101, "Work league")))
        leagues_mod.discover(conn, 12345)

        assert len(leagues_mod.all_leagues(conn)) == 3
        assert leagues_mod.tracked_ids(conn) == [101]

    def test_a_user_tracking_choice_survives_rediscovery(self, conn, fake_entry):
        """Refreshing memberships must not silently undo an opt-in."""
        fake_entry(_entry_payload(_league(314, "Overall", league_type="s")))
        leagues_mod.discover(conn, 12345)
        leagues_mod.set_tracked(conn, 314, True)

        leagues_mod.discover(conn, 12345)
        assert leagues_mod.tracked_ids(conn) == [314]

    def test_rediscovery_refreshes_rank(self, conn, fake_entry):
        fake_entry(_entry_payload(_league(101, "Work league", rank=5)))
        leagues_mod.discover(conn, 12345)
        fake_entry(_entry_payload(_league(101, "Work league", rank=2)))
        leagues_mod.discover(conn, 12345)

        assert leagues_mod.all_leagues(conn)[0]["my_rank"] == 2

    def test_no_team_id_is_reported_not_raised(self, conn):
        result = leagues_mod.discover(conn, 0)
        assert result["ok"] is False and "FPL_TEAM_ID" in result["reason"]

    def test_an_unavailable_entry_degrades(self, conn, fake_entry):
        fake_entry(None, quality=Quality.UNAVAILABLE)
        assert leagues_mod.discover(conn, 12345)["ok"] is False


class TestRivalSelection:
    @pytest.fixture
    def league_with_standings(self, conn):
        conn.execute("INSERT INTO league (league_id, name, league_type, tracked)"
                     " VALUES (101, 'Work league', 'x', 1)")
        for rank, entry_id in enumerate([900, 901, 12345, 902, 903], start=1):
            conn.execute(
                """INSERT INTO league_standing
                     (league_id, gw, entry_id, player_name, rank, total)
                   VALUES (101, 3, ?, ?, ?, ?)""",
                (entry_id, f"Manager {entry_id}", rank, 200 - rank))
        conn.commit()
        return conn

    def test_auto_selection_takes_the_top_by_rank(self, league_with_standings):
        chosen = leagues_mod.auto_select_rivals(
            league_with_standings, 101, count=3)
        assert chosen == [900, 901, 12345]

    def test_auto_selection_never_makes_you_your_own_rival(
            self, league_with_standings):
        chosen = leagues_mod.auto_select_rivals(
            league_with_standings, 101, count=3, exclude_entry=12345)
        assert 12345 not in chosen
        assert chosen == [900, 901, 902]

    def test_saving_a_rival_set_replaces_the_previous_one(
            self, league_with_standings):
        leagues_mod.set_rivals(league_with_standings, 101, [900, 901])
        leagues_mod.set_rivals(league_with_standings, 101, [903])
        assert leagues_mod.rival_ids(league_with_standings, 101) == [903]

    def test_rivals_span_tracked_leagues_when_none_is_named(
            self, league_with_standings):
        leagues_mod.set_rivals(league_with_standings, 101, [900, 903])
        assert sorted(leagues_mod.rival_ids(league_with_standings)) == [900, 903]

    def test_untracked_leagues_contribute_no_rivals(self, league_with_standings):
        leagues_mod.set_rivals(league_with_standings, 101, [900])
        leagues_mod.set_tracked(league_with_standings, 101, False)
        assert leagues_mod.rival_ids(league_with_standings) == []

    def test_ensure_rivals_keeps_an_existing_curated_set(
            self, league_with_standings):
        leagues_mod.set_rivals(league_with_standings, 101, [903])
        assert leagues_mod.ensure_rivals(league_with_standings, 101) == [903]

    def test_ensure_rivals_fills_an_empty_set(self, league_with_standings):
        assert leagues_mod.ensure_rivals(
            league_with_standings, 101, count=2, exclude_entry=12345) == [900, 901]


class TestStandingsIngest:
    def test_a_saved_rival_flag_survives_a_standings_refresh(
            self, db, monkeypatch):
        """Re-ingesting standings weekly must not wipe the rival selection."""
        db.execute("INSERT INTO league (league_id, name, league_type, tracked)"
                   " VALUES (101, 'Work league', 'x', 1)")
        db.execute("INSERT INTO league_standing (league_id, gw, entry_id, rank,"
                   " is_rival) VALUES (101, 3, 900, 1, 1)")
        db.commit()

        def entries(self, league_id, limit=50):
            return SourceResult(
                data=[{"entry": 900, "player_name": "A", "entry_name": "AA",
                       "rank": 1, "last_rank": 2, "event_total": 60,
                       "total": 200}],
                quality=Quality.FRESH, source="fpl_league",
                fetched_at="now", age_seconds=0)

        monkeypatch.setattr(
            "fpl_assistant.sources.fpl.FplSource.league_entries", entries)

        result = tasks.ingest_mini_league(db)
        assert result["ok"] and result["leagues"] == 1
        assert leagues_mod.rival_ids(db, 101) == [900]

    def test_no_tracked_leagues_is_reported_not_raised(self, db):
        assert tasks.ingest_mini_league(db)["ok"] is False


class TestJobRegistration:
    def test_discovery_is_a_registered_job(self):
        assert "discover_leagues" in tasks.REGISTRY

    def test_the_league_jobs_the_daemon_calls_all_exist(self):
        """The daemon runs these by name; a typo would fail only at runtime."""
        for name in ("discover_leagues", "ingest_mini_league", "freeze_rivals"):
            assert callable(tasks.REGISTRY[name])
