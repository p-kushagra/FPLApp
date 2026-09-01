"""Entity resolution: FPL player -> Understat player.

Written BEFORE the implementation (CLAUDE.md: test-driven validation). These
cases are the specification.

The failure this suite exists to prevent is risk R2: a *silent mis-binding*.
A player who fails to resolve is visible and harmless -- they fall back to FPL
baseline stats and the UI says so. A player bound to the WRONG Understat entity
silently attributes someone else's xG to them, and nothing ever looks wrong.
So the bar is asymmetric, and T_MARGIN below encodes it: refusing to guess is
always preferable to guessing.
"""
from __future__ import annotations

import pytest

from fpl_assistant.resolve import matcher

SEASON = 2025


# --------------------------------------------------------------------------
# Fixtures: a miniature two-club universe with every hard case in it
# --------------------------------------------------------------------------
@pytest.fixture
def fpl_players():
    return [
        # id, first, second, web_name, team short
        _p(1, "Erling", "Haaland", "Haaland", "MCI"),
        _p(2, "Rodrigo", "Hernandez Cascante", "Rodri", "MCI"),
        _p(3, "Bernardo", "Veiga de Carvalho e Silva", "Bernardo", "MCI"),
        _p(4, "Josko", "Gvardiol", "Gvardiol", "MCI"),
        _p(5, "Heung-Min", "Son", "Son", "TOT"),
        _p(6, "Cristian", "Romero", "Romero", "TOT"),
        _p(7, "Destiny", "Udogie", "Udogie", "TOT"),
    ]


@pytest.fixture
def understat_players():
    return [
        _u("100", "Erling Haaland", "Manchester City"),
        _u("101", "Rodri", "Manchester City"),
        _u("102", "Bernardo Silva", "Manchester City"),
        _u("103", "Josko Gvardiol", "Manchester City"),
        _u("200", "Son Heung-Min", "Tottenham"),
        _u("201", "Cristian Romero", "Tottenham"),
        _u("202", "Destiny Udogie", "Tottenham"),
    ]


def _p(pid, first, second, web, team):
    return {"id": pid, "first_name": first, "second_name": second,
            "web_name": web, "team_short": team}


def _u(uid, name, team):
    return {"id": uid, "player_name": name, "team_title": team}


TEAM_ALIASES = {"Manchester City": "MCI", "Tottenham": "TOT"}


def _resolve(fpl, understat, player_id, overrides=None):
    player = next(p for p in fpl if p["id"] == player_id)
    return matcher.resolve_one(
        player, understat, team_aliases=TEAM_ALIASES, overrides=overrides or {}
    )


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
class TestNormalise:
    @pytest.mark.parametrize("raw,expected", [
        ("Haaland", "haaland"),
        ("HAALAND", "haaland"),
        ("  Haaland  ", "haaland"),
        ("Håland", "haland"),                  # diacritic stripped
        ("Hernández", "hernandez"),
        ("Heung-Min", "heung min"),            # hyphen becomes a token break
        ("O'Brien", "obrien"),                 # apostrophe removed, not split
        ("Sánchez  Gómez", "sanchez gomez"),   # whitespace collapsed
        ("", ""),
        (None, ""),
    ])
    def test_normalise(self, raw, expected):
        assert matcher.normalise_name(raw) == expected

    def test_is_idempotent(self):
        once = matcher.normalise_name("Håland  O'Brien-Smith")
        assert matcher.normalise_name(once) == once


# --------------------------------------------------------------------------
# The resolution ladder, stage by stage
# --------------------------------------------------------------------------
class TestResolutionLadder:
    def test_manual_override_wins_outright(self, fpl_players, understat_players):
        """An override is trusted absolutely, even against a better fuzzy match."""
        res = _resolve(fpl_players, understat_players, 1, overrides={1: "999"})
        assert res.understat_id == "999"
        assert res.method == "manual"
        assert res.confidence == 1.0
        assert res.status == "resolved"

    def test_exact_full_name(self, fpl_players, understat_players):
        res = _resolve(fpl_players, understat_players, 1)
        assert (res.understat_id, res.method, res.confidence) == ("100", "exact", 1.0)

    def test_exact_on_web_name(self, fpl_players, understat_players):
        """Rodri: FPL full name is 'Rodrigo Hernandez Cascante', Understat 'Rodri'.

        Only the web_name matches, so web_name must be tried in the exact stage.
        """
        res = _resolve(fpl_players, understat_players, 2)
        assert (res.understat_id, res.method) == ("101", "exact")

    def test_token_set_handles_reversed_name_order(self, fpl_players, understat_players):
        """Son: FPL 'Heung-Min Son' vs Understat 'Son Heung-Min'.

        Exact fails on ordering; the token set is identical. This is the case
        the design doc calls out by name.
        """
        res = _resolve(fpl_players, understat_players, 5)
        assert res.understat_id == "200"
        assert res.method in ("exact", "token")
        assert res.confidence >= 0.95

    def test_fuzzy_catches_truncated_name(self, fpl_players, understat_players):
        """Bernardo Silva: FPL second_name is the full Portuguese legal name."""
        res = _resolve(fpl_players, understat_players, 3)
        assert res.understat_id == "102"
        assert res.status == "resolved"

    def test_diacritic_only_difference_resolves(self):
        fpl = [_p(1, "Josko", "Gvardiol", "Gvardiol", "MCI")]
        us = [_u("103", "Joško Gvardiol", "Manchester City")]
        res = _resolve(fpl, us, 1)
        assert res.understat_id == "103"


# --------------------------------------------------------------------------
# Club scoping -- what makes this deterministic rather than probabilistic
# --------------------------------------------------------------------------
class TestClubScoping:
    def test_never_matches_across_clubs(self, fpl_players):
        """A perfect name match at the wrong club must NOT bind."""
        us = [_u("500", "Erling Haaland", "Tottenham")]
        res = _resolve(fpl_players, us, 1)  # Haaland is MCI
        assert res.understat_id is None
        assert res.status == "unresolved"

    def test_unmapped_understat_team_is_not_a_candidate(self, fpl_players):
        us = [_u("501", "Erling Haaland", "Some Unknown FC")]
        res = _resolve(fpl_players, us, 1)
        assert res.status == "unresolved"

    def test_identical_surnames_at_different_clubs_stay_separate(self):
        fpl = [_p(1, "Thiago", "Silva", "Silva", "CHE"),
               _p(2, "Bernardo", "Silva", "Silva", "MCI")]
        us = [_u("600", "Thiago Silva", "Chelsea"),
              _u("601", "Bernardo Silva", "Manchester City")]
        aliases = {"Chelsea": "CHE", "Manchester City": "MCI"}
        r1 = matcher.resolve_one(fpl[0], us, team_aliases=aliases, overrides={})
        r2 = matcher.resolve_one(fpl[1], us, team_aliases=aliases, overrides={})
        assert r1.understat_id == "600"
        assert r2.understat_id == "601"


# --------------------------------------------------------------------------
# The margin rule -- the actual guard against R2
# --------------------------------------------------------------------------
class TestMarginRule:
    def test_ambiguous_pair_refuses_to_guess(self):
        """Two near-identical names at one club must yield 'unresolved'.

        This is the January-signing case: two similar names arrive at the same
        club and a score-only rule silently picks one. Neither candidate here
        matches exactly, so resolution genuinely reaches the fuzzy stage; both
        score ~91 (above T_SCORE) with a margin of ~0 (below T_MARGIN), which is
        precisely the configuration the margin rule exists to reject.
        """
        fpl = [_p(1, "Lucas", "Silva", "Silva", "MCI")]
        us = [_u("700", "Lucas Silvo", "Manchester City"),
              _u("701", "Lucas Silvu", "Manchester City")]
        res = matcher.resolve_one(fpl[0], us, team_aliases=TEAM_ALIASES, overrides={})

        assert res.candidates[0].score >= matcher.T_SCORE, (
            "precondition: the top candidate must clear the score threshold, "
            "otherwise this test proves nothing about the margin rule"
        )
        assert res.candidates[0].score - res.candidates[1].score < matcher.T_MARGIN, (
            "precondition: the runner-up must be inside the margin"
        )
        assert res.status == "unresolved"
        assert res.understat_id is None

    def test_clear_winner_above_the_margin_does_bind(self):
        """The margin rule must not reject everything -- a clear lead resolves."""
        fpl = [_p(1, "Lucas", "Silva", "Silva", "MCI")]
        us = [_u("700", "Lucas Silvo", "Manchester City"),
              _u("701", "Kevin De Bruyne", "Manchester City")]
        res = matcher.resolve_one(fpl[0], us, team_aliases=TEAM_ALIASES, overrides={})
        assert res.status == "resolved"
        assert res.understat_id == "700"
        assert res.method == "fuzzy"

    def test_fuzzy_below_score_threshold_is_unresolved(self):
        fpl = [_p(1, "Bukayo", "Saka", "Saka", "MCI")]
        us = [_u("800", "Kevin De Bruyne", "Manchester City")]
        res = matcher.resolve_one(fpl[0], us, team_aliases=TEAM_ALIASES, overrides={})
        assert res.status == "unresolved"
        assert res.understat_id is None

    def test_single_candidate_has_no_runner_up(self):
        fpl = [_p(1, "Erling", "Haaland", "Haaland", "MCI")]
        us = [_u("100", "Erling Haaland", "Manchester City")]
        res = matcher.resolve_one(fpl[0], us, team_aliases=TEAM_ALIASES, overrides={})
        assert res.runner_up_score == 0.0
        assert res.status == "resolved"

    def test_unresolved_carries_evidence_for_the_operator(self):
        """An unresolved row is a work item, so it must say what it nearly matched."""
        fpl = [_p(1, "Bukayo", "Saka", "Saka", "MCI")]
        us = [_u("800", "Kevin De Bruyne", "Manchester City"),
              _u("801", "Phil Foden", "Manchester City")]
        res = matcher.resolve_one(fpl[0], us, team_aliases=TEAM_ALIASES, overrides={})
        assert res.status == "unresolved"
        assert res.candidates, "must record what it considered"
        assert res.candidates[0].score >= res.candidates[-1].score


# --------------------------------------------------------------------------
# Uniqueness: no Understat player may serve two FPL players
# --------------------------------------------------------------------------
class TestUniqueness:
    def test_batch_resolution_never_double_binds(self, fpl_players, understat_players):
        report = matcher.resolve_batch(
            fpl_players, understat_players, team_aliases=TEAM_ALIASES, overrides={}
        )
        bound = [r.understat_id for r in report.resolutions if r.understat_id]
        assert len(bound) == len(set(bound)), "an Understat id was bound twice"

    def test_contested_id_goes_to_the_stronger_claim(self):
        """Two FPL players competing for one Understat id: best score wins,
        the loser becomes unresolved rather than silently sharing the binding."""
        fpl = [_p(1, "Bernardo", "Silva", "Silva", "MCI"),
               _p(2, "Bernardo", "Silvo", "Silvo", "MCI")]
        us = [_u("102", "Bernardo Silva", "Manchester City")]
        report = matcher.resolve_batch(
            fpl, us, team_aliases=TEAM_ALIASES, overrides={}
        )
        bound = [r for r in report.resolutions if r.understat_id]
        assert len(bound) == 1
        assert bound[0].fpl_player_id == 1

    def test_report_counts_add_up(self, fpl_players, understat_players):
        report = matcher.resolve_batch(
            fpl_players, understat_players, team_aliases=TEAM_ALIASES, overrides={}
        )
        assert report.total == len(fpl_players)
        assert report.resolved + report.unresolved == report.total
        assert 0.0 <= report.resolution_rate <= 1.0


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
class TestPersistence:
    def test_resolve_all_writes_entity_map(self, db, fpl_players, understat_players):
        _seed(db, fpl_players, understat_players)
        report = matcher.resolve_all(db, season=SEASON, team_aliases=TEAM_ALIASES,
                                     overrides={})
        assert report.resolved >= 5
        rows = db.execute("SELECT * FROM entity_map").fetchall()
        assert len(rows) == len(fpl_players)
        haaland = db.execute(
            "SELECT * FROM entity_map WHERE fpl_player_id = 1"
        ).fetchone()
        assert haaland["understat_id"] == "100"
        assert haaland["method"] == "exact"

    def test_is_idempotent(self, db, fpl_players, understat_players):
        _seed(db, fpl_players, understat_players)
        first = matcher.resolve_all(db, SEASON, TEAM_ALIASES, {})
        second = matcher.resolve_all(db, SEASON, TEAM_ALIASES, {})
        assert first.resolved == second.resolved
        assert db.execute("SELECT COUNT(*) c FROM entity_map").fetchone()["c"] \
            == len(fpl_players)

    def test_unique_index_blocks_double_binding_at_the_db_layer(self, db):
        """Belt and braces: even a buggy matcher cannot double-bind."""
        import sqlite3
        db.execute("INSERT INTO entity_map(fpl_player_id, understat_id) VALUES (1, '100')")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO entity_map(fpl_player_id, understat_id) VALUES (2, '100')")
            db.commit()

    def test_conflicting_rebind_is_flagged_not_applied(self, db, fpl_players,
                                                       understat_players):
        """A high-confidence binding must not be silently overwritten."""
        _seed(db, fpl_players, understat_players)
        matcher.resolve_all(db, SEASON, TEAM_ALIASES, {})

        # Simulate the player moving club upstream: same FPL id, new Understat id.
        moved = [dict(u) for u in understat_players]
        moved[0] = _u("999", "Erling Haaland", "Manchester City")
        _seed(db, fpl_players, moved)
        matcher.resolve_all(db, SEASON, TEAM_ALIASES, {})

        row = db.execute(
            "SELECT understat_id, status FROM entity_map WHERE fpl_player_id = 1"
        ).fetchone()
        assert row["status"] == "conflict"
        assert row["understat_id"] == "100", "existing binding must survive review"

    def test_unresolved_is_queryable(self, db, fpl_players):
        _seed(db, fpl_players, [])
        matcher.resolve_all(db, SEASON, TEAM_ALIASES, {})
        pending = matcher.unresolved(db)
        assert len(pending) == len(fpl_players)
        assert all(p["understat_id"] is None for p in pending)


# --------------------------------------------------------------------------
def _seed(conn, fpl_players, understat_players):
    """Load the miniature universe into players / teams / understat_player."""
    teams = {"MCI": 1, "TOT": 2, "CHE": 3}
    for short, tid in teams.items():
        conn.execute(
            "INSERT OR REPLACE INTO teams(id, name, short_name) VALUES (?, ?, ?)",
            (tid, short, short),
        )
    for p in fpl_players:
        conn.execute(
            """INSERT OR REPLACE INTO players
                 (id, web_name, first_name, second_name, team_id, element_type)
               VALUES (?, ?, ?, ?, ?, 3)""",
            (p["id"], p["web_name"], p["first_name"], p["second_name"],
             teams[p["team_short"]]),
        )
    conn.execute("DELETE FROM understat_player WHERE season = ?", (SEASON,))
    for u in understat_players:
        conn.execute(
            """INSERT OR REPLACE INTO understat_player
                 (understat_id, season, player_name, team_title)
               VALUES (?, ?, ?, ?)""",
            (u["id"], SEASON, u["player_name"], u["team_title"]),
        )
    conn.commit()
