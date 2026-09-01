"""Standalone Understat connectivity check.

Answers one question with real numbers: is the enrichment source actually
reachable and parsing, or is the app on the FPL baseline? Prints the underlying
metrics (npxG, xA) for a few named players so a wrong-but-plausible payload is
visible rather than silently accepted.

    python scripts/check_understat.py
    python scripts/check_understat.py --season 2025 --player Haaland --player Salah

Exit codes: 0 healthy, 1 unreachable or malformed.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_assistant.config import load_config          # noqa: E402
from fpl_assistant.db import connect                  # noqa: E402
from fpl_assistant.sources.understat import (         # noqa: E402
    AJAX_HEADER,
    AJAX_VALUE,
    TIMEOUT,
    UnderstatSource,
)

DEFAULT_PLAYERS = ("Haaland", "Palmer", "Salah")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--player", action="append", dest="players",
                        help="substring to match; repeatable")
    parser.add_argument("--matches", action="store_true",
                        help="also fetch per-match history for each player")
    args = parser.parse_args(argv)
    wanted = args.players or list(DEFAULT_PLAYERS)

    cfg = load_config()
    conn = connect(cfg.db_path)
    src = UnderstatSource(conn)

    print(f"timeout={TIMEOUT}s  header={AJAX_HEADER}: {AJAX_VALUE}")
    print(f"fetching league data for {args.season}...")

    started = time.monotonic()
    league = src.league_data(args.season)
    elapsed = time.monotonic() - started

    if not league.usable:
        print(f"FAIL  quality={league.quality.value}  error={league.error}")
        return 1

    players = src.league_players(args.season)
    teams = src.league_teams(args.season)
    if not players.usable or not teams.usable:
        print(f"FAIL  players={players.quality.value} "
              f"teams={teams.quality.value}  {players.error or teams.error}")
        return 1

    rows = players.data or []
    print(f"OK    quality={league.quality.value}  {len(rows)} players, "
          f"{len(teams.data or {})} teams  ({elapsed:.2f}s)")
    print()
    print(f"{'player':<24}{'team':<20}{'min':>5}{'G':>4}{'A':>4}"
          f"{'npxG':>8}{'xA':>8}{'xGChain':>9}")
    print("-" * 82)

    found = 0
    for needle in wanted:
        hits = [p for p in rows
                if needle.lower() in str(p.get("player_name", "")).lower()]
        if not hits:
            print(f"{needle:<24}(no match in the {args.season} season)")
            continue
        for p in hits[:3]:
            found += 1
            print(f"{p['player_name'][:23]:<24}{p['team_title'][:19]:<20}"
                  f"{int(p['time']):>5}{int(p['goals']):>4}"
                  f"{int(p['assists']):>4}{float(p['npxG']):>8.2f}"
                  f"{float(p['xA']):>8.2f}{float(p['xGChain']):>9.2f}")

            if args.matches:
                detail = src.player_matches(p["id"])
                if detail.usable:
                    recent = (detail.data or [])[:3]
                    for m in recent:
                        print(f"    {m.get('date')}  {m.get('h_team')} v "
                              f"{m.get('a_team')}  {m.get('time')}min  "
                              f"npxG {float(m.get('npxG', 0)):.2f}  "
                              f"xA {float(m.get('xA', 0)):.2f}")
                else:
                    print(f"    per-match unavailable: {detail.error}")

    if not found:
        print("\nFAIL  league data parsed but none of the named players matched")
        return 1

    print()
    print("Understat is reachable and parsing. If the app still shows the "
          "baseline badge, run: python -m fpl_assistant.ingest --understat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
