"""Local SQLite store. Holds ONLY dashboard-side decisions.

The Google Sheet is never written to by this application.
"""
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

from . import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS merges (
                alias_key   TEXT PRIMARY KEY,
                alias_name  TEXT NOT NULL,
                canonical   TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS overrides (
                cbv_key     TEXT PRIMARY KEY,
                action      TEXT NOT NULL CHECK (action IN ('include','exclude')),
                note        TEXT DEFAULT '',
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- name merges -----------------------------------------------------------

def get_merges() -> dict[str, str]:
    with db() as c:
        return {r["alias_key"]: r["canonical"] for r in c.execute("SELECT alias_key, canonical FROM merges")}


def set_merge(alias_key: str, alias_name: str, canonical: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO merges (alias_key, alias_name, canonical, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(alias_key) DO UPDATE SET canonical=excluded.canonical, created_at=excluded.created_at",
            (alias_key, alias_name, canonical, _now()),
        )


def remove_merge(alias_key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM merges WHERE alias_key = ?", (alias_key,))


# --- recommendation overrides ---------------------------------------------

def get_overrides() -> dict[str, dict]:
    with db() as c:
        return {
            r["cbv_key"]: {"action": r["action"], "note": r["note"], "created_at": r["created_at"]}
            for r in c.execute("SELECT * FROM overrides")
        }


def set_override(cbv_key: str, action: str, note: str = "") -> None:
    if action not in ("include", "exclude"):
        raise ValueError(action)
    with db() as c:
        c.execute(
            "INSERT INTO overrides (cbv_key, action, note, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(cbv_key) DO UPDATE SET action=excluded.action, note=excluded.note, created_at=excluded.created_at",
            (cbv_key, action, note, _now()),
        )


def remove_override(cbv_key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM overrides WHERE cbv_key = ?", (cbv_key,))


init()
