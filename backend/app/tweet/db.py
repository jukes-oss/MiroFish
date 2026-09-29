"""SQLite state for tweet runs.

The ledger records call counts and wall-clock time only. It has no dollar fields.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    author_context TEXT NOT NULL,
    audience_version TEXT NOT NULL,
    agent_count INTEGER NOT NULL,
    round_count INTEGER NOT NULL,
    execution_profile TEXT NOT NULL,
    subscription_cli TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_lease_owner TEXT,
    worker_lease_expires_ms INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    capability_json TEXT,
    wall_origin_ms INTEGER,
    deadline_ms INTEGER
);

CREATE TABLE IF NOT EXISTS exposures (
    exposure_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    round INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    exposure_id TEXT REFERENCES exposures(exposure_id),
    agent_id TEXT,
    round INTEGER,
    source TEXT,
    outcome TEXT,
    action TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_requests (
    request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    role TEXT,
    logical_batch TEXT,
    attempt_no INTEGER,
    channel TEXT,
    model_id TEXT,
    status TEXT NOT NULL,
    result_known INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_status TEXT,
    attempt_kind TEXT,
    day_key TEXT,
    exit_code INTEGER,
    http_status INTEGER,
    token_usage_status TEXT,
    queue_status TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    internal_attempts INTEGER,
    internal_accounting TEXT,
    diagnostics_json TEXT,
    deadline_ms INTEGER
);

CREATE TABLE IF NOT EXISTS gateway_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT,
    expires_ms INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO gateway_lock (id, owner, expires_ms) VALUES (1, '', 0);

CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    request_id TEXT REFERENCES provider_requests(request_id),
    entry_kind TEXT NOT NULL,
    call_delta INTEGER NOT NULL DEFAULT 0,
    wall_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    body_json TEXT,
    created_at TEXT NOT NULL
);
"""

ACTIVE_STATUSES = ("preparing", "running", "reporting")
TERMINAL_STATUSES = ("complete", "degraded", "failed", "cancelled")


def state_db_path() -> Path:
    override = os.environ.get("TWEET_STATE_DB", "").strip()
    if override:
        return Path(override)
    from ..config import Config

    return Path(Config.UPLOAD_FOLDER) / "state.sqlite"


def connect(path: Path | None = None) -> sqlite3.Connection:
    database = path or state_db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_ADDED_COLUMNS = {
    "runs": {
        "capability_json": "TEXT",
        "wall_origin_ms": "INTEGER",
        "deadline_ms": "INTEGER",
    },
    "provider_requests": {
        "attempt_kind": "TEXT",
        "day_key": "TEXT",
        "exit_code": "INTEGER",
        "http_status": "INTEGER",
        "token_usage_status": "TEXT",
        "queue_status": "TEXT",
        "truncated": "INTEGER NOT NULL DEFAULT 0",
        "internal_attempts": "INTEGER",
        "internal_accounting": "TEXT",
        "diagnostics_json": "TEXT",
        "deadline_ms": "INTEGER",
    },
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def init_db(path: Path | None = None) -> Path:
    database = path or state_db_path()
    with connect(database) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
    return database
