"""Deterministic FPL -> Understat entity resolution.

The ladder (design doc section 6.1), in strict order:

  1. manual override      confidence 1.00   an operator said so
  2. exact normalised     confidence 1.00   full name or web_name, unique
  3. token-set equal      confidence 0.95   same tokens, any order
  4. fuzzy WRatio         confidence s/100  score >= 88 AND margin >= 6
  5. unresolved                             recorded for operator review

Two design choices do the real work:

**Club scoping.** Candidates are restricted to the player's own club before any
fuzzy comparison happens. A 20-player candidate set with one plausible surname
is a decision; a 700-player set with six Silvas is a coin flip.

**The margin rule.** A match needs a high score AND a clear gap to the runner-up.
A high score with a close second is exactly the two-similar-signings case that
produces silent mis-binding -- the worst failure mode here, because it attributes
one player's xG to another and never announces itself. Refusing to guess is
always the better error.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

# Acceptance thresholds for the fuzzy stage.
T_SCORE = 88.0    # minimum WRatio to consider a binding at all
T_MARGIN = 6.0    # minimum lead over the runner-up

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SEPARATORS = re.compile(r"[-_/]+")
_SPACES = re.compile(r"\s+")


def normalise_name(value: str | None) -> str:
    """Casefold, strip diacritics and punctuation, collapse whitespace.

    Hyphens become spaces (so "Heung-Min" tokenises as two tokens and can match
    a space-separated source), but apostrophes are deleted rather than split --
    "O'Brien" is one token, not two.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _SEPARATORS.sub(" ", text)
    text = text.replace("'", "").replace("’", "")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip().casefold()


def _tokens(value: str | None) -> frozenset[str]:
    return frozenset(normalise_name(value).split())


@dataclass(frozen=True)
class MatchCandidate:
    understat_id: str
    understat_name: str
    score: float


@dataclass
class Resolution:
    """One player's outcome. Maps 1:1 onto an `entity_map` row."""

    fpl_player_id: int
    understat_id: str | None = None
    understat_name: str | None = None
    understat_team: str | None = None
    confidence: float = 0.0
    method: str = "none"          # manual|exact|token|fuzzy|none
    status: str = "unresolved"    # resolved|unresolved|conflict
    runner_up_score: float = 0.0
    candidates: list[MatchCandidate] = field(default_factory=list)
    source_hash: str = ""

    @property
    def bound(self) -> bool:
        return self.understat_id is not None and self.status == "resolved"


@dataclass
class ResolveReport:
    resolutions: list[Resolution] = field(default_factory=list)
    conflicts: list[Resolution] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.resolutions)

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.resolutions if r.bound)

    @property
    def unresolved(self) -> int:
        return self.total - self.resolved

    @property
    def resolution_rate(self) -> float:
        return self.resolved / self.total if self.total else 0.0

    def by_method(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.resolutions:
            if r.bound:
                counts[r.method] = counts.get(r.method, 0) + 1
        return counts


def _name_variants(player: dict) -> list[str]:
    """The FPL-side strings worth comparing, most specific first.

    web_name matters as much as the full name: single-name players (Rodri,
    Fabinho) carry their only usable identifier there, while the full name is
    a long legal name Understat never uses.
    """
    first = (player.get("first_name") or "").strip()
    second = (player.get("second_name") or "").strip()
    web = (player.get("web_name") or "").strip()
    known = (player.get("known_name") or "").strip()

    variants = []
    if first and second:
        variants.append(f"{first} {second}")
    if known:
        variants.append(known)
    if web:
        variants.append(web)
    if second:
        variants.append(second)
    # Preserve order, drop duplicates and empties.
    seen: set[str] = set()
    out = []
    for v in variants:
        key = normalise_name(v)
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _source_hash(player: dict) -> str:
    raw = "|".join(str(player.get(k) or "") for k in
                   ("first_name", "second_name", "web_name", "team_short"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _club_candidates(player: dict, understat_players: list[dict],
                     team_aliases: dict[str, str]) -> list[dict]:
    """Understat players at the same club. The determinism guarantee."""
    target = (player.get("team_short") or "").upper()
    if not target:
        return []
    return [
        u for u in understat_players
        if team_aliases.get((u.get("team_title") or "").strip(), "").upper() == target
    ]


def resolve_one(player: dict, understat_players: list[dict], *,
                team_aliases: dict[str, str],
                overrides: dict[int, str] | None = None) -> Resolution:
    """Resolve a single FPL player against the Understat universe."""
    overrides = overrides or {}
    pid = int(player["id"])
    res = Resolution(fpl_player_id=pid, source_hash=_source_hash(player))

    # Stage 1 -- manual override. Trusted absolutely, no candidate check.
    if pid in overrides:
        override_id = str(overrides[pid])
        match = next((u for u in understat_players
                      if str(u.get("id")) == override_id), None)
        res.understat_id = override_id
        res.understat_name = (match or {}).get("player_name")
        res.understat_team = (match or {}).get("team_title")
        res.confidence, res.method, res.status = 1.0, "manual", "resolved"
        return res

    candidates = _club_candidates(player, understat_players, team_aliases)
    if not candidates:
        return res  # unresolved: nothing at this club to match against

    variants = _name_variants(player)
    if not variants:
        return res

    # Stage 2 -- exact normalised match on any variant.
    for variant in variants:
        key = normalise_name(variant)
        hits = [u for u in candidates
                if normalise_name(u.get("player_name")) == key]
        if len(hits) == 1:
            return _bind(res, hits[0], 1.0, "exact")
        if len(hits) > 1:
            break  # ambiguous at this club; fall through rather than pick one

    # Stage 3 -- token-set equality. Handles reversed name order (Son).
    for variant in variants:
        toks = _tokens(variant)
        if not toks:
            continue
        hits = [u for u in candidates if _tokens(u.get("player_name")) == toks]
        if len(hits) == 1:
            return _bind(res, hits[0], 0.95, "token")

    # Stage 3b -- token subset. The Understat name's tokens are all present in
    # the FPL full name, uniquely at this club.
    #
    # Understat truncates long legal names: FPL's "Gabriel dos Santos Magalhaes"
    # is Understat's "Gabriel Magalhaes". Fuzzy cannot settle it, because
    # Arsenal field four players called Gabriel and the margin rule correctly
    # refuses to guess between them. Exact token containment does settle it, and
    # is strictly safer than fuzzy: it demands every Understat token appear
    # verbatim, and still requires a unique winner.
    #
    # Single-token names are excluded -- "Rodri" is a subset of far too much,
    # and those cases already resolve by exact match on web_name.
    for variant in variants:
        toks = _tokens(variant)
        if len(toks) < 2:
            continue
        hits = [
            u for u in candidates
            if len(_tokens(u.get("player_name"))) >= 2
            and _tokens(u.get("player_name")) < toks
        ]
        if len(hits) == 1:
            return _bind(res, hits[0], 0.93, "subset")

    # Stage 4 -- fuzzy, scored over the club-scoped set only.
    scored = [
        MatchCandidate(
            understat_id=str(u.get("id")),
            understat_name=u.get("player_name") or "",
            score=max(
                fuzz.WRatio(normalise_name(v), normalise_name(u.get("player_name")))
                for v in variants
            ),
        )
        for u in candidates
    ]
    scored.sort(key=lambda c: (-c.score, c.understat_id))
    res.candidates = scored[:5]

    best = scored[0]
    runner_up = scored[1].score if len(scored) > 1 else 0.0
    res.runner_up_score = runner_up

    if best.score >= T_SCORE and (best.score - runner_up) >= T_MARGIN:
        match = next(u for u in candidates if str(u.get("id")) == best.understat_id)
        return _bind(res, match, best.score / 100.0, "fuzzy", runner_up)

    # Deliberately unresolved. The candidates list is the operator's work item.
    return res


def _bind(res: Resolution, understat: dict, confidence: float, method: str,
          runner_up: float = 0.0) -> Resolution:
    res.understat_id = str(understat.get("id"))
    res.understat_name = understat.get("player_name")
    res.understat_team = understat.get("team_title")
    res.confidence = round(confidence, 4)
    res.method = method
    res.status = "resolved"
    res.runner_up_score = runner_up
    return res


def resolve_batch(fpl_players: list[dict], understat_players: list[dict], *,
                  team_aliases: dict[str, str],
                  overrides: dict[int, str] | None = None) -> ResolveReport:
    """Resolve every player, then enforce global one-to-one uniqueness.

    Per-player resolution cannot see that two players chose the same Understat
    id, so contested bindings are settled here: highest confidence wins (ties
    broken by FPL id for determinism) and every loser is demoted to unresolved
    rather than left sharing a binding.
    """
    resolutions = [
        resolve_one(p, understat_players, team_aliases=team_aliases,
                    overrides=overrides)
        for p in fpl_players
    ]

    claims: dict[str, list[Resolution]] = {}
    for r in resolutions:
        if r.bound and r.understat_id is not None:
            claims.setdefault(r.understat_id, []).append(r)

    for contenders in claims.values():
        if len(contenders) == 1:
            continue
        # A manual override always outranks an inferred binding.
        contenders.sort(
            key=lambda r: (r.method != "manual", -r.confidence, r.fpl_player_id)
        )
        for loser in contenders[1:]:
            loser.understat_id = None
            loser.understat_name = None
            loser.understat_team = None
            loser.confidence = 0.0
            loser.method = "none"
            loser.status = "unresolved"

    return ResolveReport(resolutions=resolutions)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def _load_fpl_players(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            """SELECT p.id, p.first_name, p.second_name, p.web_name,
                      p.known_name, t.short_name AS team_short
               FROM players p LEFT JOIN teams t ON t.id = p.team_id"""
        )
    ]


def _load_understat_players(conn: sqlite3.Connection, season: int) -> list[dict]:
    return [
        {"id": r["understat_id"], "player_name": r["player_name"],
         "team_title": r["team_title"]}
        for r in conn.execute(
            "SELECT understat_id, player_name, team_title "
            "FROM understat_player WHERE season = ?",
            (season,),
        )
    ]


def resolve_all(conn: sqlite3.Connection, season: int,
                team_aliases: dict[str, str],
                overrides: dict[int, str] | None = None) -> ResolveReport:
    """Resolve everyone and persist to `entity_map`.

    An existing high-confidence binding is never silently replaced. If a new run
    would bind the same FPL player to a different Understat id, the row is
    marked `conflict` and the ORIGINAL binding is kept -- a mid-season club
    transfer is the legitimate cause, and the operator confirms it.
    """
    fpl_players = _load_fpl_players(conn)
    understat_players = _load_understat_players(conn, season)
    report = resolve_batch(fpl_players, understat_players,
                           team_aliases=team_aliases, overrides=overrides)

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    existing = {
        r["fpl_player_id"]: r
        for r in conn.execute(
            "SELECT fpl_player_id, understat_id, confidence, status FROM entity_map"
        )
    }

    for res in report.resolutions:
        prior = existing.get(res.fpl_player_id)
        rebinding = (
            prior is not None
            and prior["understat_id"] is not None
            and res.understat_id is not None
            and str(prior["understat_id"]) != res.understat_id
            and float(prior["confidence"] or 0) >= 0.9
        )

        if rebinding:
            res.status = "conflict"
            report.conflicts.append(res)
            conn.execute(
                """UPDATE entity_map
                      SET status = 'conflict', runner_up_score = ?, resolved_at = ?
                    WHERE fpl_player_id = ?""",
                (res.confidence * 100.0, now, res.fpl_player_id),
            )
            continue

        conn.execute(
            """INSERT INTO entity_map
                 (fpl_player_id, understat_id, understat_name, understat_team,
                  confidence, method, status, runner_up_score, source_hash, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fpl_player_id) DO UPDATE SET
                 understat_id = excluded.understat_id,
                 understat_name = excluded.understat_name,
                 understat_team = excluded.understat_team,
                 confidence = excluded.confidence,
                 method = excluded.method,
                 status = excluded.status,
                 runner_up_score = excluded.runner_up_score,
                 source_hash = excluded.source_hash,
                 resolved_at = excluded.resolved_at""",
            (res.fpl_player_id, res.understat_id, res.understat_name,
             res.understat_team, res.confidence, res.method, res.status,
             res.runner_up_score, res.source_hash, now),
        )

    sync_player_links(conn)
    return report


def sync_player_links(conn: sqlite3.Connection) -> int:
    """Denormalise `entity_map` onto `players.understat_id` for cheap joins.

    `entity_map` is the source of truth; the column on `players` is a cache
    that the xP model and the shot-map join both read. Keeping the two in
    step is pure SQL with no network cost, so it is safe to call after any
    write to `players` -- which is exactly what the FPL refresh needs, since
    a bootstrap ingest that touches all 626 rows must not be able to leave
    the cache empty while resolution is intact.

    Returns the number of players carrying a link afterwards.
    """
    conn.execute(
        """UPDATE players SET understat_id = (
               SELECT understat_id FROM entity_map
               WHERE entity_map.fpl_player_id = players.id
                 AND entity_map.status = 'resolved')"""
    )
    conn.commit()
    row = conn.execute(
        "SELECT COUNT(*) FROM players WHERE understat_id IS NOT NULL"
    ).fetchone()
    return int(row[0]) if row else 0


def unresolved(conn: sqlite3.Connection) -> list[dict]:
    """Work queue for the Refresh Config review panel."""
    return [
        dict(r) for r in conn.execute(
            """SELECT em.fpl_player_id, em.understat_id, em.status,
                      em.runner_up_score, p.web_name, t.short_name AS team_short
               FROM entity_map em
               JOIN players p ON p.id = em.fpl_player_id
               LEFT JOIN teams t ON t.id = p.team_id
               WHERE em.status IN ('unresolved', 'conflict')
               ORDER BY t.short_name, p.web_name"""
        )
    ]
