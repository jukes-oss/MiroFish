"""Create, read, cancel, and delete persisted tweet runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from ..providers.profile import build_capability_profile, dump_profile
from .channels import channel_config, channel_error_message, channel_problems
from .db import ACTIVE_STATUSES, TERMINAL_STATUSES, connect, state_db_path

ALLOWED_AUDIENCES = ("zh_x_v1",)
DEFAULT_AGENTS = 120
MAX_AGENTS = 240
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 4
MAX_DRAFT_CODE_POINTS = 2000
MAX_AUTHOR_CODE_POINTS = 500
ALLOWED_PROFILES = ("mixed", "subscription-only")
REPORT_NOT_GENERATED = "运行已保存并通过配置检查。本阶段不生成人设、反应或报告。"


class TweetRunError(Exception):
    def __init__(self, message: str, status_code: int, error_code: str):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_int(value, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TweetRunError("人数或波次必须是整数。", 400, "validation") from exc
    return min(high, max(low, number))


def normalize_input(payload: dict, channels: dict | None = None) -> dict:
    if not isinstance(payload, dict):
        raise TweetRunError("请求体必须是 JSON 对象。", 400, "validation")
    draft = payload.get("draft_text")
    if not isinstance(draft, str) or not (1 <= len(draft) <= MAX_DRAFT_CODE_POINTS):
        raise TweetRunError(
            "草稿长度必须在 1 到 2000 个 Unicode 码点之间。",
            400,
            "validation",
        )
    author = payload.get("author_context", "")
    if author is None:
        author = ""
    if not isinstance(author, str) or len(author) > MAX_AUTHOR_CODE_POINTS:
        raise TweetRunError(
            "作者背景不能超过 500 个 Unicode 码点。",
            400,
            "validation",
        )
    audience = payload.get("audience_version") or ALLOWED_AUDIENCES[0]
    if audience not in ALLOWED_AUDIENCES:
        raise TweetRunError(
            "受众模板不可用。当前只接受 zh_x_v1。",
            400,
            "validation",
        )
    profile = payload.get("execution_profile") or "mixed"
    if profile not in ALLOWED_PROFILES:
        raise TweetRunError(
            "execution_profile 只能是 mixed 或 subscription-only。",
            400,
            "validation",
        )
    selected = channels or channel_config()
    return {
        "draft_text": draft,
        "author_context": author,
        "audience_version": audience,
        "agent_count": _clamp_int(payload.get("agent_count"), DEFAULT_AGENTS, 1, MAX_AGENTS),
        "round_count": _clamp_int(payload.get("round_count"), DEFAULT_ROUNDS, 1, MAX_ROUNDS),
        "execution_profile": profile,
        "subscription_cli": selected["subscription_cli"],
    }


def request_hash(normalized: dict) -> str:
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _row_to_public(row) -> dict:
    return {
        "run_id": row["run_id"],
        "status": row["status"],
        "draft_text": row["draft_text"],
        "author_context": row["author_context"],
        "audience_version": row["audience_version"],
        "agent_count": row["agent_count"],
        "round_count": row["round_count"],
        "execution_profile": row["execution_profile"],
        "subscription_cli": row["subscription_cli"],
        "schema_version": row["schema_version"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def create_run(payload: dict, *, idempotency_key: str) -> tuple[dict, bool]:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise TweetRunError("缺少有效的幂等键 idempotency_key。", 400, "validation")
    problems = channel_problems()
    if problems:
        raise TweetRunError(channel_error_message(problems), 400, "channel_config")
    normalized = normalize_input(payload)
    selected = channel_config()
    profile = build_capability_profile(
        execution_profile=normalized["execution_profile"],
        subscription_cli=normalized["subscription_cli"],
        cli_path=selected["cli_path"],
        ollama_base_url=selected["ollama_base_url"],
        ollama_model=selected["ollama_model"],
    )
    digest = request_hash(normalized)
    now = _now()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM runs WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != digest:
                raise TweetRunError(
                    "这个幂等键已经用于不同的输入，不能重复创建。",
                    409,
                    "idempotency_conflict",
                )
            return _row_to_public(existing), False
        run_id = uuid.uuid4().hex
        try:
            conn.execute(
            """
            INSERT INTO runs (
                run_id, idempotency_key, request_hash, status, draft_text,
                author_context, audience_version, agent_count, round_count,
                execution_profile, subscription_cli, schema_version,
                cancel_requested, created_at, updated_at, capability_json
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, '2.0', 0, ?, ?, ?)
            """,
            (
                run_id,
                key,
                digest,
                normalized["draft_text"],
                normalized["author_context"],
                normalized["audience_version"],
                normalized["agent_count"],
                normalized["round_count"],
                normalized["execution_profile"],
                normalized["subscription_cli"],
                now,
                now,
                dump_profile(profile),
            ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is None or existing["request_hash"] != digest:
                raise TweetRunError(
                    "这个幂等键已经用于不同的输入，不能重复创建。",
                    409,
                    "idempotency_conflict",
                )
            return _row_to_public(existing), False
        created = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_public(created), True


def get_run(run_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise TweetRunError("找不到这个运行。", 404, "not_found")
    return _row_to_public(row)


def list_runs(limit: int = 50) -> list[dict]:
    bounded = min(100, max(1, int(limit)))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, status, audience_version, agent_count, round_count,
                   execution_profile, created_at, finished_at, error_message,
                   substr(draft_text, 1, 40) AS draft_preview
            FROM runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (bounded,),
        ).fetchall()
    return [dict(row) for row in rows]


def action_count(run_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM actions WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return int(row["n"])


def provider_request_count(run_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM provider_requests WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return int(row["n"])


def cancel_run(run_id: str) -> dict:
    from ..providers.registry import terminate_run

    now = _now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise TweetRunError("找不到这个运行。", 404, "not_found")
        if row["status"] in TERMINAL_STATUSES:
            public = _row_to_public(row)
        elif row["status"] == "queued":
            conn.execute(
                """
                UPDATE runs
                SET status = 'cancelled', cancel_requested = 1, updated_at = ?,
                    finished_at = ?, error_code = 'cancelled',
                    error_message = '已取消，未发出新的模型请求。'
                WHERE run_id = ?
                """,
                (now, now, run_id),
            )
            public = _row_to_public(
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )
        else:
            conn.execute(
                """
                UPDATE runs
                SET cancel_requested = 1, updated_at = ?,
                    error_message = '已请求取消，不会发出新的模型请求。'
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            public = _row_to_public(
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )
    terminate_run(run_id)
    return public


def delete_run(run_id: str) -> None:
    with connect() as conn:
        row = conn.execute("SELECT run_id, status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise TweetRunError("找不到这个运行。", 404, "not_found")
        if row["status"] in ACTIVE_STATUSES or row["status"] == "queued":
            conn.execute(
                "UPDATE runs SET cancel_requested = 1, status = 'cancelled' WHERE run_id = ?",
                (run_id,),
            )
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))


def report_view(run_id: str) -> dict:
    run = get_run(run_id)
    with connect() as conn:
        artifact = conn.execute(
            """
            SELECT body_json FROM artifacts
            WHERE run_id = ? AND kind = 'report'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    report = json.loads(artifact["body_json"]) if artifact and artifact["body_json"] else None
    return {
        "run_id": run_id,
        "status": run["status"],
        "report": report,
        "message": None if report else REPORT_NOT_GENERATED,
    }


def database_path():
    return state_db_path()
