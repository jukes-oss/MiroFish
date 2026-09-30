"""M4 report documents and the worker that starts the loop.

The stand-in CLI and stand-in Ollama never leave the machine. N=5 is not run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts.check_contracts import check_report, load_schemas

from app.tweet.audience import build_slots
from app.tweet.db import connect
from app.tweet.loop import execute_loop, persona_cli_schema
from app.tweet.model_json import parse_model_object
from app.tweet.report import (
    order_top_replies,
    report_cli_extra_args,
    report_object_schema,
    rewrite_problems,
    semantic_problems,
)
from app.tweet.worker import WORKER_ID, advance_once, drain, refresh_lease
from test_m3_tweet_loop import DRAFT, Clock, _app, _create, _ready, _sent, seed_persona_cache

BACKEND = Path(__file__).resolve().parents[1]
_, VALIDATORS = load_schemas()


def _valid_rewrites(draft: str) -> list[dict]:
    snippet = draft[0]

    def one(variant: str, prefix: str) -> dict:
        return {
            "variant": variant,
            "text": prefix + draft,
            "what_changed": "保留原意，只改写法。",
            "changed_spans": [{"start": 0, "end": 1, "text": snippet}],
            "expected_effect": {
                "hypothesis": "可能让口气更像个人说法。",
                "tradeoff": "号召感会变弱。",
                "simulation_verified": False,
            },
        }

    return [one("preserve_claim", "个人觉得"), one("add_boundaries", "在一些情况下")]


def _evidence(draft: str) -> dict:
    return {
        "draft": draft,
        "actions": {
            "act-reply": {"action": "reply", "circle": "tech"},
            "act-quote": {"action": "quote", "circle": "tech"},
        },
    }


def test_semantic_layer_rejects_fake_group_quote_and_emoji_span():
    draft = "好👍好"
    evidence = _evidence(draft)
    fake = {
        "rewrites": _valid_rewrites(draft),
        "top_replies": [{
            "action_id": "act-fake",
            "group_ids": ["tech"],
        }],
    }
    assert "fake_citation" in semantic_problems(fake, evidence)

    wrong = {
        "rewrites": _valid_rewrites(draft),
        "top_replies": [{
            "action_id": "act-reply",
            "group_ids": ["crypto"],
        }],
    }
    assert "wrong_group" in semantic_problems(wrong, evidence)

    quoted = {
        "rewrites": _valid_rewrites(draft),
        "top_replies": [{
            "action_id": "act-quote",
            "group_ids": ["tech"],
        }],
    }
    assert "quote_as_reply" in semantic_problems(quoted, evidence)

    bad_span = {
        "rewrites": [{
            "variant": "preserve_claim",
            "text": "改写甲" + draft,
            "what_changed": "跨度对不上。",
            "changed_spans": [{"start": 1, "end": 3, "text": "👍"}],
            "expected_effect": {
                "hypothesis": "无依据。",
                "tradeoff": "不能用。",
                "simulation_verified": False,
            },
        }, {
            "variant": "change_style",
            "text": "改写乙" + draft,
            "what_changed": "另一条写法。",
            "changed_spans": [{"start": 0, "end": 1, "text": "好"}],
            "expected_effect": {
                "hypothesis": "无依据。",
                "tradeoff": "不能用。",
                "simulation_verified": False,
            },
        }],
    }
    assert "bad_span" in semantic_problems(bad_span, evidence)
    assert semantic_problems({"rewrites": _valid_rewrites(draft)}, evidence) == []


def test_top_replies_sort_by_like_rate_then_likes_then_id():
    ordered = order_top_replies([
        {
            "action_id": "b",
            "simulated_likes": 1,
            "shown_to": 10,
            "rank_basis": "simulated_like_rate",
        },
        {
            "action_id": "a",
            "simulated_likes": 5,
            "shown_to": 10,
            "rank_basis": "simulated_like_rate",
        },
        {
            "action_id": "c",
            "simulated_likes": 9,
            "shown_to": 0,
            "rank_basis": "unvalidated_candidate",
        },
        {
            "action_id": "d",
            "simulated_likes": 4,
            "shown_to": 10,
            "rank_basis": "simulated_like_rate",
        },
    ])
    assert [item["action_id"] for item in ordered] == ["a", "d", "b", "c"]


def test_worker_drain_returns_a_schema_valid_report(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(client, key="worker-report", agent_count=4, round_count=1)
        seed_persona_cache(run_id, seed=1)
        assert drain() == 1
        fetched = client.get(f"/api/tweet/runs/{run_id}")
        assert fetched.json["run"]["status"] in ("complete", "degraded")
        report = client.get(f"/api/tweet/runs/{run_id}/report")
        document = report.json["report"]
        assert report.json["message"] is None
        assert check_report(document, VALIDATORS) == []
        assert document["schema_version"] == "2.0"
        assert "模拟备忘" in document["limitations"][0]
        assert document["confidence"]["calibration_status"] == "uncalibrated"
        assert document["confidence"]["level"] == "low"
        assert document["scope"]["calibrated"] is False
        assert "currency" not in document["usage"]
        assert drain() == 0
    finally:
        env["server"].shutdown()


def test_all_scroll_report_is_complete_with_empty_top_replies(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(client, key="scroll", agent_count=6, round_count=2)
        seed_persona_cache(run_id, seed=2)
        slots = build_slots(6, seed=2)
        summary = execute_loop(
            run_id,
            seed=2,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["status"] == "complete"
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["status"] == "complete"
        assert document["top_replies"] == []
        assert document["metrics"]["action_counts"]["none"] == 6
        assert document["metrics"]["missing_agents"] == 0
        assert document["engagement"]["tier"] == "low"
        assert document["degradation_reasons"] == []
        assert 2 <= len({item["text"] for item in document["rewrites"]}) <= 3
        assert all(item["expected_effect"]["simulation_verified"] is False for item in document["rewrites"])
        usage = document["usage"]
        assert usage["physical_requests"] == _sent(run_id)
        assert sum(item["requests"] for item in usage["channels"]) == usage["physical_requests"]
        assert usage["physical_requests"] + usage["reserved_requests"] <= usage["request_limit"]
    finally:
        env["server"].shutdown()


def test_quotes_are_not_listed_as_top_replies(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        client = _app().test_client()
        run_id = _create(client, key="quotes", agent_count=4, round_count=2)
        seed_persona_cache(run_id, seed=3)
        execute_loop(
            run_id,
            seed=3,
            clock=Clock(),
            candidates={"a001": "reply", "a002": "quote", "a003": "reply", "a004": "none"},
        )
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        slots = build_slots(4, seed=3)
        with connect() as conn:
            rows = conn.execute(
                "SELECT action_id, agent_id, action FROM actions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        by_action = {row["action_id"]: row for row in rows}
        assert document["top_replies"]
        assert all(by_action[item["action_id"]]["action"] == "reply" for item in document["top_replies"])
        circles = {slot["agent_id"]: slot["circle"] for slot in slots}
        assert all(item["group_ids"] == [circles[item["agent_id"]]] for item in document["top_replies"])
    finally:
        env["server"].shutdown()


def test_failed_repair_is_legal_degraded_without_filler(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    env["brain"].script = ["valid", "bad-report", "bad-report"]
    try:
        client = _app().test_client()
        run_id = _create(client, key="bad-report", agent_count=2, round_count=1)
        seed_persona_cache(run_id, seed=5)
        slots = build_slots(2, seed=5)
        summary = execute_loop(
            run_id,
            seed=5,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["status"] == "degraded"
        assert _sent(run_id, "report") == 2
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert document["status"] == "degraded"
        assert document["rewrites"] == []
        blob = json.dumps(document, ensure_ascii=False)
        assert "act-fake" not in blob
        assert "没有编造" in blob
        assert document["degradation_reasons"]
    finally:
        env["server"].shutdown()


def test_owned_active_run_is_not_restarted(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        _app()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, idempotency_key, request_hash, status, draft_text,
                    author_context, audience_version, agent_count, round_count,
                    execution_profile, subscription_cli, schema_version,
                    cancel_requested, worker_lease_owner, worker_lease_expires_ms,
                    created_at, updated_at
                ) VALUES (
                    'owned-run', 'owned-key', 'hash', 'preparing', '草稿',
                    '', 'zh_x_v1', 4, 1, 'mixed', 'grok', '2.0',
                    0, ?, 9999999999999, 't', 't'
                )
                """,
                (WORKER_ID,),
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, idempotency_key, request_hash, status, draft_text,
                    author_context, audience_version, agent_count, round_count,
                    execution_profile, subscription_cli, schema_version,
                    cancel_requested, created_at, updated_at
                ) VALUES (
                    'queued-run', 'queued-key', 'hash2', 'queued', '草稿',
                    '', 'zh_x_v1', 4, 1, 'mixed', 'grok', '2.0',
                    0, 't', 't'
                )
                """
            )
        assert advance_once(now_ms=1_000) is False
        with connect() as conn:
            owned = conn.execute("SELECT status FROM runs WHERE run_id = 'owned-run'").fetchone()
            queued = conn.execute("SELECT status FROM runs WHERE run_id = 'queued-run'").fetchone()
            requests = conn.execute("SELECT COUNT(*) AS n FROM provider_requests").fetchone()["n"]
        assert owned["status"] == "preparing"
        assert queued["status"] == "queued"
        assert requests == 0
        refresh_lease("owned-run", 5_000)
        with connect() as conn:
            expires = conn.execute(
                "SELECT worker_lease_expires_ms FROM runs WHERE run_id = 'owned-run'"
            ).fetchone()["worker_lease_expires_ms"]
        assert expires == 5_000 + 60_000
    finally:
        env["server"].shutdown()


def test_eval_contract_layer_covers_both_profiles_and_leaves_n5_unverified():
    for profile, agent in (("mixed", "agent=ollama"), ("subscription-only", "agent=subscription_cli")):
        completed = subprocess.run(
            [sys.executable, "-m", "eval.run", "--profile", profile, "--suite", "contract"],
            cwd=BACKEND,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert agent in completed.stdout
        assert "契约样例：通过" in completed.stdout
        assert "模拟备忘" in completed.stdout
        assert "N=5 模型实验：待验" in completed.stdout
        assert "预测效果" not in completed.stdout
    for suite in ("dev", "holdout"):
        completed = subprocess.run(
            [sys.executable, "-m", "eval.run", "--profile", "mixed", "--suite", suite],
            cwd=BACKEND,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout
        assert "待验" in completed.stdout
        assert "未调用模型" in completed.stdout
        assert "预测效果" not in completed.stdout


def _arm_report_stdout(env, tmp_path, monkeypatch, generation: str, repair: str):
    """Stand-in logs argv and returns the report sentences. Persona stays cached."""

    log_path = tmp_path / "report-argv.jsonl"
    env["grok"].write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, os, sys",
            "prompt = sys.argv[2] if len(sys.argv) > 2 else ''",
            "with open(os.environ['ARGV_LOG'], 'a', encoding='utf-8') as handle:",
            "    handle.write(json.dumps(sys.argv, ensure_ascii=False) + '\\n')",
            "if 'MIROFISH_REPAIR' in prompt:",
            "    sys.stdout.write(os.environ['FAKE_REPAIR'])",
            "else:",
            "    sys.stdout.write(os.environ['FAKE_GENERATION'])",
            "",
        ]),
        encoding="utf-8",
    )
    env["grok"].chmod(0o755)
    monkeypatch.setenv("ARGV_LOG", str(log_path))
    monkeypatch.setenv("FAKE_GENERATION", generation)
    monkeypatch.setenv("FAKE_REPAIR", repair)
    return log_path


def _assert_report_command(argv):
    assert argv[1] == "-p"
    assert '"task":"report_stats"' in argv[2]
    assert argv[3:] == report_cli_extra_args()
    raw_schema = argv[argv.index("--json-schema") + 1]
    schema = json.loads(raw_schema)
    assert schema == report_object_schema()
    assert schema["type"] == "object"
    assert schema["title"] == "report"
    assert schema != persona_cli_schema()
    assert "display_name" not in schema.get("properties", {})


def test_chinese_sentence_does_not_become_a_report(tmp_path, monkeypatch):
    """Exit 0 prose is started, stays a json failure, and is not stored as rewrites."""

    env = _ready(tmp_path, monkeypatch)
    generation = "先按草稿的码点把可改切片对齐，再只产出要求的 rewrites JSON。"
    repair = "改写跨度按草稿的码点切片核对后再给出 JSON。"
    log_path = _arm_report_stdout(env, tmp_path, monkeypatch, generation, repair)
    try:
        assert parse_model_object(generation) is None
        assert rewrite_problems(None, {"draft": DRAFT}) == ["json"]
        client = _app().test_client()
        run_id = _create(client, key="report-prose", agent_count=2, round_count=1)
        seed_persona_cache(run_id, seed=8)
        slots = build_slots(2, seed=8)
        summary = execute_loop(
            run_id,
            seed=8,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["waves"][0]["model_items"] == 0
        assert summary["waves"][0]["errors"] == []
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert document["rewrites"] == []
        assert document["status"] == "degraded"
        assert _sent(run_id, "report") == 2
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert all('"task":"report_stats"' in row[2] for row in rows)
        assert "MIROFISH_REPAIR" in rows[1][2]
        assert "不要先写一句中文" in rows[0][2]
        assert "不要跑脚本数码点" in rows[0][2]
        assert "码点切片" not in rows[0][2]
        for row in rows:
            _assert_report_command(row)
    finally:
        env["server"].shutdown()


def test_valid_report_json_is_kept_from_the_started_process(tmp_path, monkeypatch):
    """A schema-valid rewrites object from the started process is kept."""

    env = _ready(tmp_path, monkeypatch)
    text = json.dumps({"rewrites": _valid_rewrites(DRAFT)}, ensure_ascii=False)
    log_path = _arm_report_stdout(env, tmp_path, monkeypatch, text, text)
    try:
        client = _app().test_client()
        run_id = _create(client, key="report-json", agent_count=2, round_count=1)
        seed_persona_cache(run_id, seed=9)
        slots = build_slots(2, seed=9)
        summary = execute_loop(
            run_id,
            seed=9,
            clock=Clock(),
            candidates={slot["agent_id"]: "none" for slot in slots},
        )
        assert summary["status"] == "complete"
        document = client.get(f"/api/tweet/runs/{run_id}/report").json["report"]
        assert check_report(document, VALIDATORS) == []
        assert len(document["rewrites"]) == 2
        assert _sent(run_id, "report") == 1
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        _assert_report_command(rows[0])
        assert "MIROFISH_REPAIR" not in rows[0][2]
    finally:
        env["server"].shutdown()
