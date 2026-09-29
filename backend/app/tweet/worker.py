"""Single local worker for tweet runs.

This walker only moves a checked run through the state machine. The tweet
simulation loop lives in ``loop.py`` and is started by ``execute_loop``.
Keeping it off this path preserves runs that are persisted and then drained
without a model call. It does not replay an unknown attempt.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

from .db import ACTIVE_STATUSES, TERMINAL_STATUSES, connect

WORKER_ID = uuid.uuid4().hex
_thread_started = False
_thread_lock = threading.Lock()
LEASE_MS = 60_000
STEP_ORDER = ("queued", "preparing", "running", "reporting", "complete")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clock_ms(clock_ms=None) -> int:
    if clock_ms is not None:
        return int(clock_ms())
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def recover_stale_leases(now_ms: int | None = None) -> int:
    """Fail or degrade runs whose worker lease is gone. Never replay them."""

    current = _clock_ms(lambda: now_ms) if now_ms is not None else _clock_ms()
    recovered = 0
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""
            SELECT run_id FROM runs
            WHERE status IN ({",".join("?" for _ in ACTIVE_STATUSES)})
              AND (worker_lease_expires_ms IS NULL OR worker_lease_expires_ms <= ?)
            """,
            (*ACTIVE_STATUSES, current),
        ).fetchall()
        for row in rows:
            run_id = row["run_id"]
            inflight = conn.execute(
                """
                SELECT request_id FROM provider_requests
                WHERE run_id = ? AND status = 'in_flight'
                """,
                (run_id,),
            ).fetchall()
            for request in inflight:
                conn.execute(
                    """
                    UPDATE provider_requests
                    SET status = 'uncertain', result_known = 0,
                        error_status = 'lease_lost_not_replayed'
                    WHERE request_id = ?
                    """,
                    (request["request_id"],),
                )
            if inflight:
                status = "degraded"
                message = "工作进程租约已失效。已发送但结果未知的尝试已记为待核对，不会自动重放。"
            else:
                status = "failed"
                message = "工作进程租约已失效，任务没有继续执行，也不会自动重放。"
            now = _now_iso()
            conn.execute(
                """
                UPDATE runs
                SET status = ?, error_code = 'stale_lease', error_message = ?,
                    worker_lease_owner = NULL, worker_lease_expires_ms = NULL,
                    updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, message, now, now, run_id),
            )
            recovered += 1
    return recovered


def _active_foreign_lease(conn, now_ms: int) -> bool:
    row = conn.execute(
        f"""
        SELECT run_id FROM runs
        WHERE status IN ({",".join("?" for _ in ACTIVE_STATUSES)})
          AND worker_lease_owner IS NOT NULL
          AND worker_lease_owner != ?
          AND worker_lease_expires_ms > ?
        LIMIT 1
        """,
        (*ACTIVE_STATUSES, WORKER_ID, now_ms),
    ).fetchone()
    return row is not None


def advance_once(now_ms: int | None = None) -> bool:
    """Move at most one run by one state. Return whether any run changed."""

    current = _clock_ms(lambda: now_ms) if now_ms is not None else _clock_ms()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _active_foreign_lease(conn, current):
            return False
        owned = conn.execute(
            f"""
            SELECT * FROM runs
            WHERE status IN ({",".join("?" for _ in ACTIVE_STATUSES)})
              AND worker_lease_owner = ?
            ORDER BY updated_at
            LIMIT 1
            """,
            (*ACTIVE_STATUSES, WORKER_ID),
        ).fetchone()
        if owned is None:
            owned = conn.execute(
                """
                SELECT * FROM runs
                WHERE status = 'queued' AND cancel_requested = 0
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
            if owned is None:
                return False
        run_id = owned["run_id"]
        fresh = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if fresh["status"] in TERMINAL_STATUSES:
            return False
        now = _now_iso()
        expires = current + LEASE_MS
        if fresh["cancel_requested"]:
            conn.execute(
                """
                UPDATE runs
                SET status = 'cancelled', error_code = 'cancelled',
                    error_message = '已取消，未发出新的模型请求。',
                    worker_lease_owner = NULL, worker_lease_expires_ms = NULL,
                    updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (now, now, run_id),
            )
            return True
        try:
            index = STEP_ORDER.index(fresh["status"])
        except ValueError:
            return False
        if index >= len(STEP_ORDER) - 1:
            return False
        nxt = STEP_ORDER[index + 1]
        started = fresh["started_at"] or (now if nxt != "queued" else None)
        finished = now if nxt == "complete" else None
        lease_owner = None if nxt in TERMINAL_STATUSES else WORKER_ID
        lease_expires = None if nxt in TERMINAL_STATUSES else expires
        conn.execute(
            """
            UPDATE runs
            SET status = ?, started_at = ?, finished_at = ?,
                worker_lease_owner = ?, worker_lease_expires_ms = ?,
                updated_at = ?,
                error_code = CASE WHEN ? = 'complete' THEN NULL ELSE error_code END,
                error_message = CASE WHEN ? = 'complete' THEN NULL ELSE error_message END
            WHERE run_id = ?
            """,
            (nxt, started, finished, lease_owner, lease_expires, now, nxt, nxt, run_id),
        )
    return True


def drain(max_steps: int = 20, now_ms: int | None = None) -> int:
    moved = 0
    for _ in range(max_steps):
        if not advance_once(now_ms=now_ms):
            break
        moved += 1
    return moved


def _loop():
    while True:
        try:
            recover_stale_leases()
            if not advance_once():
                threading.Event().wait(0.2)
        except Exception:
            threading.Event().wait(0.5)


def start_background_worker() -> None:
    global _thread_started
    mode = os.environ.get("TWEET_WORKER_MODE", "manual").strip().lower()
    if mode != "thread":
        return
    with _thread_lock:
        if _thread_started:
            return
        thread = threading.Thread(target=_loop, name="tweet-worker", daemon=True)
        thread.start()
        _thread_started = True
