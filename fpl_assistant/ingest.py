"""Command-line ingestion entry point.

Examples:
  python -m fpl_assistant.ingest --all
  python -m fpl_assistant.ingest --fpl --news
"""
from __future__ import annotations

import argparse

from .config import load_config
from . import pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="FPL Squad Assistant ingestion")
    parser.add_argument("--fpl", action="store_true", help="players, teams, fixtures")
    parser.add_argument("--team", action="store_true", help="your squad picks")
    parser.add_argument("--top", action="store_true", help="top-manager template ownership")
    parser.add_argument("--news", action="store_true", help="news feeds -> search index")
    parser.add_argument("--all", action="store_true", help="run everything")
    args = parser.parse_args()

    if not any([args.fpl, args.team, args.top, args.news, args.all]):
        parser.error("choose at least one of --fpl --team --top --news --all")

    cfg = load_config()

    if args.all or args.fpl:
        gw = pipeline.ingest_fpl(cfg)
        print(f"FPL data ingested (current GW {gw}).")
    if args.all or args.team:
        gw = pipeline.ingest_my_team(cfg)
        print("Squad picks ingested." if gw else "Skipped squad (set FPL_TEAM_ID in .env).")
    if args.all or args.top:
        sample = pipeline.ingest_top_owned(cfg)
        print(f"Template ownership ingested from {sample} top managers.")
    if args.all or args.news:
        articles, chunks = pipeline.ingest_news(cfg)
        print(f"News ingested: {articles} new articles, {chunks} chunks.")


if __name__ == "__main__":
    main()
