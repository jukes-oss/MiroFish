"""Open a gateway run for local memory calls.

The row lives in the tweet ledger so the existing call and time caps apply.
Its status is `gateway`, which the tweet worker does not claim and which is
not a terminal tweet status. The tweet history hides run_kind=memory.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from ..providers.profile import build_capability_profile, dump_profile
from ..tweet.channels import channel_config, channel_error_message, channel_problems
from ..tweet.db import connect, init_db


def open_gateway_run(*, label: str) -> str:
    problems = channel_problems()
    if problems:
        raise RuntimeError(channel_error_message(problems))
    init_db()
    selected = channel_config()
    profile = build_capability_profile(
        execution_profile="mixed",
        subscription_cli=selected["subscription_cli"],
        cli_path=selected["cli_path"],
        ollama_base_url=selected["ollama_base_url"],
        ollama_model=selected["ollama_model"],
    )
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, idempotency_key, request_hash, status, draft_text,
                author_context, audience_version, agent_count, round_count,
                execution_profile, subscription_cli, schema_version,
                cancel_requested, created_at, updated_at, capability_json, run_kind
            ) VALUES (?, ?, ?, 'gateway', ?, '', 'zh_x_v1', 1, 1, 'mixed', ?, '2.0',
                      0, ?, ?, ?, 'memory')
            """,
            (
                run_id,
                f"memory-{run_id}",
                digest,
                "本地图谱调用",
                selected["subscription_cli"],
                now,
                now,
                dump_profile(profile),
            ),
        )
    return run_id
