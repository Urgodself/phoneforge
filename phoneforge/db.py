"""SQLite ledger for phone numbers + their underlying provider accounts.

Schema is intentionally narrow:
- one row per *provisioned phone number*
- credentials embedded so `phoneforge wait` can log back in to read SMS
- `webgl_pair` is stored to keep fingerprint stable between signup and re-login;
  Camoufox fingerprint drift between sessions on the same TextNow account is
  a known detection signal.

Note: we trust filesystem ACLs + the .gitignore + chmod 600 .env for security
of credentials. SQLcipher would be cleaner but introduces a build dep that
isn't worth the friction for a v1 disposable-number tool.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS numbers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT    NOT NULL UNIQUE,
    provider        TEXT    NOT NULL,
    account_email   TEXT    NOT NULL,
    account_password TEXT   NOT NULL,
    identity_json   TEXT    NOT NULL DEFAULT '{}',
    webgl_vendor    TEXT    DEFAULT '',
    webgl_renderer  TEXT    DEFAULT '',
    proxy_str       TEXT    DEFAULT '',
    created_at      REAL    NOT NULL,
    last_used_at    REAL    DEFAULT NULL,
    used_for        TEXT    DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'active',
    notes           TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_numbers_status   ON numbers(status);
CREATE INDEX IF NOT EXISTS idx_numbers_used_for ON numbers(used_for);
CREATE INDEX IF NOT EXISTS idx_numbers_created  ON numbers(created_at DESC);

CREATE TABLE IF NOT EXISTS sms_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    number_id   INTEGER NOT NULL,
    received_at REAL    NOT NULL,
    raw_text    TEXT    NOT NULL,
    parsed_code TEXT    DEFAULT '',
    sender      TEXT    DEFAULT '',
    FOREIGN KEY (number_id) REFERENCES numbers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sms_log_number ON sms_log(number_id, received_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ensure_data_dir() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a connection with sane defaults (WAL, foreign keys, row_factory)."""
    _ensure_data_dir()
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init(db_path: Optional[Path] = None) -> Path:
    """Create schema + record version. Idempotent."""
    _ensure_data_dir()
    path = db_path or config.DB_PATH
    with connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
    return path


# ───────────────────────────── CRUD ─────────────────────────────


def insert_number(
    *,
    number: str,
    provider: str,
    account_email: str,
    account_password: str,
    identity: dict,
    webgl_vendor: str = "",
    webgl_renderer: str = "",
    proxy_str: str = "",
    used_for: str = "",
    notes: str = "",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO numbers
                (number, provider, account_email, account_password,
                 identity_json, webgl_vendor, webgl_renderer, proxy_str,
                 created_at, used_for, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                number,
                provider,
                account_email,
                account_password,
                json.dumps(identity, ensure_ascii=False),
                webgl_vendor,
                webgl_renderer,
                proxy_str,
                time.time(),
                used_for,
                notes,
            ),
        )
        return int(cur.lastrowid or 0)


def get_by_number(number: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM numbers WHERE number = ?", (number,))
        return cur.fetchone()


def list_all(status: Optional[str] = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            cur = conn.execute(
                "SELECT * FROM numbers WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cur = conn.execute("SELECT * FROM numbers ORDER BY created_at DESC")
        return list(cur.fetchall())


def mark_status(number: str, status: str, note: str = "") -> bool:
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE numbers
            SET status = ?,
                notes  = CASE WHEN ? = '' THEN notes ELSE notes || char(10) || ? END,
                last_used_at = ?
            WHERE number = ?
            """,
            (status, note, note, time.time(), number),
        )
        return cur.rowcount > 0


def touch_last_used(number: str, used_for: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE numbers SET last_used_at = ?, used_for = COALESCE(NULLIF(?, ''), used_for) WHERE number = ?",
            (time.time(), used_for, number),
        )


def log_sms(number_id: int, raw_text: str, parsed_code: str = "", sender: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_log (number_id, received_at, raw_text, parsed_code, sender)
            VALUES (?, ?, ?, ?, ?)
            """,
            (number_id, time.time(), raw_text, parsed_code, sender),
        )
        return int(cur.lastrowid or 0)
