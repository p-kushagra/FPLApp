"""Report which manually maintained configs are due for a review.

Run:  python -m fpl_assistant.check_sources
      python -m fpl_assistant.check_sources --prompt   # emit an agent briefing
"""
from __future__ import annotations

import argparse
import datetime as dt

from .config import load_config
from .freshness import manual_sources, refresh_prompt

MARK = {"ok": "OK  ", "due": "DUE ", "overdue": "LATE", "unknown": "????"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check config freshness")
    parser.add_argument("--prompt", action="store_true",
                        help="write an agent refresh briefing to briefings/")
    args = parser.parse_args()

    cfg = load_config()
    entries = manual_sources(cfg)

    print("\nManual config review status")
    print("-" * 72)
    for e in entries:
        age = f"{e['age_days']}d ago" if e["age_days"] is not None else "never"
        print(f"{MARK.get(e['status'], '?')}  {e['name'][:38]:38} "
              f"{age:>10}  (every {e['review_every_days']}d)")

    stale = [e for e in entries if e["status"] in ("due", "overdue", "unknown")]
    if not stale:
        print("\nAll manual configs are current.")
        return

    print(f"\n{len(stale)} config(s) need a review:")
    for e in stale:
        print(f"  - {e['name']}  ->  {e['config_file']}")
        for url in e["sources"][:3]:
            print(f"      {url}")

    if args.prompt:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = cfg.briefings_dir / f"config-refresh-{stamp}.md"
        path.write_text(refresh_prompt(cfg), encoding="utf-8")
        print(f"\nAgent briefing written to {path}")
    else:
        print("\nRun with --prompt to generate a Claude briefing for these.")


if __name__ == "__main__":
    main()
