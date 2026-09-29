"""A 12-person persona batch must be small enough for the 120s cap.

The stand-in CLI times out immediately. This does not call Grok or Ollama.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts.check_contracts import check_report, load_schemas

from app.providers.adapters import AdapterOutcome
from app.providers.gateway import invoke_subscription_cli
from app.providers.limits import (
    CLEANUP_RESERVE_SECONDS,
    REPORT_RESERVE_CALLS,
    REPORT_RESERVE_WALL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RUN_MAX_REQUESTS,
    RUN_MAX_WALL_SECONDS,
)
from app.tweet.audience import build_slots
from app.tweet.db import connect
from app.tweet.loop import (
    PERSONA_CALL_MAX_SLOTS,
    build_persona_payload,
    execute_loop,
    persona_call_seconds,
    persona_groups,
)
from test_m3_tweet_loop import REPLY_TEXT, Clock, _actions, _app, _create, _ready, _sent

import app.providers.gateway as gateway

_, VALIDATORS = load_schemas()

DRAFT = "今天把咖啡洒在键盘上了，但代码竟然还在跑。"
AUTHOR = "一个写代码的人"


def test_twelve_person_persona_calls_stay_inside_the_request_cap():
    slots = build_slots(12, seed=1)
    groups = persona_groups(slots, 1)
    assert [len(group) for group in groups] == [4, 4, 4]
    assert PERSONA_CALL_MAX_SLOTS == 4
    non_report_window = (
        RUN_MAX_WALL_SECONDS - REPORT_RESERVE_WALL_SECONDS - CLEANUP_RESERVE_SECONDS
    )
    assert sum(persona_call_seconds(len(group)) for group in groups) <= non_report_window
    full = build_persona_payload(slots, audience_version="zh_x_v1", seed=1)
    seen = []
    for group in groups:
        assert persona_call_seconds(len(group)) < REQUEST_TIMEOUT_SECONDS
        payload = build_persona_payload(group, audience_version="zh_x_v1", seed=1)
        assert len(payload["slots"]) <= PERSONA_CALL_MAX_SLOTS
        assert payload["output_contract"]["persona"].startswith("60")
        encoded = json.dumps(payload, ensure_ascii=False)
        assert len(encoded) < len(json.dumps(full, ensure_ascii=False))
        assert DRAFT not in encoded
        assert "draft_text" not in encoded
        seen.extend(item["agent_id"] for item in payload["slots"])
    assert seen == [slot["agent_id"] for slot in slots]
    assert len(groups) + 1 + REPORT_RESERVE_CALLS <= RUN_MAX_REQUESTS
    assert [len(group) for group in persona_groups(build_slots(120, seed=1), 3)] == [120]
    assert [len(group) for group in persona_groups(build_slots(240, seed=1), 4)] == [240]


def test_twelve_person_run_sends_three_short_persona_calls(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="twelve-fit",
            agent_count=12,
            round_count=1,
            draft=DRAFT,
            author_context=AUTHOR,
        )
        slots = build_slots(12, seed=4)
        candidates = {slot["agent_id"]: "none" for slot in slots}
        candidates["a001"] = "reply"
        summary = execute_loop(run_id, seed=4, clock=Clock(), candidates=candidates)
        assert summary["persona"]["fallback"] is False
        assert summary["persona"]["calls"] == 3
        assert summary["status"] == "complete"
        assert _sent(run_id, "persona") == 3
        actions = {row["agent_id"]: row for row in _actions(run_id)}
        assert actions["a001"]["source"] == "model"
        assert actions["a001"]["action"] == "reply"
        assert REPLY_TEXT in actions["a001"]["payload_json"]
        assert all(
            actions[slot["agent_id"]]["source"] == "rule" and actions[slot["agent_id"]]["action"] == "none"
            for slot in slots
            if slot["agent_id"] != "a001"
        )
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["schema_version"] == "2.0"
        assert document["status"] == "complete"
        assert "模拟备忘" in document["limitations"][0]
        assert len(document["rewrites"]) >= 2
        assert document["degradation_reasons"] == []
        with connect() as conn:
            prompts = conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_prompt' ORDER BY created_at",
                (run_id,),
            ).fetchall()
            calls = conn.execute(
                """
                SELECT role, channel, logical_batch FROM provider_requests
                WHERE run_id = ? AND status = 'completed'
                """,
                (run_id,),
            ).fetchall()
        assert [len(json.loads(row["body_json"])["slots"]) for row in prompts] == [4, 4, 4]
        by_role = {}
        for row in calls:
            by_role.setdefault(row["role"], set()).add(row["channel"])
        assert by_role["persona"] == {"grok_cli"}
        assert by_role["agent"] == {"ollama"}
        assert by_role["report"] == {"grok_cli"}
        assert sorted(row["logical_batch"] for row in calls if row["role"] == "persona") == [
            "persona-1",
            "persona-2",
            "persona-3",
        ]
    finally:
        env["server"].shutdown()


def test_persona_timeout_stays_a_degraded_template_memo(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)

    def timed_out(**kwargs):
        prompt = kwargs.get("prompt") or ""
        if '"task":"persona_batch"' in prompt:
            return AdapterOutcome(started=True, text="", exit_code=-9, error_code="timeout")
        return invoke_subscription_cli(**kwargs)

    monkeypatch.setattr(gateway, "invoke_subscription_cli", timed_out)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="twelve-timeout",
            agent_count=12,
            round_count=1,
            draft=DRAFT,
            author_context=AUTHOR,
        )
        slots = build_slots(12, seed=4)
        summary = execute_loop(
            run_id,
            seed=4,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["status"] == "degraded"
        assert summary["persona"]["fallback"] is True
        assert summary["persona"]["calls"] == 3
        assert _sent(run_id, "persona") == 3
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["status"] == "degraded"
        assert "人设使用了短句模板，不是订阅通道生成。" in document["degradation_reasons"]
        assert "模拟备忘" in document["limitations"][0]
        actions = _actions(run_id)
        assert len(actions) == 12
        assert {row["source"] for row in actions} == {"rule"}
        assert {row["action"] for row in actions} == {"none"}
        with connect() as conn:
            stored = json.loads(conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_batch'",
                (run_id,),
            ).fetchone()["body_json"])
            rows = conn.execute(
                """
                SELECT status, exit_code, attempt_no, logical_batch
                FROM provider_requests
                WHERE run_id = ? AND role = 'persona'
                ORDER BY logical_batch
                """,
                (run_id,),
            ).fetchall()
            cached = conn.execute("SELECT COUNT(*) AS n FROM persona_cache").fetchone()["n"]
        assert stored["fallback_ids"] == [slot["agent_id"] for slot in slots]
        assert all(item["persona_source"] == "persona_fallback" for item in stored["personas"])
        assert [row["logical_batch"] for row in rows] == ["persona-1", "persona-2", "persona-3"]
        assert {row["status"] for row in rows} == {"uncertain"}
        assert {row["exit_code"] for row in rows} == {-9}
        assert {row["attempt_no"] for row in rows} == {1}
        assert cached == 0
    finally:
        env["server"].shutdown()
