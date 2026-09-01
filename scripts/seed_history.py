"""Seed historical player baselines for Bayesian prior blending.

Usage (from the repository root):

    python scripts/seed_history.py                # full seed: FPL + Understat + imputation
    python scripts/seed_history.py --limit 20     # smoke-test on 20 players
    python scripts/seed_history.py --no-understat # skip the Understat pass
    python scripts/seed_history.py --offline      # imputation only, no network

Idempotent: rows are keyed (player_id, season_name, source) and re-runs
overwrite in place. The FPL pass is served through the SWR cache, so a re-run
within the cache TTL costs zero requests.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fpl_assistant import db as db_module
from fpl_assistant import ingest_history
from fpl_assistant.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N players (testing)")
    parser.add_argument("--no-understat", action="store_true",
                        help="skip the Understat season-aggregate pass")
    parser.add_argument("--offline", action="store_true",
                        help="no network: run only the imputation pass")
    args = parser.parse_args(argv)

    cfg = load_config()
    db_module.init_db(cfg.db_path)  # applies the v4 migration if pending
    conn = db_module.connect(cfg.db_path)

    def progress(done: int, total: int, name: str) -> None:
        print(f"  [{done}/{total}] {name}", flush=True)

    try:
        report = ingest_history.seed(
            conn,
            limit=args.limit,
            understat=not args.no_understat,
            network=not args.offline,
            progress=progress,
        )
    finally:
        conn.close()

    print()
    print("historical_player_baselines seeded")
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
