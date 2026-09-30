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
    interpret_personas,
    persona_array_schema,
    persona_call_seconds,
    persona_cli_extra_args,
    persona_groups,
    persona_repair_fits,
)
from app.tweet.model_json import parse_model_object
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
                SELECT status FROM provider_requests
                WHERE run_id = ? AND role = 'persona'
                """,
                (run_id,),
            ).fetchall()
            cached = conn.execute("SELECT COUNT(*) AS n FROM persona_cache").fetchone()["n"]
        assert stored["fallback_ids"]
        assert rows
        assert {row["status"] for row in rows} == {"uncertain"}
        assert cached == 0
    finally:
        env["server"].shutdown()


def _persona_body(agent_id: str) -> str:
    sentence = (
        f"槽位{agent_id}平时写代码，看到具体句子才会开口。没有亲身经历时不跟着下结论，"
        "也不把别人的键盘事故当成自己的故事。先看完再决定是否开口。"
    )
    assert len(sentence) >= 60
    head, tail = sentence[:28], sentence[28:]
    return head + "\n" + tail


def messy_cli_persona_reply(slots: list[dict]) -> str:
    """Plain `grok -p` text: thinking, a broken brace, then a fenced array.

    The array is what the model writes. Strings wrap onto the next line and
    the last field has a trailing comma. It is not one clean object.
    """

    blocks = []
    for slot in slots:
        agent_id = slot["agent_id"]
        blocks.append(
            "{\n"
            f'    "agent_id": "{agent_id}",\n'
            '    "display_name": "键盘旁的人",\n'
            '    "bio": "写代码",\n'
            f'    "persona": "{_persona_body(agent_id)}",\n'
            '    "avoid_speaking_when": "没句子就沉默",\n'
            "  }"
        )
    array = "[\n" + ",\n".join(blocks) + "\n]"
    return (
        '<think>四个槽位，先列字段 {"display_name":"太早"}</think>\n'
        "结果如下，前面的括号作废 {\n"
        "```json\n"
        + array
        + "\n```\n"
        "以上。\n"
    )


def _prompt_payload(prompt: str) -> dict:
    start = prompt.find("{")
    loaded, _end = json.JSONDecoder().raw_decode(prompt[start:])
    return loaded


def test_messy_four_person_cli_json_locks_without_a_repair_object():
    slots = build_slots(4, seed=1)
    text = messy_cli_persona_reply(slots)
    bare = text[text.find("["):text.rfind("]") + 1]
    assert parse_model_object(bare) is None
    locked, errors = interpret_personas(text, slots)
    assert errors == []
    assert set(locked) == {slot["agent_id"] for slot in slots}
    assert all(item["display_name"].startswith("虚构") for item in locked.values())
    assert all(item["persona_source"] == "subscription_cli" for item in locked.values())
    assert all(len(item["persona"]) >= 60 for item in locked.values())
    window = RUN_MAX_WALL_SECONDS - REPORT_RESERVE_WALL_SECONDS - CLEANUP_RESERVE_SECONDS
    assert persona_repair_fits(window, persona_calls_after=2, wave_count=1) is False
    assert persona_repair_fits(window, persona_calls_after=0, wave_count=1) is True


def test_twelve_person_messy_cli_reply_skips_repair_and_keeps_personas(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)

    def reply(**kwargs):
        prompt = kwargs.get("prompt") or ""
        if '"task":"persona_batch"' in prompt:
            assert "MIROFISH_REPAIR" not in prompt
            payload = _prompt_payload(prompt)
            return AdapterOutcome(started=True, text=messy_cli_persona_reply(payload["slots"]), exit_code=0)
        return invoke_subscription_cli(**kwargs)

    monkeypatch.setattr(gateway, "invoke_subscription_cli", reply)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="twelve-messy",
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
        assert summary["waves_skipped"] == []
        assert _sent(run_id, "persona") == 3
        assert _sent(run_id, "agent") == 1
        actions = {row["agent_id"]: row for row in _actions(run_id)}
        assert len(actions) == 12
        assert actions["a001"]["source"] == "model"
        assert actions["a001"]["action"] == "reply"
        assert all(
            actions[slot["agent_id"]]["source"] == "rule" and actions[slot["agent_id"]]["action"] == "none"
            for slot in slots
            if slot["agent_id"] != "a001"
        )
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT attempt_no, logical_batch, status FROM provider_requests
                WHERE run_id = ? AND role = 'persona'
                ORDER BY logical_batch
                """,
                (run_id,),
            ).fetchall()
        assert [row["logical_batch"] for row in rows] == ["persona-1", "persona-2", "persona-3"]
        assert {row["attempt_no"] for row in rows} == {1}
        assert {row["status"] for row in rows} == {"completed"}
    finally:
        env["server"].shutdown()


def test_persona_repair_timeout_does_not_drop_the_wave(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    clock = Clock()

    def reply(**kwargs):
        prompt = kwargs.get("prompt") or ""
        if '"task":"persona_batch"' in prompt and "MIROFISH_REPAIR" in prompt:
            clock.now_ms += REQUEST_TIMEOUT_SECONDS * 1000
            return AdapterOutcome(started=True, text="", exit_code=-9, error_code="timeout")
        if '"task":"persona_batch"' in prompt:
            clock.now_ms += 60_000
            return AdapterOutcome(started=True, text="not-json", exit_code=0)
        return invoke_subscription_cli(**kwargs)

    monkeypatch.setattr(gateway, "invoke_subscription_cli", reply)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="twelve-repair-budget",
            agent_count=12,
            round_count=1,
            draft=DRAFT,
            author_context=AUTHOR,
        )
        slots = build_slots(12, seed=4)
        summary = execute_loop(
            run_id,
            seed=4,
            clock=clock,
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["waves_skipped"] == []
        assert _sent(run_id, "persona") == 3
        assert _sent(run_id, "agent") == 1
        actions = _actions(run_id)
        assert len(actions) == 12
        assert {row["source"] for row in actions} == {"rule"}
        assert {row["action"] for row in actions} == {"none"}
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["status"] == "degraded"
        assert "人设使用了短句模板，不是订阅通道生成。" in document["degradation_reasons"]
        assert "有波次没有开始，没有补写曝光。" not in document["degradation_reasons"]
        assert "模拟备忘" in document["limitations"][0]
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT status, attempt_no, logical_batch, attempt_kind
                FROM provider_requests
                WHERE run_id = ? AND role = 'persona'
                ORDER BY logical_batch
                """,
                (run_id,),
            ).fetchall()
            stored = json.loads(conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_batch'",
                (run_id,),
            ).fetchone()["body_json"])
        assert [row["logical_batch"] for row in rows] == ["persona-1", "persona-2", "persona-3"]
        assert {row["attempt_no"] for row in rows} == {1}
        assert {row["attempt_kind"] for row in rows} == {"generation"}
        assert {row["status"] for row in rows} == {"completed"}
        assert stored["fallback_ids"] == [slot["agent_id"] for slot in slots]
        assert all(item["persona_source"] == "persona_fallback" for item in stored["personas"])
        window = RUN_MAX_WALL_SECONDS - REPORT_RESERVE_WALL_SECONDS - CLEANUP_RESERVE_SECONDS
        assert persona_repair_fits(window, persona_calls_after=2, wave_count=1) is False
    finally:
        env["server"].shutdown()


def test_explanatory_prose_does_not_become_a_persona():
    slots = [{"agent_id": f"a00{index}"} for index in range(1, 5)]
    notes = [
        "先看项目里 persona 批次的字段约定，再按码点限制生成。码点要卡死，我先用脚本把每条人设数到正好 60。",
        "我先核对这个人设批次的输出字段和字数约束，再按每个槽位生成。字数按码点卡死，我先把四条人设量准再输出。",
        "我先核对这个人设批次的字段和字数约束，再按契约只返回 JSON 数组。",
    ]
    for note in notes:
        locked, errors = interpret_personas(note, slots)
        assert locked == {}
        assert [item["codes"] for item in errors] == [["json"], ["json"], ["json"], ["json"]]
    payload = build_persona_payload(build_slots(4, seed=1), audience_version="zh_x_v1", seed=1)
    contract = payload["output_contract"]["persona"]
    assert contract.startswith("60")
    assert "120" in contract
    assert "不追求正好 60" in contract
    assert "写满即停" not in contract
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "输入已完整，只依据 slots，直接返回裸 JSON 数组。" in encoded
    assert "不查文件、不跑脚本、不写准备说明。" in encoded


PROSE_STDOUT = "我先核对字段，再用脚本数到正好 60。"


def _valid_bare_personas(slots: list[dict]) -> str:
    items = []
    for slot in slots:
        agent_id = slot["agent_id"]
        persona = (
            f"槽位{agent_id}只看公开标签。没有亲身经历时不跟着说话，也不把准备过程写进正文。"
            "先读完可见句子再决定是否开口，没有句子就保持沉默。"
        )
        assert 60 <= len(persona) <= 120
        items.append({
            "agent_id": agent_id,
            "display_name": f"虚构{agent_id}",
            "bio": "公开标签",
            "persona": persona,
            "avoid_speaking_when": "没有具体句子时沉默。",
        })
    return json.dumps(items, ensure_ascii=False)


def _arm_once(env, tmp_path, monkeypatch, text: str):
    """Stand-in exits 0 with one fixed stdout. A second persona start fails."""

    log_path = tmp_path / "argv.jsonl"
    count_path = tmp_path / "persona-starts.txt"
    env["grok"].write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, os, sys, urllib.request",
            "prompt = sys.argv[2] if len(sys.argv) > 2 else ''",
            "with open(os.environ['ARGV_LOG'], 'a', encoding='utf-8') as handle:",
            "    handle.write(json.dumps(sys.argv, ensure_ascii=False) + '\\n')",
            "if '\"task\":\"persona_batch\"' in prompt:",
            "    count_path = os.environ['PERSONA_STARTS']",
            "    seen = 0",
            "    if os.path.exists(count_path):",
            "        seen = int(open(count_path, encoding='utf-8').read() or '0')",
            "    if seen >= 1:",
            "        raise SystemExit('persona generation already used')",
            "    open(count_path, 'w', encoding='utf-8').write(str(seen + 1))",
            "    sys.stdout.write(os.environ['FAKE_STDOUT'])",
            "    raise SystemExit(0)",
            "request = urllib.request.Request(",
            "    os.environ['FAKE_BRAIN'],",
            "    data=json.dumps({'prompt': prompt}).encode('utf-8'),",
            "    headers={'Content-Type': 'application/json'},",
            ")",
            "with urllib.request.urlopen(request, timeout=30) as response:",
            "    sys.stdout.buffer.write(response.read())",
            "",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGV_LOG", str(log_path))
    monkeypatch.setenv("PERSONA_STARTS", str(count_path))
    monkeypatch.setenv("FAKE_STDOUT", text)
    return log_path, count_path


def _logged_argv(log_path):
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    persona = [row for row in rows if len(row) > 2 and '"task":"persona_batch"' in row[2]]
    report = [row for row in rows if len(row) > 2 and '"task":"report_stats"' in row[2]]
    return persona, report


def _assert_started_persona_command(argv):
    """The started persona command is not a bare ``-p``."""

    assert len(argv) > 3
    assert argv[1] == "-p"
    assert '"task":"persona_batch"' in argv[2]
    assert argv[3:] == persona_cli_extra_args()
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema == persona_array_schema()
    assert schema["type"] == "array"
    assert set(schema["items"]["properties"]) == {
        "agent_id",
        "display_name",
        "bio",
        "persona",
        "avoid_speaking_when",
    }
    denied = argv[argv.index("--disallowed-tools") + 1]
    assert denied == "run_terminal_cmd,run_terminal_command"
    assert "--tools" not in argv
    assert "--deny" not in argv


def _assert_one_persona_start(log_path, count_path, run_id: str):
    persona, report = _logged_argv(log_path)
    assert len(persona) == 1
    assert count_path.read_text(encoding="utf-8") == "1"
    _assert_started_persona_command(persona[0])
    assert report
    assert all(row[1] == "-p" and len(row) == 3 for row in report)
    assert all("--json-schema" not in row and "--disallowed-tools" not in row for row in report)
    with connect() as conn:
        repairs = conn.execute(
            """
            SELECT COUNT(*) AS n FROM provider_requests
            WHERE run_id = ? AND role = 'persona' AND attempt_kind = 'repair'
            """,
            (run_id,),
        ).fetchone()["n"]
        generations = conn.execute(
            """
            SELECT COUNT(*) AS n FROM provider_requests
            WHERE run_id = ? AND role = 'persona' AND attempt_kind = 'generation'
            """,
            (run_id,),
        ).fetchone()["n"]
    assert repairs == 0
    assert generations == 1


def test_prose_stdout_starts_once_and_stays_invalid(tmp_path, monkeypatch):
    """Exit 0 prose is started once, locks nothing, and is not repaired."""

    env = _ready(tmp_path, monkeypatch)
    log_path, count_path = _arm_once(env, tmp_path, monkeypatch, PROSE_STDOUT)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="prose-started",
            agent_count=4,
            round_count=1,
            draft=DRAFT,
            author_context=AUTHOR,
        )
        slots = build_slots(4, seed=4)
        summary = execute_loop(
            run_id,
            seed=4,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        locked, errors = interpret_personas(PROSE_STDOUT, slots)
        assert locked == {}
        assert [item["codes"] for item in errors] == [["json"], ["json"], ["json"], ["json"]]
        assert summary["persona"]["stopped"] is False
        assert summary["persona"]["calls"] == 1
        with connect() as conn:
            stored = json.loads(conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_batch'",
                (run_id,),
            ).fetchone()["body_json"])
        assert [item["codes"] for item in stored["errors"]] == [["json"], ["json"], ["json"], ["json"]]
        assert all(item["persona_source"] != "subscription_cli" for item in stored["personas"])
        _assert_one_persona_start(log_path, count_path, run_id)
    finally:
        env["server"].shutdown()


def test_valid_bare_array_starts_once_and_locks_four(tmp_path, monkeypatch):
    """A schema-valid array from the started process is kept. Repair stays 0."""

    env = _ready(tmp_path, monkeypatch)
    slots = build_slots(4, seed=4)
    text = _valid_bare_personas(slots)
    log_path, count_path = _arm_once(env, tmp_path, monkeypatch, text)
    try:
        client = _app().test_client()
        run_id = _create(
            client,
            key="array-started",
            agent_count=4,
            round_count=1,
            draft=DRAFT,
            author_context=AUTHOR,
        )
        summary = execute_loop(
            run_id,
            seed=4,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["persona"]["stopped"] is False
        assert summary["persona"]["fallback"] is False
        assert summary["persona"]["calls"] == 1
        with connect() as conn:
            stored = json.loads(conn.execute(
                "SELECT body_json FROM artifacts WHERE run_id = ? AND kind = 'persona_batch'",
                (run_id,),
            ).fetchone()["body_json"])
        assert stored["errors"] == []
        assert stored["fallback_ids"] == []
        assert [item["agent_id"] for item in stored["personas"]] == [slot["agent_id"] for slot in slots]
        assert all(item["persona_source"] == "subscription_cli" for item in stored["personas"])
        _assert_one_persona_start(log_path, count_path, run_id)
    finally:
        env["server"].shutdown()
