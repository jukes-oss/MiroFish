"""One gateway for generation, repair, and capability probes.

A call is admitted only inside a SQLite transaction that reserves a slot and
checks the run cap, the shared day cap, the logical-batch attempt limit, and
the frozen deadline. The model call itself is serialized to concurrency 1.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..tweet.db import TERMINAL_STATUSES, connect
from . import registry
from .adapters import (
    AdapterOutcome,
    invoke_ollama,
    invoke_subscription_cli,
    messages_to_prompt,
)
from .limits import (
    CLEANUP_RESERVE_SECONDS,
    COUNTED_STATUSES,
    DAY_MAX_REQUESTS,
    MAX_ATTEMPTS_PER_BATCH,
    MAX_OUTPUT_BYTES,
    PHYSICAL_STATUSES,
    REPORT_RESERVE_BATCH,
    REPORT_RESERVE_CALLS,
    REPORT_RESERVE_WALL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    ROLES,
    RULE_ROLES,
    RUN_MAX_REQUESTS,
    RUN_MAX_WALL_SECONDS,
)
from .profile import load_profile
from .redact import install_redaction, push_sensitive, reset_sensitive

logger = logging.getLogger("mirofish.gateway")
install_redaction(logger)

_MODEL_LOCK = threading.Lock()
_OWNER = uuid.uuid4().hex


@dataclass
class GenerateResult:
    admitted: bool
    sent: bool
    request_id: str | None
    status: str
    error_code: str | None
    error_message: str | None
    channel: str | None = None
    model_id: str | None = None
    adapter: str | None = None
    text: str | None = None
    http_status: int | None = None
    exit_code: int | None = None
    token_usage_status: str | None = None
    queue_status: str | None = None
    truncated: bool = False
    internal_attempts: int | None = None
    internal_accounting: str | None = None
    attempt_no: int = 0
    json_ok: bool | None = None


def _now_ms(clock) -> int:
    if clock is None:
        return int(time.time() * 1000)
    if callable(clock):
        return int(clock())
    return int(clock.now_ms)


def _day_key(now_ms: int, clock) -> str:
    if clock is not None and hasattr(clock, "day_key"):
        return str(clock.day_key())
    moment = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone()
    return moment.strftime("%Y-%m-%d")


def _iso(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat()


def _counted(conn, *, run_id: str | None = None, day_key: str | None = None) -> int:
    clauses = ["status IN (%s)" % ",".join("?" for _ in COUNTED_STATUSES)]
    params: list = list(COUNTED_STATUSES)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if day_key is not None:
        clauses.append("day_key = ?")
        params.append(day_key)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM provider_requests WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["n"])


def _ledger(conn, run_id: str, request_id: str | None, kind: str, call_delta: int, wall_ms: int, now_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO budget_ledger (
            entry_id, run_id, request_id, entry_kind, call_delta, wall_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uuid.uuid4().hex, run_id, request_id, kind, call_delta, wall_ms, _iso(now_ms)),
    )


def _reject(code: str, message: str, *, channel: str | None = None, adapter: str | None = None) -> GenerateResult:
    logger.info("gateway reject code=%s channel=%s adapter=%s", code, channel, adapter)
    return GenerateResult(
        admitted=False,
        sent=False,
        request_id=None,
        status="rejected",
        error_code=code,
        error_message=message,
        channel=channel,
        adapter=adapter,
    )


def prepare_run(run_id: str, *, clock=None) -> dict:
    """Freeze the deadline and reserve the report slots once."""

    now_ms = _now_ms(clock)
    day = _day_key(now_ms, clock)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        profile = load_profile(run["capability_json"])
        if profile is None:
            raise RuntimeError("运行缺少已冻结的能力档案。")
        if run["deadline_ms"] is None:
            conn.execute(
                """
                UPDATE runs
                SET wall_origin_ms = ?, deadline_ms = ?, updated_at = ?
                WHERE run_id = ? AND deadline_ms IS NULL
                """,
                (now_ms, now_ms + RUN_MAX_WALL_SECONDS * 1000, _iso(now_ms), run_id),
            )
        existing = conn.execute(
            """
            SELECT COUNT(*) AS n FROM provider_requests
            WHERE run_id = ? AND logical_batch = ? AND status != 'released'
            """,
            (run_id, REPORT_RESERVE_BATCH),
        ).fetchone()["n"]
        missing = REPORT_RESERVE_CALLS - int(existing)
        if missing > 0:
            run_total = _counted(conn, run_id=run_id)
            day_total = _counted(conn, day_key=day)
            if run_total + missing > RUN_MAX_REQUESTS or day_total + missing > DAY_MAX_REQUESTS:
                conn.commit()
                return {"prepared": False, "error_code": "report_reserve_cap"}
            for _ in range(missing):
                request_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO provider_requests (
                        request_id, run_id, role, logical_batch, attempt_no, attempt_kind,
                        channel, model_id, status, result_known, created_at, day_key,
                        error_status
                    ) VALUES (?, ?, 'report', ?, 0, 'report_reserve', NULL, NULL,
                              'reserved', 0, ?, ?, NULL)
                    """,
                    (request_id, run_id, REPORT_RESERVE_BATCH, _iso(now_ms), day),
                )
                _ledger(conn, run_id, request_id, "call_reserve", 1, 0, now_ms)
        conn.commit()
        fresh = conn.execute(
            "SELECT deadline_ms, wall_origin_ms, capability_json FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return {
        "prepared": True,
        "deadline_ms": fresh["deadline_ms"],
        "wall_origin_ms": fresh["wall_origin_ms"],
        "profile": load_profile(fresh["capability_json"]),
    }


def _route(profile: dict, role: str) -> dict:
    route = profile.get("routes", {}).get(role)
    if not isinstance(route, dict):
        raise KeyError(role)
    return route


def _timeout_seconds(now_ms: int, deadline_ms: int, *, for_report: bool, requested: float | None) -> float | None:
    cleanup_ms = CLEANUP_RESERVE_SECONDS * 1000
    if for_report:
        remain_ms = deadline_ms - cleanup_ms - now_ms
        window_ms = REPORT_RESERVE_WALL_SECONDS * 1000
    else:
        remain_ms = deadline_ms - (REPORT_RESERVE_WALL_SECONDS * 1000) - cleanup_ms - now_ms
        window_ms = remain_ms
    if remain_ms <= 0:
        return None
    seconds = min(REQUEST_TIMEOUT_SECONDS, remain_ms / 1000, window_ms / 1000)
    if requested is not None:
        seconds = min(seconds, requested)
    if seconds <= 0:
        return None
    return seconds


def _release(conn, request_id: str, run_id: str, now_ms: int, code: str) -> None:
    updated = conn.execute(
        """
        UPDATE provider_requests
        SET status = 'released', result_known = 1, error_status = ?, finished_at = ?
        WHERE request_id = ? AND status IN ('reserved', 'in_flight')
        """,
        (code, _iso(now_ms), request_id),
    )
    if updated.rowcount:
        _ledger(conn, run_id, request_id, "call_release", -1, 0, now_ms)


def generate(
    run_id: str,
    *,
    role: str,
    logical_batch: str,
    messages: list[dict],
    kind: str = "generation",
    clock=None,
    timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
) -> GenerateResult:
    if role in RULE_ROLES:
        return _reject("rules_only", "主持人是规则角色，不会调用模型。")
    if role not in ROLES:
        return _reject("unknown_role", "未知角色，不会调用模型。")
    if kind not in ("generation", "repair", "probe"):
        return _reject("unknown_kind", "未知调用类型。")

    sensitive = [str(item.get("content") or "") for item in messages]
    token = push_sensitive(*sensitive)
    try:
        return _generate(
            run_id,
            role=role,
            logical_batch=logical_batch,
            messages=messages,
            kind=kind,
            clock=clock,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes or MAX_OUTPUT_BYTES,
        )
    finally:
        reset_sensitive(token)


def _generate(
    run_id: str,
    *,
    role: str,
    logical_batch: str,
    messages: list[dict],
    kind: str,
    clock,
    timeout_seconds: float | None,
    max_output_bytes: int,
) -> GenerateResult:
    prepared = prepare_run(run_id, clock=clock)
    if not prepared.get("prepared"):
        return _reject("day_cap", "本机今日共用调用次数已达到上限，报告预留无法占用。")
    now_ms = _now_ms(clock)
    day = _day_key(now_ms, clock)
    profile = prepared["profile"]
    try:
        route = _route(profile, role)
    except KeyError:
        return _reject("unknown_role", "未知角色，不会调用模型。")
    for_report = role == "report"
    timeout = _timeout_seconds(now_ms, int(prepared["deadline_ms"]), for_report=for_report, requested=timeout_seconds)
    channel = route["channel"]
    adapter_name = route["adapter"]
    model_id = route.get("model_id")

    if timeout is None:
        if for_report:
            return _reject("deadline", "已到达运行截止时间，不会再发送新请求。", channel=channel, adapter=adapter_name)
        return _reject(
            "report_wall_reserved",
            "剩余时间已留给报告，这一步不再发送请求。",
            channel=channel,
            adapter=adapter_name,
        )

    request_id = None
    converted = False
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            return _reject("not_found", "找不到这个运行。")
        if run["status"] in TERMINAL_STATUSES or run["cancel_requested"]:
            return _reject("cancelled", "运行已取消，不会再发送新请求。", channel=channel, adapter=adapter_name)
        deadline_ms = int(run["deadline_ms"])
        timeout = _timeout_seconds(now_ms, deadline_ms, for_report=for_report, requested=timeout_seconds)
        if timeout is None:
            code = "deadline" if for_report else "report_wall_reserved"
            message = "已到达运行截止时间，不会再发送新请求。" if for_report else "剩余时间已留给报告，这一步不再发送请求。"
            return _reject(code, message, channel=channel, adapter=adapter_name)
        batch_attempts = conn.execute(
            """
            SELECT COUNT(*) AS n FROM provider_requests
            WHERE run_id = ? AND logical_batch = ? AND status != 'released'
            """,
            (run_id, logical_batch),
        ).fetchone()["n"]
        if int(batch_attempts) >= MAX_ATTEMPTS_PER_BATCH:
            return _reject(
                "batch_attempt_limit",
                "同一逻辑批次最多两次尝试，第三次已被拒绝。",
                channel=channel,
                adapter=adapter_name,
            )
        attempt_no = int(batch_attempts) + 1
        if for_report:
            reserve = conn.execute(
                """
                SELECT request_id FROM provider_requests
                WHERE run_id = ? AND logical_batch = ? AND status = 'reserved'
                ORDER BY created_at LIMIT 1
                """,
                (run_id, REPORT_RESERVE_BATCH),
            ).fetchone()
            if reserve is not None:
                request_id = reserve["request_id"]
                conn.execute(
                    """
                    UPDATE provider_requests
                    SET logical_batch = ?, attempt_no = ?, attempt_kind = ?, role = ?,
                        channel = ?, model_id = ?
                    WHERE request_id = ? AND status = 'reserved'
                    """,
                    (logical_batch, attempt_no, kind, role, channel, model_id, request_id),
                )
                _ledger(conn, run_id, request_id, "call_convert", 0, 0, now_ms)
                converted = True
        if request_id is None:
            if _counted(conn, run_id=run_id) + 1 > RUN_MAX_REQUESTS:
                return _reject("run_call_cap", "本次运行的调用次数已达到上限 10，不能再发送请求。", channel=channel, adapter=adapter_name)
            if _counted(conn, day_key=day) + 1 > DAY_MAX_REQUESTS:
                return _reject("day_cap", "本机今日共用调用次数已达到上限 50，不能再发送请求。", channel=channel, adapter=adapter_name)
            request_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO provider_requests (
                    request_id, run_id, role, logical_batch, attempt_no, attempt_kind,
                    channel, model_id, status, result_known, created_at, day_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 0, ?, ?)
                """,
                (request_id, run_id, role, logical_batch, attempt_no, kind, channel, model_id, _iso(now_ms), day),
            )
            _ledger(conn, run_id, request_id, "call_reserve", 1, 0, now_ms)
        conn.commit()

    assert request_id is not None
    acquired = _MODEL_LOCK.acquire(timeout=timeout)
    if not acquired:
        _release_unsent(request_id, run_id, _now_ms(clock), "not_sent")
        return _reject("concurrency", "模型执行队列在截止前没有排到，本次没有发送。", channel=channel, adapter=adapter_name)
    process_box: dict = {}
    try:
        now_ms = _now_ms(clock)
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            current = conn.execute(
                "SELECT status FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if run is None or current is None or current["status"] != "reserved":
                return _reject("not_sent", "预留已经失效，不会发送。", channel=channel, adapter=adapter_name)
            if run["status"] in TERMINAL_STATUSES or run["cancel_requested"]:
                _release(conn, request_id, run_id, now_ms, "cancelled_before_send")
                conn.commit()
                return _reject("cancelled", "运行已取消，不会再发送新请求。", channel=channel, adapter=adapter_name)
            timeout = _timeout_seconds(now_ms, int(run["deadline_ms"]), for_report=for_report, requested=timeout_seconds)
            if timeout is None:
                _release(conn, request_id, run_id, now_ms, "deadline_before_send")
                conn.commit()
                code = "deadline" if for_report else "report_wall_reserved"
                message = "已到达运行截止时间，不会再发送新请求。" if for_report else "剩余时间已留给报告，这一步不再发送请求。"
                return _reject(code, message, channel=channel, adapter=adapter_name)
            locked = _acquire_db_lock(conn, now_ms, int(timeout * 1000) + 5000)
            if not locked:
                _release(conn, request_id, run_id, now_ms, "concurrency_before_send")
                conn.commit()
                return _reject("concurrency", "已有模型调用在执行，本次没有发送。", channel=channel, adapter=adapter_name)
            inflight = conn.execute(
                "SELECT COUNT(*) AS n FROM provider_requests WHERE status = 'in_flight'"
            ).fetchone()["n"]
            if int(inflight) != 0:
                _release_db_lock(conn)
                _release(conn, request_id, run_id, now_ms, "concurrency_before_send")
                conn.commit()
                return _reject("concurrency", "已有模型调用在执行，本次没有发送。", channel=channel, adapter=adapter_name)
            conn.execute(
                """
                UPDATE provider_requests
                SET status = 'in_flight', started_at = ?, deadline_ms = ?
                WHERE request_id = ? AND status = 'reserved'
                """,
                (_iso(now_ms), now_ms + int(timeout * 1000), request_id),
            )
            conn.commit()
        started_ms = now_ms

        def cancel_check() -> bool:
            with connect() as conn:
                row = conn.execute(
                    "SELECT cancel_requested, status FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            return row is None or bool(row["cancel_requested"]) or row["status"] in TERMINAL_STATUSES

        def register_process(process) -> None:
            process_box["process"] = process
            registry.register(run_id, process)

        try:
            if adapter_name == "subscription_cli":
                outcome = invoke_subscription_cli(
                    executable=route["executable"],
                    prompt=messages_to_prompt(messages),
                    timeout=timeout,
                    cancel_check=cancel_check,
                    register_process=register_process,
                    max_output_bytes=max_output_bytes,
                )
            elif adapter_name == "ollama":
                outcome = invoke_ollama(
                    base_url=route["base_url"],
                    model=str(model_id or ""),
                    messages=messages,
                    timeout=timeout,
                    max_output_bytes=max_output_bytes,
                )
            else:
                outcome = AdapterOutcome(started=False, error_code="unknown_adapter")
        except Exception:
            logger.info("adapter failed type=unexpected channel=%s", channel)
            outcome = AdapterOutcome(started=True, error_code="adapter_error")
        finished_ms = _now_ms(clock)
        _finalize(
            run_id,
            request_id,
            outcome,
            started_ms=started_ms,
            finished_ms=finished_ms,
            profile=profile,
        )
        return _result_from_row(request_id, outcome, attempt_no, channel, model_id, adapter_name)
    finally:
        if process_box.get("process") is not None:
            registry.clear(run_id, process_box["process"])
        _MODEL_LOCK.release()
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _release_db_lock(conn)
            conn.commit()


def _acquire_db_lock(conn, now_ms: int, hold_ms: int) -> bool:
    conn.execute(
        "INSERT OR IGNORE INTO gateway_lock (id, owner, expires_ms) VALUES (1, '', 0)"
    )
    updated = conn.execute(
        """
        UPDATE gateway_lock
        SET owner = ?, expires_ms = ?
        WHERE id = 1 AND (owner = ? OR owner = '' OR expires_ms <= ?)
        """,
        (_OWNER, now_ms + hold_ms, _OWNER, now_ms),
    )
    return updated.rowcount == 1


def _release_db_lock(conn) -> None:
    conn.execute(
        "UPDATE gateway_lock SET owner = '', expires_ms = 0 WHERE id = 1 AND owner = ?",
        (_OWNER,),
    )


def _release_unsent(request_id: str, run_id: str, now_ms: int, code: str) -> None:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _release(conn, request_id, run_id, now_ms, code)
        conn.commit()


def _finalize(
    run_id: str,
    request_id: str,
    outcome: AdapterOutcome,
    *,
    started_ms: int,
    finished_ms: int,
    profile: dict,
) -> None:
    if not outcome.started:
        _release_unsent(request_id, run_id, finished_ms, outcome.error_code or "not_sent")
        return
    if outcome.error_code in {"timeout", "cancelled"}:
        status = "uncertain"
        result_known = 0
    elif outcome.error_code:
        status = "failed"
        result_known = 1
    else:
        status = "completed"
        result_known = 1
    internal = outcome.internal_attempts
    if internal is None:
        accounting = "待验"
    else:
        accounting = "reported"
    diagnostics = {
        "error_code": outcome.error_code,
        "token_usage_status": outcome.token_usage_status,
        "queue_status": outcome.queue_status,
        "truncated": outcome.truncated,
        "internal_attempts": internal,
        "internal_accounting": accounting,
        "model_id_reported": outcome.model_id_reported,
        "silent_fallback": False,
    }
    wall_ms = max(0, finished_ms - started_ms)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE provider_requests
            SET status = ?, result_known = ?, finished_at = ?, exit_code = ?,
                http_status = ?, error_status = ?, token_usage_status = ?,
                queue_status = ?, truncated = ?, internal_attempts = ?,
                internal_accounting = ?, diagnostics_json = ?
            WHERE request_id = ? AND status = 'in_flight'
            """,
            (
                status,
                result_known,
                _iso(finished_ms),
                outcome.exit_code,
                outcome.http_status,
                outcome.error_code,
                outcome.token_usage_status,
                outcome.queue_status,
                1 if outcome.truncated else 0,
                internal,
                accounting,
                json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
                request_id,
            ),
        )
        if updated.rowcount:
            _ledger(conn, run_id, request_id, "wall_observed", 0, wall_ms, finished_ms)
            if internal is not None and internal > 1:
                _record_internal_extras(conn, run_id, request_id, internal - 1, finished_ms, profile)
        conn.commit()
    logger.info(
        "gateway finish role_batch status=%s error=%s channel_accounted=%s",
        status,
        outcome.error_code,
        accounting,
    )


def _record_internal_extras(conn, run_id: str, parent_id: str, extra: int, now_ms: int, profile: dict) -> None:
    parent = conn.execute("SELECT * FROM provider_requests WHERE request_id = ?", (parent_id,)).fetchone()
    day = parent["day_key"]
    for _ in range(extra):
        if _counted(conn, run_id=run_id) >= RUN_MAX_REQUESTS or _counted(conn, day_key=day) >= DAY_MAX_REQUESTS:
            conn.execute(
                """
                UPDATE provider_requests
                SET error_status = 'internal_over_cap'
                WHERE request_id = ?
                """,
                (parent_id,),
            )
            return
        request_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO provider_requests (
                request_id, run_id, role, logical_batch, attempt_no, attempt_kind,
                channel, model_id, status, result_known, created_at, finished_at,
                day_key, error_status, internal_accounting
            ) VALUES (?, ?, ?, ?, 0, 'internal_extra', ?, ?, 'uncertain', 0, ?, ?, ?, ?, 'reported')
            """,
            (
                request_id,
                run_id,
                parent["role"],
                parent["logical_batch"] + ":internal",
                parent["channel"],
                parent["model_id"],
                _iso(now_ms),
                _iso(now_ms),
                day,
                "internal_extra",
            ),
        )
        _ledger(conn, run_id, request_id, "call_reserve", 1, 0, now_ms)


def _result_from_row(
    request_id: str,
    outcome: AdapterOutcome,
    attempt_no: int,
    channel: str,
    model_id: str | None,
    adapter_name: str,
) -> GenerateResult:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM provider_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if row is None or row["status"] == "released":
        return GenerateResult(
            admitted=False,
            sent=False,
            request_id=request_id,
            status="released",
            error_code=outcome.error_code or "not_sent",
            error_message="请求没有发出。",
            channel=channel,
            model_id=model_id,
            adapter=adapter_name,
            attempt_no=attempt_no,
        )
    sent = row["status"] in PHYSICAL_STATUSES
    return GenerateResult(
        admitted=True,
        sent=sent,
        request_id=request_id,
        status=row["status"],
        error_code=row["error_status"],
        error_message=_message_for(row["error_status"], row["status"]),
        channel=row["channel"],
        model_id=row["model_id"],
        adapter=adapter_name,
        text=outcome.text if sent else None,
        http_status=row["http_status"],
        exit_code=row["exit_code"],
        token_usage_status=row["token_usage_status"],
        queue_status=row["queue_status"],
        truncated=bool(row["truncated"]),
        internal_attempts=row["internal_attempts"],
        internal_accounting=row["internal_accounting"],
        attempt_no=attempt_no,
    )


def _message_for(error_code: str | None, status: str) -> str:
    messages = {
        "rate_limited": "通道返回 429 或限流，本次尝试已结束，不会静默更换通道。",
        "timeout": "本次等待已到截止时间，结果未知，不会自动重放。",
        "cancelled": "运行已取消。已发出的尝试仍计入次数，不会再发新请求。",
        "cli_exit": "订阅 CLI 以非零状态退出，不会静默更换通道。",
        "http_error": "Ollama 返回错误，不会静默更换通道。",
        "truncated": "输出达到字节上限并被截断，没有拆成更多请求。",
        "internal_over_cap": "CLI 报告的内部额外尝试已经触及调用上限，后续请求停止。",
        "adapter_error": "通道调用失败，不会静默更换通道。",
    }
    if error_code in messages:
        return messages[error_code]
    if status == "completed":
        return "调用已完成。"
    if status == "uncertain":
        return "调用结果未知，已计入次数，不会自动重放。"
    return "调用已结束。"


def generate_structured(
    run_id: str,
    *,
    role: str,
    logical_batch: str,
    messages: list[dict],
    clock=None,
    timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
) -> GenerateResult:
    """Send one batch and, only for invalid JSON, one same-channel repair."""

    first = generate(
        run_id,
        role=role,
        logical_batch=logical_batch,
        messages=messages,
        kind="generation",
        clock=clock,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    first.json_ok = _json_object(first.text)
    if not first.sent or first.status != "completed" or first.json_ok:
        return first
    repair = generate(
        run_id,
        role=role,
        logical_batch=logical_batch,
        messages=messages + [{
            "role": "user",
            "content": "上一次输出不是合法 JSON 对象。请只返回一个 JSON 对象。",
        }],
        kind="repair",
        clock=clock,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    repair.json_ok = _json_object(repair.text)
    return repair


def _json_object(text: str | None) -> bool:
    if not text:
        return False
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(loaded, dict)


def apply_late_result(request_id: str, text: str) -> bool:
    """A late success may not overwrite a terminal attempt. Text is not logged."""

    del text
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE provider_requests
            SET status = 'completed', result_known = 1, error_status = NULL
            WHERE request_id = ? AND status = 'in_flight'
            """,
            (request_id,),
        )
        changed = updated.rowcount == 1
        if changed:
            conn.commit()
        else:
            conn.rollback()
    logger.info("late result applied=%s", changed)
    return changed


def usage(run_id: str, *, clock=None) -> dict:
    now_ms = _now_ms(clock)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM provider_requests
            WHERE run_id = ? GROUP BY status
            """,
            (run_id,),
        ).fetchall()
        run = conn.execute(
            "SELECT wall_origin_ms, deadline_ms FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        ledger = conn.execute(
            "SELECT COALESCE(SUM(call_delta), 0) AS calls FROM budget_ledger WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(budget_ledger)")]
    counts = {row["status"]: int(row["n"]) for row in rows}
    physical = sum(counts.get(status, 0) for status in PHYSICAL_STATUSES)
    reserved = counts.get("reserved", 0)
    origin = run["wall_origin_ms"] if run and run["wall_origin_ms"] is not None else now_ms
    return {
        "physical": physical,
        "reserved": reserved,
        "uncertain": counts.get("uncertain", 0),
        "counted": physical + reserved,
        "ledger_calls": int(ledger["calls"]),
        "wall_elapsed_ms": max(0, now_ms - int(origin)),
        "deadline_ms": run["deadline_ms"] if run else None,
        "dollar_columns": [name for name in columns if "usd" in name or "dollar" in name or name == "currency"],
    }
