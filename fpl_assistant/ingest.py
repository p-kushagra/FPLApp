"""Command-line ingestion entry point.

Examples:
  python -m fpl_assistant.ingest --all
  python -m fpl_assistant.ingest --fpl --news
"""
from __future__ import annotations

import argparse

from . import pipeline
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="FPL Squad Assistant ingestion")
    parser.add_argument("--fpl", action="store_true", help="players, teams, fixtures")
    parser.add_argument("--team", action="store_true", help="your squad picks")
    parser.add_argument("--top", action="store_true", help="top-manager template ownership")
    parser.add_argument("--news", action="store_true", help="news feeds -> search index")
    parser.add_argument("--history", action="store_true",
                        help="per-gameweek player history (powers the learning engine)")
    parser.add_argument("--understat", action="store_true",
                        help="Understat league aggregates, entity resolution "
                             "and per-match fan-out (enrichment; the app works "
                             "without it)")
    parser.add_argument("--xp", action="store_true",
                        help="recompute expected-points projections")
    parser.add_argument("--freeze", action="store_true",
                        help="freeze pre-deadline projections if a deadline is near")
    parser.add_argument("--calibrate", action="store_true",
                        help="score frozen projections against realised points")
    parser.add_argument("--all", action="store_true", help="run everything")
    args = parser.parse_args()

    chosen = [args.fpl, args.team, args.top, args.news, args.history,
              args.understat, args.xp, args.freeze, args.calibrate, args.all]
    if not any(chosen):
        parser.error("choose at least one of --fpl --team --top --news "
                     "--history --understat --xp --freeze --calibrate --all")

    cfg = load_config()

    if args.all or args.fpl:
        gw = pipeline.ingest_fpl(cfg)
        print(f"FPL data ingested (current GW {gw}).")
    if args.all or args.team:
        team_gw = pipeline.ingest_my_team(cfg)
        print("Squad picks ingested." if team_gw
              else "Skipped squad (set FPL_TEAM_ID in .env).")
    if args.all or args.top:
        sample = pipeline.ingest_top_owned(cfg)
        print(f"Template ownership ingested from {sample} top managers.")
    if args.all or args.history:
        gws, rows = pipeline.ingest_history(cfg)
        print(f"History ingested: {gws} gameweeks, {rows} player-gameweek rows.")
    if args.all or args.news:
        articles, chunks, errors = pipeline.ingest_news(cfg)
        print(f"News ingested: {articles} new articles, {chunks} chunks.")
        for err in errors:
            print(f"  ! {err}")

    # v2 projection pipeline. Ordered after ingestion so the projections and
    # the freeze see the freshest data this run produced. The freeze is safe
    # on every tick: capture refuses a gameweek that is already frozen, too
    # far out, or past its deadline (models/snapshot.py), so "run it with
    # every refresh" is the schedule -- no clever timing required.
    if args.all or args.understat or args.xp or args.freeze or args.calibrate:
        from . import db as db_module
        from .jobs import tasks

        conn = db_module.connect(cfg.db_path)
        try:
            if args.all or args.understat:
                # League aggregates first: one request covers every player, so
                # it is always cheaper than discovering the same numbers by
                # fanning out. Resolution then decides who is worth fanning to.
                league = tasks.ingest_understat_league(conn)
                if not league.get("ok"):
                    print(f"Understat unavailable: {league.get('reason')} "
                          "- staying on FPL baseline stats.")
                else:
                    print(f"Understat league: {league['players']} players, "
                          f"{league['teams']} teams.")
                    resolved = tasks.resolve_entities(conn)
                    print(f"Entities resolved: {resolved['resolved']}"
                          f"/{resolved['total']} ({resolved['rate']:.0%}).")

                    ids = [r["understat_id"] for r in conn.execute(
                        "SELECT DISTINCT understat_id FROM entity_map "
                        "WHERE understat_id IS NOT NULL")]
                    matches = failed = 0
                    for i in range(0, len(ids), tasks.UNDERSTAT_CHUNK):
                        batch = ids[i:i + tasks.UNDERSTAT_CHUNK]
                        out = tasks.understat_fanout(conn, understat_ids=batch)
                        matches += out["matches"]
                        failed += len(out["failed"])
                        print(f"  ...{min(i + len(batch), len(ids))}/{len(ids)} "
                              f"players, {matches} match rows", flush=True)
                    print(f"Understat per-match: {matches} rows, {failed} failed.")

            if args.all or args.xp:
                result = tasks.recompute_xp(conn)
                print(f"xP recomputed: {result.get('projections', 0)} projections "
                      f"(understat_ok={result.get('understat_ok')}).")
            if args.all or args.freeze:
                result = tasks.freeze_projections(conn)
                for rec in result.get("frozen", []):
                    print(f"Froze GW{rec['gw']}: {rec['rows']} projections "
                          f"({rec['deadline_source']}).")
                for rec in result.get("skipped", []):
                    print(f"Freeze skipped GW{rec['gw']}: {rec['reason']}")
                if not result.get("frozen") and not result.get("skipped"):
                    from .models import snapshot as snapshot_mod
                    row = conn.execute("SELECT MAX(gw) FROM player_gw").fetchone()
                    next_gw = int(row[0] or 0) + 1
                    check = snapshot_mod.check_due(conn, next_gw)
                    print(f"Freeze: GW{next_gw} not captured - {check.reason}.")
            if args.all or args.calibrate:
                result = tasks.calibrate(conn)
                print(f"Calibration: {result['verdict']} "
                      f"(RMSE {result.get('rmse_model')}, "
                      f"{result.get('n_rows')} rows).")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
