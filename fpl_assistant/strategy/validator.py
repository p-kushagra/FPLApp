"""Independent squad-legality validator -- T-SOLV-06.

DELIBERATELY SHARES NO CODE WITH THE SOLVER.

A validator built from the solver's own constraint helpers can only ever prove
the solver is self-consistent. This one re-derives every rule straight from
`config/rules.yaml` and checks a concrete squad, so a wrong constraint in
`solver.py` shows up as a failure here rather than as confidently illegal advice.

That asymmetry is the point: an illegal squad and a mis-bound entity are the two
failures that produce output which looks authoritative and is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..rules import ELEMENT_TYPE_TO_POS, load_rules


@dataclass
class Violation:
    rule: str
    detail: str
    gw: int | None = None

    def __str__(self) -> str:
        where = f"GW{self.gw}: " if self.gw is not None else ""
        return f"{where}{self.rule} - {self.detail}"


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def legal(self) -> bool:
        return not self.violations

    def __bool__(self) -> bool:
        return self.legal

    def report(self) -> str:
        if self.legal:
            return "legal"
        return "; ".join(str(v) for v in self.violations)

    def add(self, rule: str, detail: str, gw: int | None = None) -> None:
        self.violations.append(Violation(rule, detail, gw))


def _position(player: dict) -> str:
    if player.get("position"):
        return str(player["position"])
    etype = player.get("element_type")
    return ELEMENT_TYPE_TO_POS.get(etype, "?") if etype is not None else "?"


def validate_squad(squad: list[dict], *, bank: float = 0.0,
                   rules: dict | None = None,
                   gw: int | None = None) -> ValidationResult:
    """Check a 15-man squad against every structural rule.

    Each `player` needs: id, element_type (or position), team_id, now_cost.
    """
    r = load_rules() if rules is None else rules
    sq = r["squad"]
    result = ValidationResult()

    # -- size
    if len(squad) != int(sq["size"]):
        result.add("squad_size",
                   f"expected {sq['size']} players, got {len(squad)}", gw)

    # -- duplicates
    ids = [p["id"] for p in squad]
    if len(ids) != len(set(ids)):
        dupes = {i for i in ids if ids.count(i) > 1}
        result.add("duplicate_players", f"player ids appear twice: {sorted(dupes)}", gw)

    # -- positional quota
    counts: dict[str, int] = {}
    for p in squad:
        counts[_position(p)] = counts.get(_position(p), 0) + 1
    for pos, want in sq["quota"].items():
        got = counts.get(pos, 0)
        if got != int(want):
            result.add("position_quota", f"{pos}: expected {want}, got {got}", gw)

    # -- club limit
    per_club: dict[int, int] = {}
    for p in squad:
        # -1 buckets players whose club is unknown. They still count toward a
        # limit, so a squad of unmapped players cannot slip past the club cap.
        tid = int(p["team_id"]) if p.get("team_id") is not None else -1
        per_club[tid] = per_club.get(tid, 0) + 1
    limit = int(sq["max_per_club"])
    for tid, n in sorted(per_club.items()):
        if n > limit:
            result.add("club_limit", f"team {tid} has {n} players (max {limit})", gw)

    # -- budget
    total = sum(float(p.get("now_cost") or 0.0) for p in squad)
    budget = float(sq["budget"])
    if total - bank > budget + 1e-6:
        result.add("budget",
                   f"squad costs {total:.1f} with bank {bank:.1f}, "
                   f"exceeds {budget:.1f}", gw)
    if bank < -1e-6:
        result.add("budget", f"negative bank: {bank:.2f}", gw)

    return result


def validate_lineup(starting_xi: list[dict], squad: list[dict],
                    captain_id: int | None = None,
                    vice_id: int | None = None,
                    rules: dict | None = None,
                    gw: int | None = None) -> ValidationResult:
    """Check the starting XI: size, subset, formation legality, captaincy."""
    r = load_rules() if rules is None else rules
    formation = r["squad"]["formation"]
    result = ValidationResult()

    if len(starting_xi) != 11:
        result.add("xi_size", f"expected 11 starters, got {len(starting_xi)}", gw)

    squad_ids = {p["id"] for p in squad}
    outsiders = [p["id"] for p in starting_xi if p["id"] not in squad_ids]
    if outsiders:
        result.add("xi_subset", f"starters not in the squad: {outsiders}", gw)

    counts: dict[str, int] = {}
    for p in starting_xi:
        counts[_position(p)] = counts.get(_position(p), 0) + 1

    for pos, (lo, hi) in formation.items():
        got = counts.get(pos, 0)
        if got < int(lo) or got > int(hi):
            result.add("formation", f"{pos}: {got} not in [{lo}, {hi}]", gw)

    if captain_id is not None:
        xi_ids = {p["id"] for p in starting_xi}
        if captain_id not in xi_ids:
            result.add("captain", f"captain {captain_id} is not in the XI", gw)
        if vice_id is not None and vice_id == captain_id:
            result.add("captain", "captain and vice are the same player", gw)

    return result


def validate_transfers(*, transfers_made: int, free_transfers: int,
                       hits_charged: int, chip: str | None = None,
                       rules: dict | None = None,
                       gw: int | None = None) -> ValidationResult:
    """Check transfer economics against the ruleset.

    Re-derives the expected hit independently of the solver's C11/C12 block.
    """
    r = load_rules() if rules is None else rules
    t = r["transfers"]
    result = ValidationResult()

    is_squad_chip = (chip or "").lower().replace(" ", "") in {
        "wildcard", "freehit", "free_hit"
    }

    if is_squad_chip and bool(t["chip_retains_ft"]):
        expected_hits = 0
    else:
        expected_hits = max(0, transfers_made - free_transfers)

    if hits_charged != expected_hits:
        result.add(
            "transfer_cost",
            f"{transfers_made} transfers with {free_transfers} FT"
            f"{' under ' + str(chip) if chip else ''} implies {expected_hits} hit(s), "
            f"charged {hits_charged}",
            gw,
        )

    if free_transfers < 0 or free_transfers > int(t["max_banked"]):
        result.add("ft_bank",
                   f"free transfers {free_transfers} outside [0, {t['max_banked']}]", gw)

    if transfers_made < 0:
        result.add("transfers", f"negative transfer count: {transfers_made}", gw)

    return result


def validate_path(path, players_by_id: dict[int, dict],
                  rules: dict | None = None) -> ValidationResult:
    """Validate an entire multi-gameweek solver path.

    Walks the squad forward through each gameweek's moves and re-checks every
    rule at every step, so an illegal intermediate squad cannot hide behind a
    legal final one.
    """
    r = load_rules() if rules is None else rules
    result = ValidationResult()

    squad_ids = set(path.initial_squad)
    bank = float(path.initial_bank)
    ft = int(path.initial_ft)

    for step in path.steps:
        outs = [m.player_out for m in step.moves]
        ins = [m.player_in for m in step.moves]

        for pid in outs:
            if pid not in squad_ids:
                result.add("transfer_out",
                           f"selling {pid} who is not in the squad", step.gw)
        for pid in ins:
            if pid in squad_ids:
                result.add("transfer_in",
                           f"buying {pid} who is already in the squad", step.gw)
        if len(set(ins)) != len(ins):
            result.add("transfer_in", "same player bought twice", step.gw)

        proceeds = sum(float(players_by_id[p]["now_cost"]) for p in outs
                       if p in players_by_id)
        spend = sum(float(players_by_id[p]["now_cost"]) for p in ins
                    if p in players_by_id)
        bank = bank + proceeds - spend
        if bank < -1e-6:
            result.add("budget", f"bank went negative: {bank:.2f}", step.gw)

        squad_ids = (squad_ids - set(outs)) | set(ins)

        squad = [players_by_id[p] for p in squad_ids if p in players_by_id]
        result.violations.extend(
            validate_squad(squad, bank=bank, rules=r, gw=step.gw).violations
        )
        result.violations.extend(
            validate_transfers(
                transfers_made=len(step.moves), free_transfers=ft,
                hits_charged=step.hits, chip=step.chip, rules=r, gw=step.gw,
            ).violations
        )

        # Advance the bank using the same rule the validator just enforced.
        from ..temporal import FTBank, project_ft
        ft = project_ft(FTBank(gw=step.gw, available=ft),
                        len(step.moves), step.chip, r).available

    return result
