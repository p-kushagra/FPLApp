"""Ingestion pipeline: pull FPL + news data and persist it to SQLite."""
from __future__ import annotations

import datetime as dt

from . import chunk, entity, news_fetch
from .config import Config
from .db import connect, current_gw, init_db, set_meta
from .fpl_client import FplClient

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
OVERALL_LEAGUE_ID = 314


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return dt.datetime.utcnow().isoformat()


def ingest_fpl(cfg: Config) -> int:
    """Load teams, players, fixtures and the current gameweek."""
    init_db(cfg.db_path)
    client = FplClient()
    boot = client.bootstrap()
    conn = connect(cfg.db_path)
    try:
        for t in boot["teams"]:
            conn.execute(
                """INSERT OR REPLACE INTO teams
                   (id, name, short_name, strength,
                    strength_attack_home, strength_attack_away,
                    strength_defence_home, strength_defence_away,
                    strength_overall_home, strength_overall_away)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (t["id"], t["name"], t["short_name"], t.get("strength"),
                 t.get("strength_attack_home"), t.get("strength_attack_away"),
                 t.get("strength_defence_home"), t.get("strength_defence_away"),
                 t.get("strength_overall_home"), t.get("strength_overall_away")),
            )

        for p in boot["elements"]:
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (id, web_name, first_name, second_name, team_id, element_type, position,
                    now_cost, selected_by_percent, form, points_per_game, total_points, status,
                    chance_of_playing_next_round, transfers_in_event, transfers_out_event,
                    news, news_added, region, known_name, minutes, starts,
                    price_change_percent, scout_news_link, ep_next, team_join_date,
                    corners_order, freekicks_order, penalties_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p["id"], p["web_name"], p["first_name"], p["second_name"], p["team"],
                    p["element_type"], POSITIONS.get(p["element_type"], "?"),
                    p["now_cost"] / 10.0, _f(p["selected_by_percent"]), _f(p["form"]),
                    _f(p["points_per_game"]), p["total_points"], p["status"],
                    p.get("chance_of_playing_next_round"),
                    p["transfers_in_event"], p["transfers_out_event"],
                    p.get("news", ""), p.get("news_added"),
                    p.get("region"), p.get("known_name") or "",
                    p.get("minutes") or 0, p.get("starts") or 0,
                    _f(p.get("price_change_percent")), p.get("scout_news_link") or "",
                    _f(p.get("ep_next")), p.get("team_join_date"),
                    p.get("corners_and_indirect_freekicks_order"),
                    p.get("direct_freekicks_order"), p.get("penalties_order"),
                ),
            )

        gw = next((e["id"] for e in boot["events"] if e.get("is_current")), None)
        if gw is None:
            gw = next((e["id"] for e in boot["events"] if e.get("is_next")), 1)
        set_meta(conn, "current_gw", gw)

        for f in client.fixtures():
            conn.execute(
                """INSERT OR REPLACE INTO fixtures
                   (id, event, team_h, team_a, team_h_difficulty, team_a_difficulty,
                    kickoff_time, finished)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f["id"], f.get("event"), f["team_h"], f["team_a"],
                    f.get("team_h_difficulty"), f.get("team_a_difficulty"),
                    f.get("kickoff_time"), 1 if f.get("finished") else 0,
                ),
            )

        set_meta(conn, "fpl_last_ingest", _now())
        conn.commit()
        return int(gw)
    finally:
        conn.close()


def ingest_my_team(cfg: Config) -> int | None:
    """Load your current squad picks (requires FPL_TEAM_ID)."""
    if not cfg.fpl_team_id:
        return None
    client = FplClient()
    conn = connect(cfg.db_path)
    try:
        gw = current_gw(conn)
        picks = client.picks(cfg.fpl_team_id, gw)
        conn.execute("DELETE FROM my_picks WHERE gw = ?", (gw,))
        for pk in picks["picks"]:
            conn.execute(
                """INSERT OR REPLACE INTO my_picks
                   (gw, player_id, position, multiplier, is_captain, is_vice)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    gw, pk["element"], pk["position"], pk["multiplier"],
                    1 if pk["is_captain"] else 0, 1 if pk["is_vice_captain"] else 0,
                ),
            )
        set_meta(conn, "team_last_ingest", _now())
        conn.commit()
        return gw
    finally:
        conn.close()


def ingest_top_owned(cfg: Config, league_id: int = OVERALL_LEAGUE_ID) -> int:
    """Sample the top-N overall managers to derive effective ownership + captaincy."""
    client = FplClient()
    conn = connect(cfg.db_path)
    try:
        gw = current_gw(conn)
        wanted = cfg.top_managers_sample
        entries: list[int] = []
        page = 1
        while len(entries) < wanted:
            data = client.league_standings(league_id, page)
            results = data["standings"]["results"]
            if not results:
                break
            entries.extend(r["entry"] for r in results)
            if not data["standings"]["has_next"]:
                break
            page += 1
        entries = entries[:wanted]

        owned: dict[int, int] = {}
        captained: dict[int, int] = {}
        for entry_id in entries:
            try:
                picks = client.picks(entry_id, gw)
            except Exception:
                continue
            for pk in picks["picks"]:
                owned[pk["element"]] = owned.get(pk["element"], 0) + 1
                if pk["is_captain"]:
                    captained[pk["element"]] = captained.get(pk["element"], 0) + 1

        sample = len(entries) or 1
        conn.execute("DELETE FROM top_owned WHERE gw = ?", (gw,))
        for pid, count in owned.items():
            conn.execute(
                """INSERT OR REPLACE INTO top_owned
                   (gw, player_id, ownership_pct, captain_pct, sample_size)
                   VALUES (?, ?, ?, ?, ?)""",
                (gw, pid, 100.0 * count / sample, 100.0 * captained.get(pid, 0) / sample, sample),
            )
        set_meta(conn, "top_last_ingest", _now())
        conn.commit()
        return sample
    finally:
        conn.close()


def ingest_history(cfg: Config, upto_gw: int | None = None,
                   refresh_last: int = 2) -> tuple[int, int]:
    """Build per-gameweek player history from event/{gw}/live.

    One request per gameweek covers every player, so a full season costs ~38
    requests rather than one per player. Completed gameweeks are only fetched once;
    the most recent `refresh_last` are re-fetched in case stats were corrected.

    Returns (gameweeks_fetched, rows_written).
    """
    init_db(cfg.db_path)
    client = FplClient()
    conn = connect(cfg.db_path)
    try:
        gw_now = upto_gw or current_gw(conn)

        fixture_lookup: dict[tuple[int, int], tuple[int, int, int]] = {}
        for f in conn.execute(
            "SELECT id, event, team_h, team_a FROM fixtures WHERE event IS NOT NULL"
        ):
            fixture_lookup[(f["event"], f["team_h"])] = (f["id"], f["team_a"], 1)
            fixture_lookup[(f["event"], f["team_a"])] = (f["id"], f["team_h"], 0)

        player_team = {r["id"]: r["team_id"] for r in
                       conn.execute("SELECT id, team_id FROM players")}

        done = {r["gw"] for r in conn.execute("SELECT DISTINCT gw FROM player_gw")}
        gws_fetched = 0
        rows = 0

        for gw in range(1, gw_now + 1):
            if gw in done and gw < gw_now - refresh_last:
                continue
            try:
                live = client.live(gw)
            except Exception:
                continue
            gws_fetched += 1

            for el in live.get("elements", []):
                pid = el["id"]
                s = el.get("stats", {})
                if not s:
                    continue

                fixture_id = opponent = None
                was_home = None
                explain = el.get("explain") or []
                if explain and isinstance(explain[0], dict):
                    fixture_id = explain[0].get("fixture")
                meta = fixture_lookup.get((gw, player_team.get(pid)))
                if meta:
                    fixture_id = fixture_id or meta[0]
                    opponent, was_home = meta[1], meta[2]

                conn.execute(
                    """INSERT OR REPLACE INTO player_gw
                       (player_id, gw, minutes, starts, total_points,
                        goals_scored, assists, clean_sheets,
                        expected_goals, expected_assists,
                        expected_goal_involvements, expected_goals_conceded,
                        defensive_contribution, tackles, recoveries,
                        clearances_blocks_interceptions, saves, bps, bonus,
                        yellow_cards, red_cards, threat, creativity, influence,
                        ict_index, fixture_id, opponent_team, was_home)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pid, gw, s.get("minutes") or 0, s.get("starts") or 0,
                        s.get("total_points") or 0, s.get("goals_scored") or 0,
                        s.get("assists") or 0, s.get("clean_sheets") or 0,
                        _f(s.get("expected_goals")), _f(s.get("expected_assists")),
                        _f(s.get("expected_goal_involvements")),
                        _f(s.get("expected_goals_conceded")),
                        _f(s.get("defensive_contribution")), s.get("tackles") or 0,
                        s.get("recoveries") or 0,
                        s.get("clearances_blocks_interceptions") or 0,
                        s.get("saves") or 0, s.get("bps") or 0, s.get("bonus") or 0,
                        s.get("yellow_cards") or 0, s.get("red_cards") or 0,
                        _f(s.get("threat")), _f(s.get("creativity")),
                        _f(s.get("influence")), _f(s.get("ict_index")),
                        fixture_id, opponent, was_home,
                    ),
                )
                rows += 1
            conn.commit()

        set_meta(conn, "history_last_ingest", _now())
        set_meta(conn, "history_upto_gw", gw_now)
        conn.commit()
        return gws_fetched, rows
    finally:
        conn.close()


def ingest_news(cfg: Config) -> tuple[int, int, list[str]]:
    """Fetch news, chunk it, tag players, and index for full-text search.

    Returns (new_articles, new_chunks, source_errors).
    """
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        players = [dict(r) for r in conn.execute(
            "SELECT id, web_name, first_name, second_name FROM players"
        )]
        alias_index = entity.build_alias_index(players)

        feeds = cfg.sources.get("rss", []) or []
        subs = cfg.sources.get("reddit", []) or []
        rss_items, rss_errors = news_fetch.fetch_rss(feeds)
        reddit_items, reddit_errors = news_fetch.fetch_reddit(subs)
        items = rss_items + reddit_items
        errors = rss_errors + reddit_errors

        now = _now()
        new_articles = 0
        new_chunks = 0
        for item in items:
            if not item["url"]:
                continue
            if conn.execute("SELECT 1 FROM news_articles WHERE url = ?", (item["url"],)).fetchone():
                continue
            cur = conn.execute(
                """INSERT INTO news_articles(source, url, title, published_at, fetched_at, raw_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (item["source"], item["url"], item["title"],
                 item["published_at"], now, item["body"]),
            )
            article_id = cur.lastrowid
            new_articles += 1

            full_text = f"{item['title']}. {item['body']}".strip()
            for idx, piece in enumerate(chunk.chunk_text(full_text)):
                cc = conn.execute(
                    """INSERT INTO news_chunks(article_id, chunk_index, text, published_at, source, url)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (article_id, idx, piece, item["published_at"], item["source"], item["url"]),
                )
                chunk_id = cc.lastrowid
                new_chunks += 1
                for pid, score in entity.tag_text(piece, alias_index).items():
                    conn.execute(
                        """INSERT OR REPLACE INTO news_chunk_players(chunk_id, player_id, match_score)
                           VALUES (?, ?, ?)""",
                        (chunk_id, pid, score),
                    )

        set_meta(conn, "news_last_ingest", now)
        conn.commit()
        return new_articles, new_chunks, errors
    finally:
        conn.close()
