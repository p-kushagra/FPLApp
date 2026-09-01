"""Schema v2 migration: additive, idempotent, and safe on a v1 database.

Risk R7 -- a migration corrupting an existing database -- is the one failure in
Phase 1 with no cheap recovery: a season of `player_gw` history cannot be
re-derived once lost.
"""
from __future__ import annotations

import sqlite3

from fpl_assistant import db as db_module

V2_TABLES = [
    "gw_state", "ft_bank", "chip_state", "entity_map",
    "understat_player", "understat_player_match", "understat_team",
    "league", "league_standing", "league_rival_pick", "ileo_cache",
    "xp_projection", "variance_decomp",
    "price_snapshot", "price_change", "price_prediction",
    "solver_run", "solver_path", "solver_move", "planned_move",
    "cache_entry", "job", "source_health", "rate_budget",
]

V1_TABLES = [
    "meta", "teams", "players", "player_gw", "fixtures", "my_picks",
    "top_owned", "news_articles", "news_chunks", "news_chunk_players",
    "insights", "ai_cache",
]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class TestFreshDatabase:
    def test_creates_every_v2_table(self, db):
        present = _tables(db)
        assert not (set(V2_TABLES) - present)

    def test_keeps_every_v1_table(self, db):
        present = _tables(db)
        assert not (set(V1_TABLES) - present)

    def test_stamps_the_version(self, db):
        assert db_module.schema_version(db) == db_module.SCHEMA_VERSION == 2

    def test_adds_v2_columns_to_v1_tables(self, db):
        players = {r["name"] for r in db.execute("PRAGMA table_info(players)")}
        assert {"understat_id", "purchase_price"} <= players
        picks = {r["name"] for r in db.execute("PRAGMA table_info(my_picks)")}
        assert {"selling_price", "purchase_price", "chip"} <= picks

    def test_fts5_still_works(self, db):
        db.execute("INSERT INTO news_chunks(article_id, chunk_index, text) "
                   "VALUES (1, 0, 'Haaland scored a hat-trick')")
        db.commit()
        hits = db.execute(
            "SELECT rowid FROM news_chunks_fts WHERE news_chunks_fts MATCH 'hat'"
        ).fetchall()
        assert len(hits) == 1


class TestIdempotence:
    def test_reinit_is_a_noop(self, db_path):
        db_module.init_db(db_path)
        conn = db_module.connect(db_path)
        before = _tables(conn)
        conn.close()

        db_module.init_db(db_path)
        db_module.init_db(db_path)

        conn = db_module.connect(db_path)
        assert _tables(conn) == before
        assert db_module.schema_version(conn) == 2
        conn.close()

    def test_migrate_on_a_current_database_changes_nothing(self, db):
        assert db_module.migrate(db) == 2
        assert db_module.migrate(db) == 2


class TestUpgradeFromV1:
    def _make_v1(self, path):
        """A v1 database: the old schema and old column-adder only."""
        conn = db_module.connect(path)
        conn.executescript(db_module.SCHEMA)
        db_module._migrate(conn)
        conn.execute("INSERT INTO teams(id, name, short_name) VALUES (1, 'City', 'MCI')")
        conn.execute(
            "INSERT INTO players(id, web_name, team_id, now_cost) VALUES (1, 'Haaland', 1, 15.0)")
        conn.execute(
            "INSERT INTO player_gw(player_id, gw, minutes, total_points) VALUES (1, 1, 90, 13)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('current_gw', '7')")
        conn.commit()
        conn.close()

    def test_upgrade_preserves_all_data(self, db_path):
        self._make_v1(db_path)
        db_module.init_db(db_path)

        conn = db_module.connect(db_path)
        assert conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 1
        row = conn.execute("SELECT * FROM player_gw WHERE player_id=1").fetchone()
        assert row["total_points"] == 13
        assert db_module.get_meta(conn, "current_gw") == "7"
        assert db_module.schema_version(conn) == 2
        conn.close()

    def test_upgrade_writes_a_backup(self, db_path):
        self._make_v1(db_path)
        db_module.init_db(db_path)
        backup = db_path.with_suffix(db_path.suffix + ".bak.v1")
        assert backup.exists() and backup.stat().st_size > 0

    def test_backup_is_taken_once_not_on_every_open(self, db_path):
        self._make_v1(db_path)
        db_module.init_db(db_path)
        backup = db_path.with_suffix(db_path.suffix + ".bak.v1")
        first = backup.stat().st_mtime_ns

        conn = db_module.connect(db_path)
        conn.execute("INSERT INTO players(id, web_name) VALUES (2, 'Salah')")
        conn.commit()
        conn.close()
        db_module.init_db(db_path)

        assert backup.stat().st_mtime_ns == first, "must not re-snapshot post-upgrade"

    def test_v1_database_reports_version_1_before_upgrade(self, db_path):
        self._make_v1(db_path)
        conn = db_module.connect(db_path)
        assert db_module.schema_version(conn) == 1
        conn.close()

    def test_migration_is_purely_additive(self, db_path):
        """No v1 table or column may be dropped or renamed."""
        self._make_v1(db_path)
        conn = db_module.connect(db_path)
        before = {t: {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}
                  for t in V1_TABLES}
        conn.close()

        db_module.init_db(db_path)

        conn = db_module.connect(db_path)
        for table, columns in before.items():
            now = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert columns <= now, f"{table} lost columns: {columns - now}"
        conn.close()


class TestConstraints:
    def test_understat_id_is_unique_across_players(self, db):
        db.execute("INSERT INTO entity_map(fpl_player_id, understat_id) VALUES (1,'100')")
        db.commit()
        try:
            db.execute(
                "INSERT INTO entity_map(fpl_player_id, understat_id) VALUES (2,'100')")
            db.commit()
            raise AssertionError("expected IntegrityError on duplicate understat_id")
        except sqlite3.IntegrityError:
            db.rollback()

    def test_null_understat_ids_do_not_collide(self, db):
        """Unresolved players all have NULL; the partial index must allow that."""
        db.execute("INSERT INTO entity_map(fpl_player_id, understat_id) VALUES (1, NULL)")
        db.execute("INSERT INTO entity_map(fpl_player_id, understat_id) VALUES (2, NULL)")
        db.commit()
        assert db.execute("SELECT COUNT(*) c FROM entity_map").fetchone()["c"] == 2

    def test_hot_path_indices_exist(self, db):
        idx = {r["name"] for r in
               db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for expected in ("ix_player_gw_gw", "ix_player_gw_player",
                         "ix_fixtures_event", "ix_players_team", "ix_cache_tier"):
            assert expected in idx, expected
