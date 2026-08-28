"""End-to-end smoke test. Exercises every module against the real database.

Run:  python smoke_test.py
"""
from __future__ import annotations

import io
import sys
import traceback

# Risk bands contain emoji; the default Windows console codepage cannot encode them.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from fpl_assistant import analytics, congestion, role_arbitrage, search, squad_intel
from fpl_assistant.freshness import manual_sources, refresh_prompt
from fpl_assistant.insights import cache_stats, get_provider, summarise_cached
from fpl_assistant.insights.claude_provider import build_squad_briefing
from fpl_assistant.ui import boot

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name, fn):
    try:
        value = fn()
        results.append((PASS, name, str(value)[:90]))
    except Exception as exc:
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        traceback.print_exc()


cfg, conn = boot()

counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
          for t in ("players", "teams", "fixtures", "news_chunks",
                    "news_chunk_players", "player_gw")}
print("row counts:", counts)

pid_row = conn.execute(
    "SELECT id, team_id FROM players ORDER BY total_points DESC LIMIT 1").fetchone()
pid, tid = pid_row["id"], pid_row["team_id"]
player = dict(conn.execute(
    """SELECT p.*, t.short_name AS team_short FROM players p
       JOIN teams t ON t.id = p.team_id WHERE p.id = ?""", (pid,)).fetchone())

check("config.calendar", lambda: len(cfg.calendar))
check("config.regions", lambda: len(cfg.regions))
check("config.managers", lambda: len(cfg.managers))

check("analytics.differentials", lambda: len(analytics.differentials(conn)))
check("analytics.captaincy", lambda: len(analytics.captaincy(conn, cfg=cfg)))
check("analytics.price_watch", lambda: len(analytics.price_watch(conn)))
check("analytics.template", lambda: len(analytics.template(conn)))
check("analytics.squad_overview", lambda: len(analytics.squad_overview(conn)))

check("search.search_text", lambda: len(search.search_text(conn, "injury")))
check("search.search_player_news", lambda: len(search.search_player_news(conn, pid)))

check("congestion.active_events", lambda: len(congestion.active_events(cfg, horizon_days=400)))
check("congestion.cup_rounds", lambda: len(congestion.upcoming_cup_rounds(cfg, horizon_days=400)))
check("congestion.team_fixture_load", lambda: congestion.team_fixture_load(conn, cfg, tid))
check("congestion.rotation_risk", lambda: congestion.rotation_risk(conn, cfg, player)["band"])
check("congestion.unmapped_regions", lambda: len(congestion.unmapped_regions(conn, cfg)))

check("intel.start_probability", lambda: squad_intel.start_probability(conn, pid))
check("intel.predicted_xi", lambda: len(squad_intel.predicted_xi(conn, tid)))
check("intel.team_rotation_profile", lambda: squad_intel.team_rotation_profile(conn, tid))
check("intel.impact_share", lambda: squad_intel.impact_share(conn, pid))
check("intel.key_players", lambda: len(squad_intel.key_players(conn, tid)))
check("intel.absence_effect", lambda: squad_intel.absence_effect(conn, pid)["confidence"])
check("intel.comeback_watch", lambda: len(squad_intel.comeback_watch(conn)))
check("intel.sub_impact", lambda: squad_intel.sub_impact(conn, pid))
check("intel.head_to_head", lambda: squad_intel.head_to_head(conn, pid, 1)["sample"])
check("intel.new_signings", lambda: len(squad_intel.new_signings(conn)))
check("intel.team_style", lambda: squad_intel.team_style(conn, cfg, tid)["team"])

check("arb.position_baselines", lambda: len(role_arbitrage.position_baselines(conn)))
check("arb.role_profile", lambda: role_arbitrage.role_profile(conn, pid)["role"])
check("arb.points_premium",
      lambda: role_arbitrage.points_premium(role_arbitrage.role_profile(conn, pid)))
check("arb.window_risk",
      lambda: role_arbitrage.window_risk(conn, role_arbitrage.role_profile(conn, pid))["verdict"])
check("arb.candidates", lambda: len(role_arbitrage.arbitrage_candidates(conn, cfg)))
check("arb.squad", lambda: len(role_arbitrage.squad_arbitrage(conn)))

check("freshness.manual_sources", lambda: len(manual_sources(cfg)))
check("freshness.refresh_prompt", lambda: len(refresh_prompt(cfg)))

news = search.search_player_news(conn, pid, limit=5)
provider = get_provider(cfg)
check("insights.summarise", lambda: summarise_cached(conn, cfg, provider, player, news)[0].signal_type)
check("insights.cache_hit", lambda: summarise_cached(conn, cfg, provider, player, news)[1])
check("insights.cache_stats", lambda: cache_stats(conn))
check("insights.squad_briefing", lambda: len(build_squad_briefing([(player, news)])))

print("\n" + "=" * 70)
for status, name, detail in results:
    print(f"{status:4}  {name:32}  {detail}")
failed = [r for r in results if r[0] == FAIL]
print("=" * 70)
print(f"{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
