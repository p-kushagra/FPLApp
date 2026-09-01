"""Small helpers shared by the Streamlit pages."""
from __future__ import annotations

import sqlite3

from .config import Config, load_config
from .db import connect, get_meta, init_db


def boot() -> tuple[Config, sqlite3.Connection]:
    cfg = load_config()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    return cfg, conn


def has_data(conn: sqlite3.Connection) -> bool:
    return get_meta(conn, "fpl_last_ingest") is not None
