"""SQLite persistence.

A single connection guarded by a lock. Statements are short, so they are run
directly from the event loop; WAL mode keeps readers and the writer apart.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    base_url    TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT 'cloud',
    enabled     INTEGER NOT NULL DEFAULT 1,
    cost_in     REAL NOT NULL DEFAULT 0,
    cost_out    REAL NOT NULL DEFAULT 0,
    options     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ladders (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tiers       TEXT NOT NULL DEFAULT '[]',
    options     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS profiles (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    personality   TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    skills        TEXT NOT NULL DEFAULT '[]',
    knowledge     TEXT NOT NULL DEFAULT '[]',
    keywords      TEXT NOT NULL DEFAULT '[]',
    ladder_id     TEXT NOT NULL DEFAULT '',
    coordinator   INTEGER NOT NULL DEFAULT 0,
    builtin       INTEGER NOT NULL DEFAULT 0,
    color         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    profile_id   TEXT NOT NULL,
    ladder_id    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    max_parallel INTEGER NOT NULL DEFAULT 2,
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',
    priority      INTEGER NOT NULL DEFAULT 3,
    agent_id      INTEGER,
    parent_id     INTEGER,
    depth         INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'api',
    source_ref    TEXT NOT NULL DEFAULT '{}',
    requester     TEXT NOT NULL DEFAULT '',
    thread_key    TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    model_id      TEXT NOT NULL DEFAULT '',
    confidence    INTEGER,
    needs_review  INTEGER NOT NULL DEFAULT 0,
    escalations   INTEGER NOT NULL DEFAULT 0,
    triage        TEXT NOT NULL DEFAULT '{}',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(thread_key);

CREATE TABLE IF NOT EXISTS task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);

CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    host         TEXT NOT NULL DEFAULT '',
    port         INTEGER NOT NULL DEFAULT 22,
    platform     TEXT NOT NULL,
    transport    TEXT NOT NULL DEFAULT 'ssh',
    username     TEXT NOT NULL DEFAULT '',
    password_enc TEXT NOT NULL DEFAULT '',
    enable_enc   TEXT NOT NULL DEFAULT '',
    site         TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',
    allow_config INTEGER NOT NULL DEFAULT 0,
    notes        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS change_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    agent_id    INTEGER,
    device_id   INTEGER NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    rationale   TEXT NOT NULL DEFAULT '',
    risk        TEXT NOT NULL DEFAULT 'medium',
    commands    TEXT NOT NULL,
    rollback    TEXT NOT NULL DEFAULT '',
    verify      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    output      TEXT NOT NULL DEFAULT '',
    backup      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    scope        TEXT NOT NULL DEFAULT 'tasks',
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);
"""

JSON_COLUMNS = {
    "options", "tiers", "skills", "knowledge", "keywords", "source_ref", "data", "triage",
}
BOOL_COLUMNS = {"enabled", "coordinator", "builtin", "allow_config", "revoked", "needs_review"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _decode(row: sqlite3.Row) -> dict:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in JSON_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        elif key in BOOL_COLUMNS and value is not None:
            value = bool(value)
        out[key] = value
    return out


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- low level -------------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [_decode(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return row[0] if row else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a statement; returns lastrowid for INSERTs, rowcount otherwise."""
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.lastrowid if sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount

    # -- helpers ---------------------------------------------------------
    def insert(self, table: str, values: dict) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", [_encode(v) for v in values.values()]
        )

    def update(self, table: str, key: Any, values: dict, key_col: str = "id") -> int:
        if not values:
            return 0
        sets = ", ".join(f"{c} = ?" for c in values)
        return self.execute(
            f"UPDATE {table} SET {sets} WHERE {key_col} = ?",
            [_encode(v) for v in values.values()] + [key],
        )

    def get(self, table: str, key: Any, key_col: str = "id") -> dict | None:
        return self.one(f"SELECT * FROM {table} WHERE {key_col} = ?", [key])

    def delete(self, table: str, key: Any, key_col: str = "id") -> int:
        return self.execute(f"DELETE FROM {table} WHERE {key_col} = ?", [key])

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        raw = self.scalar("SELECT value FROM settings WHERE key = ?", [key])
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, json.dumps(value)],
        )
