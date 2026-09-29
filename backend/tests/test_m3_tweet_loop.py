"""M3 tweet loop against the fake gateway. No network and no API keys.

A four-account run covers reply, quote, scroll-past, and missing as branches.
It does not claim a real sample of four accounts must contain those actions.
"""

from __future__ import annotations

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import create_app
from app.tweet.audience import DIMENSIONS, build_slots, quota_table, wave_sizes
from app.tweet.db import connect
from app.tweet.loop import (
    build_persona_payload,
    execute_loop,
    interpret_actions,
    private_bleed,
    shared_context_risk,
)
from app.tweet.sampling import (
    BASE_WEIGHTS,
    draw_candidates,
    model_items,
    sample_action,
    target_frequencies,
)

DRAFT = "ZZDRAFT-不应进入人设-9f3c"
AUTHOR = "作者背景"
REPLY_TEXT = "只回这一句，不改成别的动作。"
REWRITTEN = "改写了已锁定的回复，这句不该落库。"


def _fit(text: str, low: int, high: int) -> str:
    if len(text) < low:
        text += "。" * (low - len(text))
    return text[:high]


def _persona(slot: dict) -> dict:
    agent_id = slot["agent_id"]
    persona = _fit(
        (
            f"槽位{agent_id}按公开标签行动，不引用当前草稿。说话短，先看句子再决定是否开口。"
            "没有亲身经历时不下判断，也不把别人的私有描述当成自己的经历。"
        ),
        60,
        120,
    )
    return {
        "agent_id": agent_id,
        "display_name": f"虚构{agent_id}",
        "bio": "公开标签下的虚构读者",
        "persona": persona,
        "avoid_speaking_when": "没有具体句子时保持沉默。",
    }


def _action_for(item: dict) -> dict:
    kind = item["candidate_action"]
    draft = item["draft_text"]
    if kind in ("like", "repost"):
        return {
            "action": kind,
            "target_id": "post_0",
            "text": None,
            "expressed_stance": "unexpressed",
            "trigger_span": None,
        }
    span = draft[:2] if len(draft) >= 2 else draft
    return {
        "action": kind,
        "target_id": "post_0",
        "text": REPLY_TEXT,
        "expressed_stance": "neutral",
        "trigger_span": {"start": 0, "end": len(span), "text": span},
    }


def _like(target: str) -> dict:
    return {
        "action": "like",
        "target_id": target,
        "text": None,
        "expressed_stance": "unexpressed",
        "trigger_span": None,
    }


def _extract_task(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        loaded, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def render_output(document: dict, mode: str, repair: bool) -> str:
    task = document.get("task")
    if mode == "not-json":
        return "not-json"
    if task == "persona_batch":
        return json.dumps(
            {"schema_version": "2.0", "personas": [_persona(slot) for slot in document.get("slots") or []]},
            ensure_ascii=False,
        )
    if task == "report_stats":
        return json.dumps({"received": True}, ensure_ascii=False)
    items = document.get("items") or []
    round_number = document.get("round")
    results = []
    if mode == "extra-id":
        results.append({
            "agent_id": "a999",
            "action": _like("post_0"),
        })
    elif mode == "duplicate":
        if items:
            results = [
                {"agent_id": items[0]["agent_id"], "action": _action_for(items[0])},
                {"agent_id": items[0]["agent_id"], "action": _action_for(items[0])},
            ]
    elif mode == "illegal-mix":
        for index, item in enumerate(items):
            if index == 0:
                action = _like("future-wave-9")
            elif index == 1:
                action = _like(item["agent_id"])
            else:
                action = _like("hidden-post")
            results.append({"agent_id": item["agent_id"], "action": action})
    elif mode == "lock-check":
        for item in items:
            action = _action_for(item)
            if item["agent_id"] == "a001" and repair:
                action = dict(action)
                action["text"] = REWRITTEN
            if item["agent_id"] == "a002":
                action = _like("post_0")
            results.append({"agent_id": item["agent_id"], "action": action})
    elif mode == "bleed":
        for index, item in enumerate(items):
            action = _action_for(item)
            if index == 0 and len(items) > 1 and item["candidate_action"] in ("reply", "quote"):
                action = dict(action)
                action["text"] = items[1]["persona"][:16]
            results.append({"agent_id": item["agent_id"], "action": action})
    else:
        omitted = mode.split(":", 1)[1] if mode.startswith("omit:") else None
        for item in items:
            if omitted and item["agent_id"] == omitted:
                continue
            results.append({"agent_id": item["agent_id"], "action": _action_for(item)})
    return json.dumps(
        {"schema_version": "2.0", "round": round_number, "results": results},
        ensure_ascii=False,
    )


class Brain:
    def __init__(self):
        self.lock = threading.Lock()
        self.script: list[str] = []
        self.prompts: list[str] = []
        self.ollama_calls = 0
        self.brain_calls = 0

    def next_mode(self) -> str:
        with self.lock:
            if self.script:
                return self.script.pop(0)
            return "valid"


FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys, urllib.request
prompt = sys.argv[2] if len(sys.argv) > 2 else ""
request = urllib.request.Request(
    os.environ["FAKE_BRAIN"],
    data=json.dumps({"prompt": prompt}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    sys.stdout.buffer.write(response.read())
"""


def _start_brain(brain: Brain):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            if self.path.startswith("/synthesize"):
                prompt = json.loads(raw).get("prompt") or ""
                brain.brain_calls += 1
            else:
                payload = json.loads(raw or "{}")
                messages = payload.get("messages") or []
                prompt = "\n".join(str(item.get("content") or "") for item in messages)
                brain.ollama_calls += 1
            brain.prompts.append(prompt)
            mode = brain.next_mode()
            document = _extract_task(prompt)
            body = render_output(document, mode, "MIROFISH_REPAIR" in prompt).encode("utf-8")
            if not self.path.startswith("/synthesize"):
                wrapped = json.dumps(
                    {
                        "choices": [{"message": {"content": body.decode("utf-8")}}],
                        "usage": {"total_tokens": 3},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                body = wrapped
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class Clock:
    def __init__(self, now_ms: int = 1_700_000_000_000):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def day_key(self) -> str:
        return "2026-09-29"


def _ready(tmp_path, monkeypatch):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    script.chmod(0o755)
    grok = tmp_path / "grok"
    codex = tmp_path / "codex"
    grok.write_text(FAKE_CLI, encoding="utf-8")
    codex.write_text(FAKE_CLI, encoding="utf-8")
    grok.chmod(0o755)
    codex.chmod(0o755)
    login = tmp_path / "login"
    login.write_text("logged-in\n", encoding="utf-8")
    brain = Brain()
    server = _start_brain(brain)
    host, port = server.server_address
    monkeypatch.setenv("TWEET_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("TWEET_WORKER_MODE", "manual")
    monkeypatch.setenv("SUBSCRIPTION_CLI", "grok")
    monkeypatch.setenv("GROK_CLI_PATH", str(grok))
    monkeypatch.setenv("CODEX_CLI_PATH", str(codex))
    monkeypatch.setenv("SUBSCRIPTION_CLI_LOGIN_PATH", str(login))
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://{host}:{port}/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.8:27b-mxfp8")
    monkeypatch.setenv("FAKE_BRAIN", f"http://{host}:{port}/synthesize")
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return {"server": server, "brain": brain, "grok": grok}


def _app():
    app = create_app()
    app.config.update(TESTING=True)
    return app


def _create(client, **overrides):
    body = {
        "idempotency_key": overrides.pop("key", "loop-1"),
        "draft_text": overrides.pop("draft", DRAFT),
        "author_context": AUTHOR,
        "audience_version": "zh_x_v1",
        "agent_count": 120,
        "round_count": 3,
        "execution_profile": "mixed",
    }
    body.update(overrides)
    response = client.post("/api/tweet/runs", json=body)
    assert response.status_code == 202, response.json
    return response.json["run_id"]


def _sent(run_id: str, role: str | None = None) -> int:
    clauses = ["status IN ('completed', 'failed', 'uncertain', 'in_flight')"]
    params: list = []
    if role is not None:
        clauses.append("role = ?")
        params.append(role)
    clauses.append("run_id = ?")
    params.append(run_id)
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM provider_requests WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
    return int(row["n"])


def _actions(run_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, source, outcome, action, payload_json, "round"
            FROM actions WHERE run_id = ? ORDER BY agent_id
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_quota_sums_are_exact_and_independent():
    for total in (1, 4, 7, 120, 240):
        table = quota_table(total)
        assert set(table) == {name for name, _spec in DIMENSIONS}
        for name, spec in DIMENSIONS:
            assert sum(table[name].values()) == total
            assert set(table[name]) == {key for key, _label, _weight in spec}
        slots = build_slots(total, seed=3)
        assert [slot["agent_id"] for slot in slots] == [f"a{index:03d}" for index in range(1, total + 1)]
        for name, _spec in DIMENSIONS:
            counts: dict[str, int] = {}
            for slot in slots:
                counts[slot[name]] = counts.get(slot[name], 0) + 1
            assert counts == {key: value for key, value in table[name].items() if value}
    slots = build_slots(120, seed=3)
    tech = [slot["activity"] for slot in slots if slot["circle"] == "tech"]
    assert len(set(tech)) > 1
    assert wave_sizes(120, 3) == [40, 40, 40]
    assert wave_sizes(240, 4) == [60, 60, 60, 60]
    assert wave_sizes(4, 3) == [2, 1, 1]
    assert sum(wave_sizes(4, 3)) == 4
    assert sum(weight for _name, weight in BASE_WEIGHTS) == 10_000


def test_rule_draws_are_reproducible_and_base_rates_stay_within_one_point():
    slots = build_slots(120, seed=11)
    assert draw_candidates(slots, 11) == draw_candidates(slots, 11)
    assert draw_candidates(slots, 11) != draw_candidates(slots, 12)
    rng = random.Random("frequency-check")
    counts = {name: 0 for name, _weight in BASE_WEIGHTS}
    for _ in range(10_000):
        counts[sample_action(rng, "mid")] += 1
    for name, weight in BASE_WEIGHTS:
        assert abs(counts[name] / 10_000 - weight / 10_000) <= 0.01
    low = sum(sample_action(random.Random(f"low:{index}"), "low") == "none" for index in range(4000))
    high = sum(sample_action(random.Random(f"high:{index}"), "high") == "none" for index in range(4000))
    assert high < low
    assert target_frequencies("mid")["none"] == 0.9


def test_persona_payload_omits_the_draft_and_silence_is_not_a_model_item():
    slots = build_slots(4, seed=1)
    payload = build_persona_payload(slots, audience_version="zh_x_v1", seed=1)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert DRAFT not in encoded
    assert "draft_text" not in payload
    candidates = {slot["agent_id"]: "none" for slot in slots}
    candidates["a001"] = "reply"
    assert [slot["agent_id"] for slot in model_items(slots, candidates)] == ["a001"]
    missing_text = '{"schema_version":"2.0","round":1,"results":[]}'
    items = [{
        "agent_id": "a001",
        "candidate_action": "reply",
        "visible_timeline": [{"id": "post_0", "kind": "root_post", "text": DRAFT}],
        "persona": "x" * 60,
    }]
    locked, errors = interpret_actions(missing_text, round_number=1, items=items, draft=DRAFT)
    assert locked == {}
    assert errors[0]["codes"] == ["missing_id"]
    assert "none" not in json.dumps(errors)


def test_default_120_by_3_cold_cache_is_five_calls(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(client)
        summary = execute_loop(run_id, seed=7, clock=Clock())
        assert summary["status"] == "complete"
        assert summary["wave_sizes"] == [40, 40, 40]
        assert summary["persona"]["cache"] == "miss"
        assert summary["persona"]["fallback"] is False
        assert _sent(run_id) == 5
        assert _sent(run_id, "persona") == 1
        assert _sent(run_id, "agent") == 3
        assert _sent(run_id, "report") == 1
        with connect() as conn:
            exposures = conn.execute(
                "SELECT COUNT(*) AS n FROM exposures WHERE run_id = ?",
                (run_id,),
            ).fetchone()["n"]
            batches = conn.execute(
                """
                SELECT logical_batch FROM provider_requests
                WHERE run_id = ? AND role = 'agent' AND status = 'completed'
                ORDER BY logical_batch
                """,
                (run_id,),
            ).fetchall()
            prompts = conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_prompt'",
                (run_id,),
            ).fetchone()
            waves = conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'wave_input' ORDER BY created_at",
                (run_id,),
            ).fetchall()
        assert exposures == 120
        assert [row["logical_batch"] for row in batches] == ["wave-1", "wave-2", "wave-3"]
        assert DRAFT not in prompts["body_json"]
        assert env["brain"].ollama_calls == 3
        assert env["brain"].brain_calls == 2
        seen_items = []
        none_ids = {
            agent_id
            for agent_id, action in draw_candidates(build_slots(120, 7), 7).items()
            if action == "none"
        }
        for row in waves:
            payload = json.loads(row["body_json"])
            ids = [item["agent_id"] for item in payload["items"]]
            assert len(ids) == len(set(ids))
            assert not (set(ids) & none_ids)
            assert all(item["candidate_action"] != "none" for item in payload["items"])
            seen_items.extend(ids)
            for item in payload["items"]:
                assert DRAFT not in item["persona"]
                assert all("persona" not in json.dumps(entry, ensure_ascii=False) for entry in item["visible_timeline"])
        assert len(seen_items) == 120 - len(none_ids)
        assert all(wave["physical_isolation"] is False for wave in summary["waves"])
        report = client.get(f"/api/tweet/runs/{run_id}/report")
        assert report.json["report"] is None
        assert "不生成" in report.json["message"]
        persona_prompts = [prompt for prompt in env["brain"].prompts if '"task":"persona_batch"' in prompt]
        assert persona_prompts and all(DRAFT not in prompt for prompt in persona_prompts)
    finally:
        env["server"].shutdown()


def test_warm_cache_skips_the_persona_call(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        first = _create(client, key="cold", draft=DRAFT)
        second = _create(client, key="warm", draft=DRAFT + "另一条")
        cold = execute_loop(first, seed=7, clock=Clock())
        warm = execute_loop(second, seed=7, clock=Clock())
        assert cold["persona"]["cache"] == "miss"
        assert warm["persona"]["cache"] == "hit"
        assert warm["persona"]["calls"] == 0
        assert _sent(second) == 4
        assert _sent(second, "persona") == 0
        assert _sent(second, "agent") == 3
        assert _sent(second, "report") == 1
        with connect() as conn:
            exposures = conn.execute(
                "SELECT COUNT(*) AS n FROM exposures WHERE run_id = ?",
                (second,),
            ).fetchone()["n"]
        assert exposures == 120
    finally:
        env["server"].shutdown()


def test_empty_candidate_wave_sends_one_empty_batch_and_no_silent_requests(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(client, key="empty", agent_count=6, round_count=2)
        slots = build_slots(6, seed=2)
        candidates = {slot["agent_id"]: "none" for slot in slots}
        summary = execute_loop(run_id, seed=2, clock=Clock(), candidates=candidates)
        assert [wave["model_items"] for wave in summary["waves"]] == [0, 0]
        assert _sent(run_id, "agent") == 2
        assert _sent(run_id) == 4
        actions = _actions(run_id)
        assert len(actions) == 6
        assert {row["source"] for row in actions} == {"rule"}
        assert {row["action"] for row in actions} == {"none"}
        with connect() as conn:
            batches = [
                row["logical_batch"]
                for row in conn.execute(
                    "SELECT logical_batch FROM provider_requests WHERE run_id = ? AND role = 'agent'",
                    (run_id,),
                )
            ]
        assert batches == ["wave-1", "wave-2"]
        assert all(not str(batch).startswith("agent-") for batch in batches)
    finally:
        env["server"].shutdown()


def test_four_account_mock_covers_reply_quote_scroll_and_missing(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    env["brain"].script = ["valid", "omit:a004", "omit:a004", "valid"]
    try:
        client = _app().test_client()
        run_id = _create(client, key="four", agent_count=4, round_count=1)
        slots = build_slots(4, seed=1)
        candidates = {
            "a001": "reply",
            "a002": "quote",
            "a003": "none",
            "a004": "reply",
        }
        summary = execute_loop(run_id, seed=1, clock=Clock(), candidates=candidates)
        by_id = {row["agent_id"]: row for row in _actions(run_id)}
        assert by_id["a001"]["action"] == "reply" and by_id["a001"]["source"] == "model"
        assert by_id["a002"]["action"] == "quote" and by_id["a002"]["source"] == "model"
        assert by_id["a003"]["action"] == "none" and by_id["a003"]["source"] == "rule"
        assert by_id["a004"]["outcome"] == "missing"
        assert by_id["a004"]["action"] is None
        assert summary["waves"][0]["model_items"] == 3
        assert summary["waves"][0]["attempts"] == 2
        assert slots[0]["agent_id"] == "a001"
    finally:
        env["server"].shutdown()


def test_rejects_invisible_self_like_duplicate_and_keeps_locked_repairs(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    env["brain"].script = ["valid", "illegal-mix", "illegal-mix", "valid"]
    try:
        client = _app().test_client()
        illegal = _create(client, key="illegal", agent_count=3, round_count=1)
        execute_loop(
            illegal,
            seed=1,
            clock=Clock(),
            candidates={agent: "like" for agent in ("a001", "a002", "a003")},
        )
        by_id = {row["agent_id"]: row for row in _actions(illegal)}
        assert by_id["a001"]["outcome"] == "missing"
        assert "invisible_or_future_target" in by_id["a001"]["payload_json"]
        assert "self_like" in by_id["a002"]["payload_json"]
        assert "invisible_or_future_target" in by_id["a003"]["payload_json"]
        assert by_id["a003"]["outcome"] == "missing"
        assert by_id["a003"]["action"] is None
        assert all(row["action"] is None for row in by_id.values())

        env["brain"].script = ["valid", "duplicate", "duplicate", "valid"]
        duplicated = _create(client, key="duplicate", agent_count=2, round_count=1)
        execute_loop(
            duplicated,
            seed=1,
            clock=Clock(),
            candidates={"a001": "reply", "a002": "reply"},
        )
        dup_rows = _actions(duplicated)
        assert {row["outcome"] for row in dup_rows} == {"missing"}
        assert all(row["action"] is None for row in dup_rows)
        assert any("duplicate_id" in row["payload_json"] for row in dup_rows)

        env["brain"].script = ["valid", "lock-check", "lock-check", "valid"]
        locked = _create(client, key="locked", agent_count=2, round_count=1)
        execute_loop(
            locked,
            seed=9,
            clock=Clock(),
            candidates={"a001": "reply", "a002": "reply"},
        )
        locked_rows = {row["agent_id"]: row for row in _actions(locked)}
        assert locked_rows["a001"]["outcome"] == "completed"
        assert REPLY_TEXT in locked_rows["a001"]["payload_json"]
        assert REWRITTEN not in locked_rows["a001"]["payload_json"]
        assert locked_rows["a002"]["outcome"] == "missing"
        assert locked_rows["a002"]["action"] is None
    finally:
        env["server"].shutdown()


def test_private_bleed_is_recorded_with_shared_context_risk(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    env["brain"].script = ["valid", "bleed", "valid"]
    try:
        client = _app().test_client()
        run_id = _create(client, key="bleed", agent_count=2, round_count=1)
        summary = execute_loop(
            run_id,
            seed=1,
            clock=Clock(),
            candidates={"a001": "reply", "a002": "reply"},
        )
        risk = summary["shared_context_risk"][0]
        assert risk["physical_isolation"] is False
        assert risk["shared_model_context"] is True
        assert risk["private_bleed"]
        assert risk["private_bleed"][0]["source_agent_id"] == "a002"
        assert "物理隔离" in risk["residual_risk"]
        items = [
            {"agent_id": "a001", "persona": "甲" * 60, "draft_text": DRAFT, "visible_timeline": []},
            {"agent_id": "a002", "persona": "乙" * 60, "draft_text": DRAFT, "visible_timeline": []},
        ]
        assert private_bleed(items, [{
            "agent_id": "a001",
            "action": {"text": "乙" * 12},
        }])
        assert shared_context_risk(2, [])["physical_isolation"] is False
    finally:
        env["server"].shutdown()


def test_public_timeline_contains_only_earlier_waves(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(client, key="timeline", agent_count=4, round_count=2)
        execute_loop(
            run_id,
            seed=1,
            clock=Clock(),
            candidates={"a001": "reply", "a002": "quote", "a003": "like", "a004": "none"},
        )
        with connect() as conn:
            waves = [
                json.loads(row["body_json"])
                for row in conn.execute(
                    """
                    SELECT body_json FROM artifacts
                    WHERE run_id = ? AND kind = 'wave_input'
                    ORDER BY created_at
                    """,
                    (run_id,),
                )
            ]
            reply = conn.execute(
                """
                SELECT action_id FROM actions
                WHERE run_id = ? AND agent_id = 'a001' AND action = 'reply'
                """,
                (run_id,),
            ).fetchone()
            quote = conn.execute(
                """
                SELECT action_id FROM actions
                WHERE run_id = ? AND agent_id = 'a002' AND action = 'quote'
                """,
                (run_id,),
            ).fetchone()
        first_ids = {entry["id"] for entry in waves[0]["items"][0]["visible_timeline"]}
        second_ids = {entry["id"] for entry in waves[1]["items"][0]["visible_timeline"]}
        assert first_ids == {"post_0"}
        assert reply["action_id"] in second_ids
        assert quote["action_id"] in second_ids
        kinds = {entry["id"]: entry["kind"] for entry in waves[1]["items"][0]["visible_timeline"]}
        assert kinds[reply["action_id"]] == "reply"
        assert kinds[quote["action_id"]] == "quote"
        assert all(entry["kind"] != "reply" or entry["id"] == reply["action_id"] for entry in waves[1]["items"][0]["visible_timeline"])
    finally:
        env["server"].shutdown()


def test_time_and_call_truncation_do_not_invent_exposures(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        clock = Clock()
        timed = _create(client, key="timed")

        def jump():
            with connect() as conn:
                deadline = conn.execute(
                    "SELECT deadline_ms FROM runs WHERE run_id = ?",
                    (timed,),
                ).fetchone()["deadline_ms"]
            clock.now_ms = deadline - 240_000 - 5_000

        summary = execute_loop(timed, seed=7, clock=clock, after_persona=jump)
        assert summary["waves"] == []
        assert [item["round"] for item in summary["waves_skipped"]] == [1, 2, 3]
        assert _sent(timed, "persona") == 1
        assert _sent(timed, "agent") == 0
        assert _sent(timed, "report") == 1
        with connect() as conn:
            exposures = conn.execute(
                "SELECT COUNT(*) AS n FROM exposures WHERE run_id = ?",
                (timed,),
            ).fetchone()["n"]
        assert exposures == 0
        assert env["brain"].ollama_calls == 0

        capped = _create(client, key="capped")
        with connect() as conn:
            for index in range(7):
                conn.execute(
                    """
                    INSERT INTO provider_requests (
                        request_id, run_id, role, logical_batch, attempt_no, status,
                        result_known, created_at, day_key
                    ) VALUES (?, ?, 'agent', ?, 1, 'completed', 1, 't', '2026-09-29')
                    """,
                    (f"prefill-{index}", capped, f"prefill-{index}"),
                )
        capped_summary = execute_loop(capped, seed=8, clock=Clock())
        assert capped_summary["waves"] == []
        assert capped_summary["waves_skipped"]
        assert _sent(capped, "persona") == 1
        assert _sent(capped, "agent") == 7
        with connect() as conn:
            wave_sent = conn.execute(
                """
                SELECT COUNT(*) AS n FROM provider_requests
                WHERE run_id = ? AND logical_batch LIKE 'wave-%'
                  AND status IN ('completed', 'failed', 'uncertain', 'in_flight')
                """,
                (capped,),
            ).fetchone()["n"]
            exposures = conn.execute(
                "SELECT COUNT(*) AS n FROM exposures WHERE run_id = ?",
                (capped,),
            ).fetchone()["n"]
        assert wave_sent == 0
        assert exposures == 0
        assert _sent(capped, "report") == 1
    finally:
        env["server"].shutdown()


def test_subscription_only_uses_the_same_loop_on_the_chosen_cli(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    monkeypatch.setenv("SUBSCRIPTION_CLI", "codex")
    try:
        client = _app().test_client()
        run_id = _create(client, key="sub-only", agent_count=3, round_count=1, execution_profile="subscription-only")
        before = env["brain"].ollama_calls
        summary = execute_loop(
            run_id,
            seed=1,
            clock=Clock(),
            candidates={"a001": "reply", "a002": "none", "a003": "like"},
        )
        assert summary["status"] == "complete"
        assert env["brain"].ollama_calls == before
        with connect() as conn:
            channels = {
                row["role"]: row["channel"]
                for row in conn.execute(
                    """
                    SELECT role, channel FROM provider_requests
                    WHERE run_id = ? AND status = 'completed'
                    """,
                    (run_id,),
                )
            }
        assert channels["persona"] == "codex_cli"
        assert channels["agent"] == "codex_cli"
        assert channels["report"] == "codex_cli"
        assert _sent(run_id) == 3
    finally:
        env["server"].shutdown()


def test_persona_repair_failure_uses_template_and_does_not_cache_it(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    env["brain"].script = ["not-json", "not-json"]
    try:
        client = _app().test_client()
        run_id = _create(client, key="fallback", agent_count=2, round_count=1)
        summary = execute_loop(
            run_id,
            seed=4,
            clock=Clock(),
            candidates={"a001": "none", "a002": "none"},
        )
        assert summary["persona"]["fallback"] is True
        assert _sent(run_id, "persona") == 2
        with connect() as conn:
            cached = conn.execute("SELECT COUNT(*) AS n FROM persona_cache").fetchone()["n"]
            stored = json.loads(conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_batch'",
                (run_id,),
            ).fetchone()["body_json"])
        assert cached == 0
        assert stored["fallback_ids"] == ["a001", "a002"]
        assert all(item["persona_source"] == "persona_fallback" for item in stored["personas"])
        assert all(item["display_name"].startswith("虚构") for item in stored["personas"])
    finally:
        env["server"].shutdown()
