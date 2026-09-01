"""Small helpers shared by the Streamlit pages.

Was a single module; now a package so the v2 components can live alongside the
original helpers without changing a single existing import. `boot()` keeps its
two-value signature -- all ten v1 pages unpack it that way, and breaking them is
a consolidation task, not a UI task. `boot_full()` is the additive v2 entry
point that also returns the data-quality envelope.
"""
from __future__ import annotations

import sqlite3

from ..config import Config, load_config
from ..db import connect, get_meta, init_db


def boot() -> tuple[Config, sqlite3.Connection]:
    cfg = load_config()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    return cfg, conn


def boot_full():
    """cfg, conn and a DataQuality snapshot. Used by the v2 decision pages."""
    from ..services.degrade import collect

    cfg, conn = boot()
    return cfg, conn, collect(conn)


def has_data(conn: sqlite3.Connection) -> bool:
    return get_meta(conn, "fpl_last_ingest") is not None
