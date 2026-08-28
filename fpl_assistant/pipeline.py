"""Ingestion pipeline: pull FPL + news data and persist it to SQLite."""
from __future__ import annotations

import datetime as dt

from . import chunk, entity, news_fetch
from .config import Config
from .db import connect, current_gw, init_db, set_meta
from .fpl_client import FplClient

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
OVERALL_LEAGUE_ID = 314


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
                "INSERT OR REPLACE INTO teams(id, name, short_name, strength) VALUES (?, ?, ?, ?)",
                (t["id"], t["name"], t["short_name"], t.get("strength")),
            )

        for p in boot["elements"]:
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (id, web_name, first_name, second_name, team_id, element_type, position,
                    now_cost, selected_by_percent, form, points_per_game, total_points, status,
                    chance_of_playing_next_round, transfers_in_event, transfers_out_event,
                    news, news_added)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p["id"], p["web_name"], p["first_name"], p["second_name"], p["team"],
                    p["element_type"], POSITIONS.get(p["element_type"], "?"),
                    p["now_cost"] / 10.0, float(p["selected_by_percent"]), float(p["form"]),
                    float(p["points_per_game"]), p["total_points"], p["status"],
                    p.get("chance_of_playing_next_round"),
                    p["transfers_in_event"], p["transfers_out_event"],
                    p.get("news", ""), p.get("news_added"),
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


def ingest_news(cfg: Config) -> tuple[int, int]:
    """Fetch news, chunk it, tag players, and index for full-text search."""
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        players = [dict(r) for r in conn.execute(
            "SELECT id, web_name, first_name, second_name FROM players"
        )]
        alias_index = entity.build_alias_index(players)

        feeds = cfg.sources.get("rss", []) or []
        subs = cfg.sources.get("reddit", []) or []
        items = news_fetch.fetch_rss(feeds) + news_fetch.fetch_reddit(subs)

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
        return new_articles, new_chunks
    finally:
        conn.close()
