"""Wrapped CLI and Ollama payloads must stay usable.

These fixtures stand in for Grok and Ollama. They do not call either one.
A reply that is only prose stays missing; it is not stored as silence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts.check_contracts import check_report, load_schemas

from app.providers.adapters import _from_http
from app.tweet.audience import build_slots
from app.tweet.db import connect
from app.tweet.loop import (
    _wave_input,
    build_persona_payload,
    execute_loop,
    interpret_actions,
    interpret_personas,
)
from app.tweet.model_json import parse_model_object
from app.tweet.report import _clean_rewrites, _report_prompt, rewrite_problems, semantic_problems
from test_m3_tweet_loop import REPLY_TEXT, Clock, _actions, _app, _create, _ready, seed_persona_cache
from test_m4_report import _evidence, _valid_rewrites

_, VALIDATORS = load_schemas()

DRAFT = "今天把咖啡洒在键盘上了，但代码竟然还在跑。"
AUTHOR = "一个写代码的人"


def _fit(text: str) -> str:
    if len(text) < 60:
        text += "。" * (60 - len(text))
    return text[:120]


def _persona(agent_id: str = "a001", **overrides) -> dict:
    item = {
        "agent_id": agent_id,
        "display_name": "虚构读者",
        "bio": "写代码的虚构读者",
        "persona": _fit(
            "平时写代码，看到具体句子才会开口。没有亲身经历时不跟着下结论，也不把别人的键盘事故当成自己的故事。"
        ),
        "avoid_speaking_when": "没有具体句子时保持沉默。",
    }
    item.update(overrides)
    return item


def _batch(items: list[dict], **extra) -> dict:
    document = {"schema_version": "2.0", "personas": items}
    document.update(extra)
    return document


def _wrap(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    return (
        "下面是结果，前面的括号作废。\n"
        "<think>坏括号 { 不是 JSON\n"
        "```json\n"
        + raw
        + "\n```\n"
        "以上。\n"
    )


def _reply_action() -> dict:
    return {
        "action": "reply",
        "target_id": "post_0",
        "text": "键盘还能响就先别关机。",
        "expressed_stance": "neutral",
        "trigger_span": {"start": 0, "end": 2, "text": "今天", "reason": "模型多写的字段"},
        "confidence": 0.2,
    }


def _wave_item(agent_id: str = "a001") -> dict:
    return {
        "agent_id": agent_id,
        "candidate_action": "reply",
        "visible_timeline": [{"id": "post_0", "kind": "root_post", "text": DRAFT}],
        "persona": "x" * 60,
    }


def test_wrapped_persona_json_locks_slots_and_does_not_use_templates():
    slots = [{"agent_id": "a001"}, {"agent_id": "a002"}]
    first = _persona("a001", mood="tired", persona_source="hacked", display_name="键盘旁的人")
    second = _persona(
        "a002",
        display_name="虚构" + ("路人" * 20),
        bio="短" * 50,
        persona="写" * 180,
        avoid_speaking_when="沉默" * 40,
    )
    text = (
        '<think>{"note":"skip"}</think>\n'
        '说明 {"schema_version":"2.0"} 还不是正文。\n'
        + _wrap(_batch([first, second]))
    )
    locked, errors = interpret_personas(text, slots)
    assert errors == []
    assert set(locked) == {"a001", "a002"}
    assert locked["a001"]["display_name"] == "虚构键盘旁的人"
    assert locked["a001"]["persona_source"] == "subscription_cli"
    assert "mood" not in locked["a001"]
    assert locked["a002"]["display_name"].startswith("虚构")
    assert len(locked["a002"]["display_name"]) <= 30
    assert len(locked["a002"]["bio"]) == 40
    assert len(locked["a002"]["persona"]) == 120
    assert len(locked["a002"]["avoid_speaking_when"]) == 60
    assert locked["a002"]["persona_source"] == "subscription_cli"


def test_short_or_unparsed_personas_are_not_invented():
    slots = [{"agent_id": "a001"}, {"agent_id": "a002"}]
    short = _persona("a001", persona="太短")
    missing_name = _persona("a002", display_name="   ")
    locked, errors = interpret_personas(json.dumps(_batch([short, missing_name]), ensure_ascii=False), slots)
    assert locked == {}
    assert {item["agent_id"] for item in errors} == {"a001", "a002"}
    assert all(item["codes"] == ["schema"] for item in errors)
    prose_locked, prose_errors = interpret_personas("这里没有 JSON。", slots)
    assert prose_locked == {}
    assert [item["codes"] for item in prose_errors] == [["json"], ["json"]]
    wrong_version = _batch([_persona("a001"), _persona("a002")], schema_version="1.0")
    version_locked, version_errors = interpret_personas(json.dumps(wrong_version), slots)
    assert version_locked == {}
    assert version_errors[0]["codes"] == ["schema"]
    omitted = {"personas": [_persona("a001"), _persona("a002")]}
    omitted_locked, omitted_errors = interpret_personas(json.dumps(omitted, ensure_ascii=False), slots)
    assert omitted_errors == []
    assert set(omitted_locked) == {"a001", "a002"}


def test_wrapped_wave_keeps_one_reply_and_prose_stays_missing():
    item = _wave_item()
    document = {
        "round": "1",
        "actions": [{
            "agent_id": "a001",
            "action": _reply_action(),
        }],
    }
    text = "思考完毕。\n" + _wrap(document)
    locked, errors = interpret_actions(text, round_number=1, items=[item], draft=DRAFT)
    assert errors == []
    assert locked["a001"]["action"] == "reply"
    assert locked["a001"]["text"] == "键盘还能响就先别关机。"
    assert locked["a001"]["trigger_span"] == {"start": 0, "end": 2, "text": "今天"}
    assert "confidence" not in locked["a001"]

    prose_locked, prose_errors = interpret_actions(
        "我只想说话，没有对象。",
        round_number=1,
        items=[item],
        draft=DRAFT,
    )
    assert prose_locked == {}
    assert prose_errors[0]["codes"] == ["json"]

    renamed = {
        "schema_version": "2.0",
        "round": 1,
        "results": [{
            "agent_id": "a001",
            "action": {"type": "reply", "target_id": "post_0", "text": "不会改名"},
        }],
    }
    renamed_locked, renamed_errors = interpret_actions(
        json.dumps(renamed, ensure_ascii=False),
        round_number=1,
        items=[item],
        draft=DRAFT,
    )
    assert renamed_locked == {}
    assert "schema" in renamed_errors[0]["codes"]

    bad_span = {
        "schema_version": "2.0",
        "round": 1,
        "results": [{
            "agent_id": "a001",
            "action": {
                "action": "reply",
                "target_id": "post_0",
                "text": "跨度不对",
                "expressed_stance": "neutral",
                "trigger_span": {"start": 1, "end": 3, "text": "👍"},
            },
        }],
    }
    span_locked, span_errors = interpret_actions(
        _wrap(bad_span),
        round_number=1,
        items=[_wave_item()],
        draft="好👍好",
    )
    assert span_locked == {}
    assert "trigger_span" in span_errors[0]["codes"]


def test_wrapped_rewrites_are_kept_when_citations_are_not():
    draft = DRAFT
    evidence = _evidence(draft)
    model = {
        "rewrites": _valid_rewrites(draft),
        "top_replies": [{"action_id": "act-fake", "group_ids": ["tech"]}],
        "trigger_lines": [{"span": {"start": 1, "end": 3, "text": "👍"}, "evidence_ids": ["missing"]}],
    }
    wrapped = (
        "报告如下，第一个括号坏了 { \n"
        "```json\n"
        + json.dumps(model, ensure_ascii=False)
        + "\n```\n"
    )
    parsed = parse_model_object(wrapped)
    assert rewrite_problems(parsed, evidence) == []
    assert "fake_citation" in semantic_problems(parsed, evidence)
    assert "bad_span" in semantic_problems(parsed, evidence)
    cleaned = _clean_rewrites(parsed, evidence)
    assert len(cleaned) == 2
    assert all(item["expected_effect"]["simulation_verified"] is False for item in cleaned)
    assert parse_model_object("不是 JSON") is None
    assert parse_model_object(json.dumps([model], ensure_ascii=False)) is None
    encoded = json.dumps(json.dumps(model, ensure_ascii=False), ensure_ascii=False)
    assert parse_model_object(encoded)["rewrites"][0]["variant"] == "preserve_claim"


def test_prompts_name_the_output_contract():
    slots = build_slots(2, seed=1)
    persona = build_persona_payload(slots, audience_version="zh_x_v1", seed=1)
    encoded = json.dumps(persona, ensure_ascii=False)
    assert "想好的全名必须原样写进 JSON 的 display_name" in persona["output_contract"]["display_name"]
    assert "不能只写前缀" in persona["output_contract"]["display_name"]
    assert '错的是"虚构"' in persona["output_contract"]["display_name"]
    assert '对的是"虚构甲"' in persona["output_contract"]["display_name"]
    assert "至少 3 个码点" in persona["output_contract"]["display_name"]
    assert "同一批里不要重名" in persona["output_contract"]["display_name"]
    assert "再加至少一个字" not in persona["output_contract"]["display_name"]
    assert "不要说明" not in persona["output_contract"]["shape"]
    assert "60" in persona["output_contract"]["persona"]
    assert "120" in persona["output_contract"]["persona"]
    assert DRAFT not in encoded
    assert "draft_text" not in encoded
    wave = _wave_input(
        1,
        [{
            "agent_id": "a001",
            "candidate_action": "reply",
            "persona": "公开人设",
            "visible_timeline": [],
        }],
        {"a001": {"persona": "公开人设"}},
        [{"id": "post_0", "kind": "root_post", "text": DRAFT}],
        DRAFT,
        AUTHOR,
    )
    contract = json.dumps(wave["output_contract"], ensure_ascii=False)
    assert "post_0" in contract
    assert "trigger_span" in contract
    assert "none" in contract
    report = _report_prompt(
        {"draft_text": DRAFT},
        {
            "metrics": {},
            "engagement": {"tier": "low"},
            "backlash_risk": {"level": "low"},
            "top_replies": [],
        },
    )
    assert "rewrites" in report["instruction"]
    assert "top_replies" in report["instruction"]
    assert "这是预测" in report["instruction"]
    assert "模拟备忘" in report["instruction"]
    assert "整段回复就是一个 JSON 对象，不要先写一句中文。" in report["instruction"]
    assert "不要跑脚本数码点" in report["instruction"]
    assert "rewrites 必须是数组，长度只能是 2 或 3。" in report["instruction"]
    assert "2 到 3" not in report["instruction"]
    assert "码点切片" not in report["instruction"]
    assert "先按" not in report["instruction"]


def test_ollama_reads_parts_and_empty_content_reasoning():
    fenced = _wrap({"schema_version": "2.0", "round": 1, "results": []})
    listed = json.dumps({
        "choices": [{
            "message": {
                "content": [
                    {"type": "thinking", "text": "不要用这段"},
                    {"type": "text", "text": fenced},
                ],
                "reasoning": json.dumps({"schema_version": "2.0", "round": 9, "results": [{"agent_id": "a999"}]}),
            }
        }]
    }).encode()
    from_parts = _from_http(listed, 200, 200_000, started=True)
    assert parse_model_object(from_parts.text)["round"] == 1
    assert "a999" not in from_parts.text

    reasoned = json.dumps({
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": fenced,
            }
        }]
    }).encode()
    from_reasoning = _from_http(reasoned, 200, 200_000, started=True)
    assert parse_model_object(from_reasoning.text)["results"] == []


def test_wrapped_mixed_run_keeps_persona_reply_and_schema_report(tmp_path, monkeypatch):
    import test_m3_tweet_loop as loop_tests

    env = _ready(tmp_path, monkeypatch)
    original = loop_tests.render_output

    def wrapped(document, mode, repair):
        raw = original(document, mode, repair)
        if mode == "not-json" or raw == "not-json":
            return raw
        return _wrap(json.loads(raw))

    monkeypatch.setattr(loop_tests, "render_output", wrapped)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="wrapped-mixed",
            agent_count=2,
            round_count=1,
            draft=DRAFT,
            author_context=AUTHOR,
        )
        seed_persona_cache(run_id, seed=4)
        slots = build_slots(2, seed=4)
        candidates = {slot["agent_id"]: "none" for slot in slots}
        candidates["a001"] = "reply"
        summary = execute_loop(run_id, seed=4, clock=Clock(), candidates=candidates)
        assert summary["persona"]["cache"] == "hit"
        assert summary["persona"]["fallback"] is False
        assert summary["status"] == "complete"
        actions = {row["agent_id"]: row for row in _actions(run_id)}
        assert actions["a001"]["source"] == "model"
        assert actions["a001"]["action"] == "reply"
        assert REPLY_TEXT in actions["a001"]["payload_json"]
        assert actions["a002"]["source"] == "rule"
        assert actions["a002"]["action"] == "none"
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["schema_version"] == "2.0"
        assert document["status"] == "complete"
        assert "模拟备忘" in document["limitations"][0]
        assert len(document["rewrites"]) >= 2
        assert document["degradation_reasons"] == []
        blob = json.dumps(document, ensure_ascii=False)
        assert "短句模板" not in blob
        assert "没有编造" not in blob
        with connect() as conn:
            calls = conn.execute(
                """
                SELECT role, channel FROM provider_requests
                WHERE run_id = ? AND status = 'completed'
                ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
        assert sorted((row["role"], row["channel"]) for row in calls) == [
            ("agent", "ollama"),
            ("report", "grok_cli"),
        ]
        assert not any('"task":"persona_batch"' in prompt for prompt in env["brain"].prompts)
    finally:
        env["server"].shutdown()


def test_unusable_wave_text_is_missing_not_silence(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    env["brain"].script = ["not-json", "not-json", "valid"]
    try:
        client = _app().test_client()
        run_id = _create(client, key="prose-wave", agent_count=2, round_count=1, draft=DRAFT)
        seed_persona_cache(run_id, seed=4)
        slots = build_slots(2, seed=4)
        candidates = {slot["agent_id"]: "none" for slot in slots}
        candidates["a001"] = "reply"
        summary = execute_loop(run_id, seed=4, clock=Clock(), candidates=candidates)
        assert summary["status"] == "degraded"
        actions = {row["agent_id"]: row for row in _actions(run_id)}
        assert actions["a001"]["source"] == "model"
        assert actions["a001"]["outcome"] == "missing"
        assert actions["a001"]["action"] is None
        payload = json.loads(actions["a001"]["payload_json"])
        assert payload["action"] is None
        assert actions["a002"]["source"] == "rule"
        assert actions["a002"]["action"] == "none"
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["rewrites"]
        assert "有账号的反应缺失，没有补记为划走。" in document["degradation_reasons"]
        assert "没有编造" not in json.dumps(document, ensure_ascii=False)
    finally:
        env["server"].shutdown()
