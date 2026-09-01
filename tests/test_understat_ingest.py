"""Understat ingest after the AJAX migration.

The per-match payload lost its `h_a` flag when the site moved off inline HTML.
Nothing raises when that is mishandled -- every match simply records as an away
game for the home team, which is the class of silent wrongness ADR-004 exists to
prevent.
"""
from __future__ import annotations

import pytest

from fpl_assistant.jobs import tasks
from fpl_assistant.sources.base import Quality, SourceResult


def _payload(matches, groups, shots=None):
    return {"player": {"id": "8260"}, "matches": matches,
            "groups": groups, "shots": shots or []}


SHOT = {
    "id": "354876", "minute": "58", "result": "Goal", "X": "0.888",
    "Y": "0.666", "xG": "0.0793", "player": "Erling Haaland", "h_a": "a",
    "player_id": "8260", "situation": "OpenPlay", "season": "2026",
    "shotType": "LeftFoot", "match_id": "12562", "h_team": "Augsburg",
    "a_team": "Borussia Dortmund", "lastAction": "Pass",
    "player_assisted": "Jadon Sancho", "date": "2026-01-18 14:30:00",
}


def _result(data):
    return SourceResult(data=data, quality=Quality.FRESH, source="understat",
                        fetched_at=None, age_seconds=0.0)


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)


class TestTeamsBySeason:
    def test_maps_each_season_to_its_club(self):
        groups = {"season": [{"season": "2025", "team": "Borussia Dortmund"},
                             {"season": "2026", "team": "Manchester City"}]}
        assert tasks._teams_by_season(groups) == {
            2025: {"Borussia Dortmund"}, 2026: {"Manchester City"}}

    def test_a_midseason_transfer_keeps_both_clubs(self):
        groups = {"season": [{"season": "2026", "team": "Chelsea"},
                             {"season": "2026", "team": "Arsenal"}]}
        assert tasks._teams_by_season(groups) == {2026: {"Chelsea", "Arsenal"}}

    def test_missing_groups_is_empty_not_an_error(self):
        assert tasks._teams_by_season(None) == {}
        assert tasks._teams_by_season({}) == {}


class TestHomeAwayDerivation:
    def _run(self, db, monkeypatch, matches, groups):
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_data",
            lambda self, uid: _result(_payload(matches, groups)))
        result = tasks.understat_fanout(db, understat_ids=["8260"])
        assert result["ok"]
        return [dict(r) for r in db.execute(
            "SELECT * FROM understat_player_match ORDER BY match_id")]

    GROUPS = {"season": [{"season": "2026", "team": "Manchester City"}]}

    def test_away_match_is_recorded_as_away(self, db, monkeypatch):
        rows = self._run(db, monkeypatch, [{
            "id": "1", "season": "2026", "h_team": "Crystal Palace",
            "a_team": "Manchester City", "time": "90", "xG": "0.5"}], self.GROUPS)
        assert rows[0]["is_home"] == 0
        assert rows[0]["team_title"] == "Manchester City"
        assert rows[0]["opponent_title"] == "Crystal Palace"

    def test_home_match_is_recorded_as_home(self, db, monkeypatch):
        rows = self._run(db, monkeypatch, [{
            "id": "2", "season": "2026", "h_team": "Manchester City",
            "a_team": "Bournemouth", "time": "90", "xG": "0.7"}], self.GROUPS)
        assert rows[0]["is_home"] == 1
        assert rows[0]["team_title"] == "Manchester City"
        assert rows[0]["opponent_title"] == "Bournemouth"

    def test_the_players_own_club_is_stored_not_the_home_club(
            self, db, monkeypatch):
        """The pre-migration code stored h_team as team_title unconditionally,
        so every away appearance was attributed to the opposition."""
        rows = self._run(db, monkeypatch, [{
            "id": "3", "season": "2026", "h_team": "Liverpool",
            "a_team": "Manchester City", "time": "90"}], self.GROUPS)
        assert rows[0]["team_title"] != "Liverpool"

    def test_an_unresolvable_side_is_null_not_a_guess(self, db, monkeypatch):
        """Both clubs in one season: recording either side would be invented."""
        rows = self._run(db, monkeypatch, [{
            "id": "4", "season": "2026", "h_team": "Chelsea",
            "a_team": "Arsenal", "time": "90"}],
            {"season": [{"season": "2026", "team": "Chelsea"},
                        {"season": "2026", "team": "Arsenal"}]})
        assert rows[0]["is_home"] is None
        assert rows[0]["team_title"] is None

    def test_a_season_with_no_group_row_is_null(self, db, monkeypatch):
        rows = self._run(db, monkeypatch, [{
            "id": "5", "season": "2019", "h_team": "Augsburg",
            "a_team": "Borussia Dortmund", "time": "60"}], self.GROUPS)
        assert rows[0]["is_home"] is None

    def test_metrics_survive_the_round_trip(self, db, monkeypatch):
        rows = self._run(db, monkeypatch, [{
            "id": "6", "season": "2026", "h_team": "Manchester City",
            "a_team": "Bournemouth", "time": "90", "goals": "2",
            "npxG": "0.75", "xA": "0.11", "xGChain": "0.95"}], self.GROUPS)
        assert rows[0]["minutes"] == 90
        assert rows[0]["goals"] == 2
        assert rows[0]["npxg"] == pytest.approx(0.75)
        assert rows[0]["xa"] == pytest.approx(0.11)


class TestShotPersistence:
    def _run(self, db, monkeypatch, shots):
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_data",
            lambda self, uid: _result(_payload([], {}, shots)))
        return tasks.understat_fanout(db, understat_ids=["8260"])

    def test_shots_are_stored_from_the_same_payload(self, db, monkeypatch):
        """No extra request: the fan-out already holds the shot array."""
        assert self._run(db, monkeypatch, [SHOT])["shots"] == 1
        row = dict(db.execute("SELECT * FROM understat_shot").fetchone())
        assert row["shot_id"] == "354876"
        assert row["understat_id"] == "8260"
        assert row["x"] == pytest.approx(0.888)
        assert row["y"] == pytest.approx(0.666)
        assert row["xg"] == pytest.approx(0.0793)
        assert row["result"] == "Goal"
        assert row["situation"] == "OpenPlay"
        assert row["shot_type"] == "LeftFoot"
        assert row["h_a"] == "a"
        assert row["season"] == 2026

    def test_reingest_replaces_rather_than_duplicates(self, db, monkeypatch):
        """Understat returns a whole career every time it is asked."""
        self._run(db, monkeypatch, [SHOT])
        self._run(db, monkeypatch, [SHOT])
        assert db.execute(
            "SELECT COUNT(*) c FROM understat_shot").fetchone()["c"] == 1

    def test_a_shot_without_an_id_is_skipped_not_crashed(self, db, monkeypatch):
        result = self._run(db, monkeypatch, [{"X": "0.5"}, SHOT])
        assert result["shots"] == 1

    def test_no_shots_is_a_clean_zero(self, db, monkeypatch):
        assert self._run(db, monkeypatch, [])["shots"] == 0

    def test_missing_shots_key_does_not_fail_the_player(self, db, monkeypatch):
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_data",
            lambda self, uid: _result({"matches": [], "groups": {}}))
        out = tasks.understat_fanout(db, understat_ids=["8260"])
        assert out["ok"] and out["shots"] == 0 and out["failed"] == []


class TestFanoutResilience:
    def test_an_unavailable_player_is_counted_not_raised(self, db, monkeypatch):
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_data",
            lambda self, uid: SourceResult.unavailable("understat", "boom"))
        result = tasks.understat_fanout(db, understat_ids=["1", "2"])
        assert result["ok"] and result["failed"] == ["1", "2"]
        assert result["matches"] == 0

    def test_a_total_failure_flags_the_source_offline(self, db, monkeypatch):
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_data",
            lambda self, uid: SourceResult.unavailable("understat", "boom"))
        tasks.understat_fanout(db, understat_ids=["1"])
        assert tasks.understat_offline(db)

    def test_there_is_a_pause_between_players(self, db, monkeypatch):
        """Enrichment must not hammer a small volunteer-run site."""
        slept: list[float] = []
        monkeypatch.setattr(tasks.time, "sleep", slept.append)
        monkeypatch.setattr(
            "fpl_assistant.sources.understat.UnderstatSource.player_data",
            lambda self, uid: _result(_payload([], {})))
        tasks.understat_fanout(db, understat_ids=["1", "2", "3"])
        assert slept == [tasks.PLAYER_FETCH_PAUSE] * 2, (
            "one pause between each pair, none before the first")

    def test_no_ids_is_a_clean_noop(self, db):
        assert tasks.understat_fanout(db, understat_ids=[])["matches"] == 0
