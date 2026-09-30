"""M1 tweet runs: local persistence and channel checks, no network and no API keys."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import create_app
from app.tweet.db import connect
from app.tweet.worker import WORKER_ID, advance_once, drain, recover_stale_leases


BACKEND = Path(__file__).resolve().parents[1]


def _ready_channels(tmp_path, monkeypatch):
    cli = tmp_path / "grok"
    login = tmp_path / "login"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    login.write_text("logged-in\n", encoding="utf-8")
    monkeypatch.setenv("TWEET_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("TWEET_WORKER_MODE", "manual")
    monkeypatch.setenv("SUBSCRIPTION_CLI", "grok")
    monkeypatch.setenv("GROK_CLI_PATH", str(cli))
    monkeypatch.setenv("SUBSCRIPTION_CLI_LOGIN_PATH", str(login))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.8:27b-mxfp8")
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def _app():
    app = create_app()
    app.config.update(TESTING=True)
    return app


def _payload(**overrides):
    body = {
        "idempotency_key": "run-key-1",
        "draft_text": "所有人都该用AI写作。",
        "author_context": "作者背景",
        "audience_version": "zh_x_v1",
        "agent_count": 500,
        "round_count": 9,
    }
    body.update(overrides)
    return body


def test_health_and_completed_run_need_no_zep_or_api_key(tmp_path, monkeypatch):
    _ready_channels(tmp_path, monkeypatch)
    app = _app()
    client = app.test_client()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json["status"] == "ok"
    assert health.json["channels_required_to_browse"] is False
    assert health.json["generic_mode"]["available"] is False
    assert "不会改用云端图谱" in health.json["generic_mode"]["reason"]

    created = client.post("/api/tweet/runs", json=_payload())
    assert created.status_code == 202
    run_id = created.json["run_id"]
    assert created.json["run"]["agent_count"] == 240
    assert created.json["run"]["round_count"] == 4
    assert drain() >= 1

    fetched = client.get(f"/api/tweet/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json["run"]["status"] == "degraded"
    report = client.get(f"/api/tweet/runs/{run_id}/report")
    assert report.status_code == 200
    document = report.json["report"]
    assert document["schema_version"] == "2.0"
    assert document["status"] == "degraded"
    assert "模拟备忘" in document["confidence"]["basis"]
    from contracts.check_contracts import check_report, load_schemas

    _schemas, validators = load_schemas()
    assert check_report(document, validators) == []

    with connect() as conn:
        actions = conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"]
        requests = conn.execute(
            """
            SELECT COUNT(*) AS n FROM provider_requests
            WHERE status IN ('completed', 'failed', 'uncertain', 'in_flight')
            """
        ).fetchone()["n"]
        columns = [row[1] for row in conn.execute("PRAGMA table_info(budget_ledger)")]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert actions == 0
    assert requests > 0
    assert foreign_keys == 1
    assert str(mode).lower() == "wal"
    assert not any(name for name in columns if "usd" in name or "dollar" in name or name == "currency")


def test_missing_channel_returns_chinese_error_without_asking_for_a_paid_key(tmp_path, monkeypatch):
    _ready_channels(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("SUBSCRIPTION_CLI_LOGIN_PATH", str(tmp_path / "missing-login"))
    client = _app().test_client()

    response = client.post("/api/tweet/runs", json=_payload())
    assert response.status_code == 400
    assert response.json["error_code"] == "channel_config"
    text = response.json["error"]
    assert "订阅 CLI" in text
    assert "Ollama" in text
    assert "不需要提供 OpenAI 或 xAI 的 API key" in text
    assert "LLM_API_KEY" not in text
    assert "ZEP_API_KEY" not in text
    assert "请填写" not in text
    history = client.get("/api/tweet/runs")
    assert history.status_code == 200
    assert history.json["runs"] == []


def test_idempotency_reuses_the_same_run_and_rejects_a_different_body(tmp_path, monkeypatch):
    _ready_channels(tmp_path, monkeypatch)
    client = _app().test_client()
    first = client.post("/api/tweet/runs", json=_payload())
    second = client.post("/api/tweet/runs", json=_payload(agent_count=240))
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json["run_id"] == first.json["run_id"]
    assert first.json["run"]["agent_count"] == 240

    conflict = client.post("/api/tweet/runs", json=_payload(draft_text="另一条草稿"))
    assert conflict.status_code == 409
    assert conflict.json["error_code"] == "idempotency_conflict"

    too_long = client.post("/api/tweet/runs", json=_payload(idempotency_key="other", draft_text="文" * 2001))
    assert too_long.status_code == 400
    author = client.post(
        "/api/tweet/runs",
        json=_payload(idempotency_key="author", author_context="景" * 501),
    )
    assert author.status_code == 400
    audience = client.post(
        "/api/tweet/runs",
        json=_payload(idempotency_key="audience", audience_version="unknown"),
    )
    assert audience.status_code == 400


def test_cancel_stops_the_run_and_restart_can_read_it(tmp_path, monkeypatch):
    _ready_channels(tmp_path, monkeypatch)
    client = _app().test_client()
    created = client.post("/api/tweet/runs", json=_payload(idempotency_key="cancel-me"))
    run_id = created.json["run_id"]
    cancelled = client.post(f"/api/tweet/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json["run"]["status"] == "cancelled"
    assert drain() == 0

    with connect() as conn:
        actions = conn.execute(
            "SELECT COUNT(*) AS n FROM actions WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
        requests = conn.execute(
            "SELECT COUNT(*) AS n FROM provider_requests WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
    assert actions == 0
    assert requests == 0

    restarted = _app().test_client()
    fetched = restarted.get(f"/api/tweet/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json["run"]["status"] == "cancelled"

    deleted = restarted.delete(f"/api/tweet/runs/{run_id}")
    assert deleted.status_code == 200
    missing = restarted.get(f"/api/tweet/runs/{run_id}")
    assert missing.status_code == 404


def test_stale_lease_is_not_replayed(tmp_path, monkeypatch):
    _ready_channels(tmp_path, monkeypatch)
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
                'stale-run', 'stale-key', 'hash', 'running', '草稿',
                '', 'zh_x_v1', 120, 3, 'mixed', 'grok', '2.0',
                0, 'dead-worker', 1, 't', 't'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO provider_requests (
                request_id, run_id, role, attempt_no, channel, status,
                result_known, created_at
            ) VALUES ('req-1', 'stale-run', 'agent', 1, 'ollama', 'in_flight', 0, 't')
            """
        )
        conn.execute(
            """
            INSERT INTO runs (
                run_id, idempotency_key, request_hash, status, draft_text,
                author_context, audience_version, agent_count, round_count,
                execution_profile, subscription_cli, schema_version,
                cancel_requested, worker_lease_owner, worker_lease_expires_ms,
                created_at, updated_at
            ) VALUES (
                'held-run', 'held-key', 'hash2', 'running', '草稿',
                '', 'zh_x_v1', 120, 3, 'mixed', 'grok', '2.0',
                0, 'other-worker', 9999999999999, 't', 't'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO runs (
                run_id, idempotency_key, request_hash, status, draft_text,
                author_context, audience_version, agent_count, round_count,
                execution_profile, subscription_cli, schema_version,
                cancel_requested, created_at, updated_at
            ) VALUES (
                'waiting-run', 'waiting-key', 'hash3', 'queued', '草稿',
                '', 'zh_x_v1', 120, 3, 'mixed', 'grok', '2.0',
                0, 't', 't'
            )
            """
        )

    assert recover_stale_leases(now_ms=1_000) == 1
    with connect() as conn:
        stale = conn.execute("SELECT status FROM runs WHERE run_id = 'stale-run'").fetchone()
        request = conn.execute(
            "SELECT status, result_known FROM provider_requests WHERE request_id = 'req-1'"
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM provider_requests WHERE run_id = 'stale-run'"
        ).fetchone()["n"]
    assert stale["status"] == "degraded"
    assert request["status"] == "uncertain"
    assert request["result_known"] == 0
    assert count == 1
    assert advance_once(now_ms=1_000) is False
    with connect() as conn:
        waiting = conn.execute("SELECT status FROM runs WHERE run_id = 'waiting-run'").fetchone()
    assert waiting["status"] == "queued"
    assert WORKER_ID != "other-worker"


def test_importing_the_app_does_not_import_the_zep_sdk():
    script = r"""
import builtins
import os
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp())
os.environ.pop("ZEP_API_KEY", None)
os.environ.pop("LLM_API_KEY", None)
os.environ["TWEET_WORKER_MODE"] = "manual"
os.environ["TWEET_STATE_DB"] = str(root / "state.sqlite")
os.environ["MODE_DEFAULT"] = "tweet"
os.environ["MEMORY_BACKEND"] = "local"
real_import = builtins.__import__

def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "zep_cloud" or name.startswith("zep_cloud."):
        raise ImportError("zep sdk blocked")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded
from app import create_app
app = create_app()
response = app.test_client().get("/health")
assert response.status_code == 200, response.data
body = response.get_json()
assert body["generic_mode"]["available"] is False
assert "云端" in body["generic_mode"]["reason"]
print("ok")
"""
    env = os.environ.copy()
    env.pop("ZEP_API_KEY", None)
    env.pop("LLM_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_validate_does_not_require_keys_for_tweet_mode(monkeypatch):
    from app.config import Config

    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("MODE_DEFAULT", "tweet")
    monkeypatch.setenv("MEMORY_BACKEND", "local")
    assert Config.validate() == []
