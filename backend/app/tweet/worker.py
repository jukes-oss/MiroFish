"""Single local worker for tweet runs.

A queued run is claimed and then ``execute_loop`` runs it. There is no
second path that only walks status. An owned active run is not restarted,
and a lost lease is never replayed.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone

from .db import ACTIVE_STATUSES, connect

logger = logging.getLogger("mirofish.tweet_worker")

WORKER_ID = uuid.uuid4().hex
_thread_started = False
_thread_lock = threading.Lock()
LEASE_MS = 60_000
CALL_HOLD_MS = 120_000


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


class _FixedClock:
    def __init__(self, now_ms: int):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def day_key(self) -> str:
        moment = datetime.fromtimestamp(self.now_ms / 1000, tz=timezone.utc).astimezone()
        return moment.strftime("%Y-%m-%d")


def refresh_lease(run_id: str, now_ms: int, hold_ms: int = LEASE_MS) -> None:
    """Extend this worker's lease. A foreign owner is left untouched."""

    with connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET worker_lease_expires_ms = ?, updated_at = ?
            WHERE run_id = ? AND worker_lease_owner = ?
              AND status IN ({})
            """.format(",".join("?" for _ in ACTIVE_STATUSES)),
            (int(now_ms) + int(hold_ms), _now_iso(), run_id, WORKER_ID, *ACTIVE_STATUSES),
        )


def advance_once(now_ms: int | None = None) -> bool:
    """Claim one queued run and execute its loop. Do not only walk status."""

    current = _clock_ms(lambda: now_ms) if now_ms is not None else _clock_ms()
    clock = _FixedClock(current) if now_ms is not None else None
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _active_foreign_lease(conn, current):
            return False
        owned = conn.execute(
            f"""
            SELECT run_id FROM runs
            WHERE status IN ({",".join("?" for _ in ACTIVE_STATUSES)})
              AND worker_lease_owner = ?
            LIMIT 1
            """,
            (*ACTIVE_STATUSES, WORKER_ID),
        ).fetchone()
        if owned is not None:
            return False
        queued = conn.execute(
            """
            SELECT run_id FROM runs
            WHERE status = 'queued' AND cancel_requested = 0
            ORDER BY created_at
            LIMIT 1
            """
        ).fetchone()
        if queued is None:
            return False
        run_id = queued["run_id"]
        now = _now_iso()
        updated = conn.execute(
            """
            UPDATE runs
            SET status = 'preparing', started_at = ?, updated_at = ?,
                worker_lease_owner = ?, worker_lease_expires_ms = ?
            WHERE run_id = ? AND status = 'queued' AND cancel_requested = 0
            """,
            (now, now, WORKER_ID, current + LEASE_MS + CALL_HOLD_MS, run_id),
        )
        if updated.rowcount != 1:
            return False
    from .loop import execute_loop

    def beat():
        stamp = clock() if clock is not None else _clock_ms()
        refresh_lease(run_id, stamp, LEASE_MS + CALL_HOLD_MS)

    try:
        execute_loop(run_id, seed=1, clock=clock, on_step=beat)
    except Exception:
        logger.info("tweet worker failed code=worker_exception")
        from .report import write_closed_report

        write_closed_report(run_id, "工作进程中断，没有自动重放，也没有编造报告正文。")
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
