"""T-RES-01 (BLOCKING): zero false bindings between FPL and Understat.

A hand-audited corpus of 50 pairs covering every way the two sources disagree
about a name. The pass bar is asymmetric and deliberately so:

    FALSE BINDING   -> ZERO TOLERANCE. Attributing one player's xG to another
                       is silent, self-consistent and never looks wrong. It is
                       the single worst failure the data layer can produce.
    NO BINDING      -> acceptable. The player falls back to FPL baseline stats
                       and the UI says so. Visible and harmless.

So the suite asserts precision == 1.0 and only holds recall to a floor.

Every EXPECT_NONE case is a real trap: a perfect name at the wrong club, two
similar names at the same club, an unmapped club. Each one would bind under a
naive score-only matcher.
"""
from __future__ import annotations

import pytest

from fpl_assistant.resolve import matcher

TEAM_ALIASES = {
    "Arsenal": "ARS", "Aston Villa": "AVL", "Bournemouth": "BOU",
    "Brentford": "BRE", "Brighton": "BHA", "Chelsea": "CHE",
    "Crystal Palace": "CRY", "Everton": "EVE", "Fulham": "FUL",
    "Liverpool": "LIV", "Manchester City": "MCI", "Manchester United": "MUN",
    "Newcastle United": "NEW", "Nottingham Forest": "NFO",
    "Tottenham": "TOT", "West Ham": "WHU", "Wolverhampton Wanderers": "WOL",
}

# (understat_id, understat name, understat team)
UNDERSTAT = [
    ("1", "Erling Haaland", "Manchester City"),
    ("2", "Rodri", "Manchester City"),
    ("3", "Bernardo Silva", "Manchester City"),
    ("4", "Joško Gvardiol", "Manchester City"),
    ("5", "Phil Foden", "Manchester City"),
    ("6", "Rúben Dias", "Manchester City"),
    ("7", "Mohamed Salah", "Liverpool"),
    ("8", "Virgil van Dijk", "Liverpool"),
    ("9", "Alisson", "Liverpool"),
    ("10", "Trent Alexander-Arnold", "Liverpool"),
    ("11", "Luis Díaz", "Liverpool"),
    ("12", "Dominik Szoboszlai", "Liverpool"),
    ("13", "Son Heung-Min", "Tottenham"),
    ("14", "Cristian Romero", "Tottenham"),
    ("15", "Destiny Udogie", "Tottenham"),
    ("16", "Dejan Kulusevski", "Tottenham"),
    ("17", "Bukayo Saka", "Arsenal"),
    ("18", "Martin Ødegaard", "Arsenal"),
    ("19", "Gabriel Magalhães", "Arsenal"),
    ("20", "Gabriel Martinelli", "Arsenal"),
    ("21", "Gabriel Jesus", "Arsenal"),
    ("22", "William Saliba", "Arsenal"),
    ("23", "Kai Havertz", "Arsenal"),
    ("24", "Cole Palmer", "Chelsea"),
    ("25", "Nicolas Jackson", "Chelsea"),
    ("26", "Moisés Caicedo", "Chelsea"),
    ("27", "Thiago Silva", "Chelsea"),
    ("28", "Bruno Fernandes", "Manchester United"),
    ("29", "Marcus Rashford", "Manchester United"),
    ("30", "Diogo Dalot", "Manchester United"),
    ("31", "Alexander Isak", "Newcastle United"),
    ("32", "Anthony Gordon", "Newcastle United"),
    ("33", "Bruno Guimarães", "Newcastle United"),
    ("34", "Ollie Watkins", "Aston Villa"),
    ("35", "Morgan Rogers", "Aston Villa"),
    ("36", "Bryan Mbeumo", "Brentford"),
    ("37", "Yoane Wissa", "Brentford"),
    ("38", "Antoine Semenyo", "Bournemouth"),
    ("39", "Milos Kerkez", "Bournemouth"),
    ("40", "Jarrod Bowen", "West Ham"),
    ("41", "Lucas Paquetá", "West Ham"),
    ("42", "Jean-Philippe Mateta", "Crystal Palace"),
    ("43", "Eberechi Eze", "Crystal Palace"),
    ("44", "Matheus Cunha", "Wolverhampton Wanderers"),
    ("45", "Jarrad Branthwaite", "Everton"),
    ("46", "Chris Wood", "Nottingham Forest"),
    ("47", "Kaoru Mitoma", "Brighton"),
    ("48", "João Pedro", "Brighton"),
    ("49", "Raúl Jiménez", "Fulham"),
    ("50", "Antonee Robinson", "Fulham"),
    # Traps, present in the pool but must never bind to the cases below.
    ("90", "Gabriel Veiga", "Arsenal"),
    ("91", "Lucas Silva", "Chelsea"),
    ("92", "Lucas Silvo", "Chelsea"),
]

# (fpl_id, first, second, web_name, team_short, expected_understat_id)
# expected None means: MUST NOT BIND.
CASES = [
    # -- exact full-name matches --------------------------------------------
    (1, "Erling", "Haaland", "Haaland", "MCI", "1"),
    (5, "Phil", "Foden", "Foden", "MCI", "5"),
    (7, "Mohamed", "Salah", "M.Salah", "LIV", "7"),
    (17, "Bukayo", "Saka", "Saka", "ARS", "17"),
    (23, "Kai", "Havertz", "Havertz", "ARS", "23"),
    (24, "Cole", "Palmer", "Palmer", "CHE", "24"),
    (28, "Bruno", "Fernandes", "B.Fernandes", "MUN", "28"),
    (29, "Marcus", "Rashford", "Rashford", "MUN", "29"),
    (31, "Alexander", "Isak", "Isak", "NEW", "31"),
    (32, "Anthony", "Gordon", "Gordon", "NEW", "32"),
    (34, "Ollie", "Watkins", "Watkins", "AVL", "34"),
    (35, "Morgan", "Rogers", "Rogers", "AVL", "35"),
    (36, "Bryan", "Mbeumo", "Mbeumo", "BRE", "36"),
    (37, "Yoane", "Wissa", "Wissa", "BRE", "37"),
    (38, "Antoine", "Semenyo", "Semenyo", "BOU", "38"),
    (40, "Jarrod", "Bowen", "Bowen", "WHU", "40"),
    (43, "Eberechi", "Eze", "Eze", "CRY", "43"),
    (44, "Matheus", "Cunha", "Cunha", "WOL", "44"),
    (45, "Jarrad", "Branthwaite", "Branthwaite", "EVE", "45"),
    (46, "Chris", "Wood", "Wood", "NFO", "46"),
    (47, "Kaoru", "Mitoma", "Mitoma", "BHA", "47"),
    (50, "Antonee", "Robinson", "Robinson", "FUL", "50"),
    (15, "Destiny", "Udogie", "Udogie", "TOT", "15"),
    (16, "Dejan", "Kulusevski", "Kulusevski", "TOT", "16"),
    (14, "Cristian", "Romero", "Romero", "TOT", "14"),
    (22, "William", "Saliba", "Saliba", "ARS", "22"),
    (25, "Nicolas", "Jackson", "Jackson", "CHE", "25"),
    (30, "Diogo", "Dalot", "Dalot", "MUN", "30"),
    (20, "Gabriel", "Martinelli", "Martinelli", "ARS", "20"),
    (21, "Gabriel", "Jesus", "Jesus", "ARS", "21"),

    # -- diacritics ---------------------------------------------------------
    (4, "Josko", "Gvardiol", "Gvardiol", "MCI", "4"),
    (6, "Ruben", "Dias", "Dias", "MCI", "6"),
    (11, "Luis", "Diaz", "Luis Diaz", "LIV", "11"),
    (18, "Martin", "Odegaard", "Odegaard", "ARS", "18"),
    (26, "Moises", "Caicedo", "Caicedo", "CHE", "26"),
    (33, "Bruno", "Guimaraes", "Bruno G.", "NEW", "33"),
    (41, "Lucas", "Paqueta", "Paqueta", "WHU", "41"),
    (48, "Joao", "Pedro", "Joao Pedro", "BHA", "48"),
    (49, "Raul", "Jimenez", "Jimenez", "FUL", "49"),
    (19, "Gabriel", "dos Santos Magalhaes", "Gabriel", "ARS", "19"),

    # -- single-name players (only web_name is usable) -----------------------
    (2, "Rodrigo", "Hernandez Cascante", "Rodri", "MCI", "2"),
    (9, "Alisson", "Ramses Becker", "Alisson", "LIV", "9"),

    # -- reversed name order -------------------------------------------------
    (13, "Heung-Min", "Son", "Son", "TOT", "13"),

    # -- long legal names truncated by Understat ------------------------------
    (3, "Bernardo", "Veiga de Carvalho e Silva", "Bernardo", "MCI", "3"),
    (8, "Virgil", "van Dijk", "Virgil", "LIV", "8"),
    (10, "Trent", "Alexander-Arnold", "Alexander-Arnold", "LIV", "10"),
    (12, "Dominik", "Szoboszlai", "Szoboszlai", "LIV", "12"),
    (27, "Thiago", "Silva", "T.Silva", "CHE", "27"),
    (39, "Milos", "Kerkez", "Kerkez", "BOU", "39"),
    (42, "Jean-Philippe", "Mateta", "Mateta", "CRY", "42"),

    # -- TRAPS: must refuse to bind -------------------------------------------
    # Perfect name, wrong club.
    (101, "Erling", "Haaland", "Haaland", "LIV", None),
    (102, "Cole", "Palmer", "Palmer", "ARS", None),
    # Club not in the alias map at all.
    (103, "Some", "Player", "Player", "LEE", None),
    # Two near-identical names at one club: margin rule must reject.
    (104, "Lucas", "Silvu", "Silvu", "CHE", None),
    # Nothing remotely similar at the club.
    (105, "Completely", "Different", "Different", "NFO", None),
]


@pytest.fixture
def understat_pool():
    return [{"id": uid, "player_name": name, "team_title": team}
            for uid, name, team in UNDERSTAT]


def _fpl(case):
    fid, first, second, web, team, _ = case
    return {"id": fid, "first_name": first, "second_name": second,
            "web_name": web, "team_short": team}


# ==========================================================================
class TestGoldenCorpus:
    def test_corpus_is_the_advertised_size(self):
        assert len(CASES) == 55
        assert sum(1 for c in CASES if c[5] is None) == 5, "5 traps"
        assert sum(1 for c in CASES if c[5] is not None) == 50, "50 true pairs"

    @pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[3]}@{c[4]}")
    def test_each_case_resolves_as_audited(self, case, understat_pool):
        expected = case[5]
        res = matcher.resolve_one(_fpl(case), understat_pool,
                                  team_aliases=TEAM_ALIASES, overrides={})

        if expected is None:
            assert res.understat_id is None, (
                f"FALSE BINDING: {case[3]} ({case[4]}) bound to "
                f"{res.understat_name!r} via {res.method} "
                f"at confidence {res.confidence}"
            )
        else:
            assert res.understat_id == expected, (
                f"expected {expected}, got {res.understat_id} "
                f"({res.understat_name!r}) via {res.method}"
            )

    def test_precision_is_perfect(self, understat_pool):
        """ZERO false bindings across the whole corpus. Blocking assertion."""
        false_bindings = []
        for case in CASES:
            expected = case[5]
            res = matcher.resolve_one(_fpl(case), understat_pool,
                                      team_aliases=TEAM_ALIASES, overrides={})
            if res.understat_id is not None and res.understat_id != expected:
                false_bindings.append(
                    f"{case[3]}@{case[4]} -> {res.understat_name!r} "
                    f"({res.method}, conf {res.confidence})")

        assert not false_bindings, (
            f"{len(false_bindings)} FALSE BINDING(S):\n  "
            + "\n  ".join(false_bindings)
        )

    def test_recall_meets_the_floor(self, understat_pool):
        """>= 95% of genuine pairs resolve automatically (Phase 3 exit bar)."""
        true_pairs = [c for c in CASES if c[5] is not None]
        hits = sum(
            1 for c in true_pairs
            if matcher.resolve_one(_fpl(c), understat_pool,
                                   team_aliases=TEAM_ALIASES,
                                   overrides={}).understat_id == c[5]
        )
        recall = hits / len(true_pairs)
        assert recall >= 0.95, (
            f"recall {recall:.1%} ({hits}/{len(true_pairs)}) below the 95% floor"
        )

    def test_batch_resolution_binds_each_understat_id_once(self, understat_pool):
        players = [_fpl(c) for c in CASES]
        report = matcher.resolve_batch(players, understat_pool,
                                       team_aliases=TEAM_ALIASES, overrides={})
        bound = [r.understat_id for r in report.resolutions if r.understat_id]
        assert len(bound) == len(set(bound)), "an Understat id was bound twice"

    def test_batch_agrees_with_the_audit(self, understat_pool):
        players = [_fpl(c) for c in CASES]
        report = matcher.resolve_batch(players, understat_pool,
                                       team_aliases=TEAM_ALIASES, overrides={})
        by_id = {r.fpl_player_id: r for r in report.resolutions}
        for case in CASES:
            got = by_id[case[0]].understat_id
            assert got == case[5], f"{case[3]}: expected {case[5]}, got {got}"

    def test_confidence_is_recorded_for_every_binding(self, understat_pool):
        for case in (c for c in CASES if c[5] is not None):
            res = matcher.resolve_one(_fpl(case), understat_pool,
                                      team_aliases=TEAM_ALIASES, overrides={})
            assert 0.0 < res.confidence <= 1.0
            assert res.method in ("exact", "token", "subset", "fuzzy", "manual")

    def test_an_override_rescues_every_trap(self, understat_pool):
        """The documented escape hatch: the operator can always force a bind."""
        for case in (c for c in CASES if c[5] is None):
            res = matcher.resolve_one(_fpl(case), understat_pool,
                                      team_aliases=TEAM_ALIASES,
                                      overrides={case[0]: "1"})
            assert res.understat_id == "1"
            assert res.method == "manual"


class TestCorpusGuardsAgainstRegression:
    """Prove the corpus can fail: weakened guards must be caught."""

    def test_removing_club_scope_produces_false_bindings(self, understat_pool,
                                                         monkeypatch):
        monkeypatch.setattr(
            matcher, "_club_candidates",
            lambda player, understat_players, team_aliases: list(understat_players),
        )
        false_bindings = [
            c[3] for c in CASES if c[5] is None
            and matcher.resolve_one(_fpl(c), understat_pool,
                                    team_aliases=TEAM_ALIASES,
                                    overrides={}).understat_id is not None
        ]
        assert false_bindings, "corpus failed to catch unscoped matching"

    def test_dropping_the_margin_rule_produces_a_false_binding(self, understat_pool,
                                                               monkeypatch):
        monkeypatch.setattr(matcher, "T_MARGIN", 0.0)
        res = matcher.resolve_one(_fpl((104, "Lucas", "Silvu", "Silvu", "CHE", None)),
                                  understat_pool, team_aliases=TEAM_ALIASES,
                                  overrides={})
        assert res.understat_id is not None, (
            "corpus failed to catch a disabled margin rule"
        )
