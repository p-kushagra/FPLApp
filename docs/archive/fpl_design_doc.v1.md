# FPL App Design Document - Change Request Summary

## Document Created
Created comprehensive change request: `CHANGE_REQUEST.md` in repo root

## Four Major Issues Addressed

### 1. Gameweek Context Logic (Issue #1)
- **Problem:** Planner uses ambiguous "next unfinished" gameweek instead of FPL's actual current/next flags
- **Solution:** 
  - Store `current_gw` and `upcoming_gw` metadata from FPL API bootstrap data
  - Add `current_gw()` and `upcoming_gw()` helpers to planner.py
  - Update pages to use correct gameweek context
- **Files to modify:** pipeline.py, planner.py, pages/10_Fixture_Planner.py, pages/5_Captaincy.py

### 2. Understat Integration (Issue #2)
- **Problem:** FPL stats lack underlying xG/xA for quality assessment
- **Solution:**
  - Create understat_client.py with graceful failure and caching
  - Add understat_player_stats and understat_team_stats tables to db.py
  - Implement ingest_understat() in pipeline.py
  - Display xG diff and underlying metrics in pages
- **Files to create:** fpl_assistant/understat_client.py
- **Files to modify:** db.py, pipeline.py, ingest.py, pages/3_Transfer_Market.py

### 3. Weekly Performance Summary (Issue #3)
- **New page:** pages/0_Weekly_Summary.py
- **Shows:** Live/completed gameweek performance vs peers/template
- **Features:** Position breakdown, differentials tracking, captain analysis, bench regrets, next-week preview
- **Context:** Auto-uses current_gw() for live or latest_completed

### 4. Long-term Transfer Strategy (Issue #4)
- **New page:** pages/11_Transfer_Strategy.py
- **Shows:** Multi-week transfer planning with top-10 targets
- **Features:** Transfer scoring, captaincy plan, chip strategy, squad rotation planning
- **Adaptive:** Adjusts recommendations based on league position and risk tolerance

## Key Helper Functions Added
- `planner.current_gw()` - gameweek FPL is scoring
- `planner.upcoming_gw()` - gameweek for which decisions should be made
- `analytics.latest_stats()` - fetch up-to-date player stats through last completed GW
- `analytics.enriched_player_stats()` - merge FPL + Understat data

## Data Schema Additions
- meta table: stores current_gw, upcoming_gw, meta_updated_at
- understat_player_stats: xG, xA, goals, assists, defensive metrics
- understat_team_stats: team-level attacking/defensive xG, xA
- understat_player_fixture: per-fixture xG, xA, shots, etc.

## Implementation Timeline
- Phase 1: Gameweek logic (1-2h)
- Phase 2: Understat foundation (2-3h)
- Phase 3: Weekly summary page (1-2h)
- Phase 4: Transfer strategy page (2-3h)
- Phase 5: Integration & polish (1-2h)
- **Total: 7-12 hours**

## Outstanding Questions
1. Understat scraping vs API vs paid access?
2. FPL-Understat player name matching strategy?
3. Understat data cache TTL?
4. Transfer score weights tuning?
5. Configurable league-specific strategy logic?

## Success Criteria
- Gameweek context always correct
- Understat data available with graceful failure
- Weekly Summary shows all required metrics
- Transfer Strategy page recommends top-10 transfers
- No breaking changes to existing pages
