"""M2 gateway: fake CLI and fake Ollama, no network and no API keys."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import create_app
from app.providers.blocked import DirectModelCallBlocked
from app.providers.gateway import (
    apply_late_result,
    generate,
    generate_structured,
    prepare_run,
    usage,
)
from app.providers.profile import load_profile
from app.tweet.db import connect
from app.tweet.worker import recover_stale_leases
from app.utils.llm_client import LLMClient

BACKEND = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND / "app"
SCRIPTS = BACKEND / "scripts"
DRAFT = "草稿哨兵DRAFT-SHOULD-NOT-BE-LOGGED"
EMAIL = "person@example.com"
SECRET = "sk-test-secret-value"

FAKE_CLI = r"""#!/usr/bin/env python3
import json, os, sys, time, fcntl
from pathlib import Path

control = json.loads(Path(os.environ["FAKE_CONTROL"]).read_text(encoding="utf-8"))
record_path = Path(control["record"])
counter_path = Path(control["counter"])
concurrency_path = Path(control["concurrency"])

def locked(path, mutate):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        current = handle.read()
        updated = mutate(current)
        handle.seek(0)
        handle.truncate()
        handle.write(updated)
        handle.flush()

box = {"n": 0}
def bump(current):
    box["n"] = int(current.strip() or "0")
    return str(box["n"] + 1)
locked(counter_path, bump)
modes = control.get("modes") or ["ok"]
mode = modes[min(box["n"], len(modes) - 1)]
prompt = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-p" else ""

def add_record(current):
    rows = json.loads(current or "[]")
    rows.append({
        "argv0": Path(sys.argv[0]).name,
        "has_p": "-p" in sys.argv,
        "tools": os.environ.get("MIROFISH_TOOLS"),
        "sensitive_keys": [key for key in os.environ if any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "EMAIL"))],
        "email_in_values": any("@" in value for value in os.environ.values()),
        "secret_in_values": any(value.startswith("sk-") for value in os.environ.values()),
        "sentinel": "DRAFT-SHOULD-NOT-BE-LOGGED" in prompt,
        "slot_count": prompt.count("slot-"),
        "item_count": prompt.count('"agent_id"'),
        "mode": mode,
    })
    return json.dumps(rows)
locked(record_path, add_record)

def mark(kind):
    with open(concurrency_path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(f"{kind} {time.time():.6f}\n")
        handle.flush()

mark("start")
try:
    time.sleep(float(control.get("sleep") or 0))
    meta = {"internal_attempts": 1}
    body = '{"ok":true}'
    exit_code = 0
    if mode == "timeout":
        time.sleep(30)
    elif mode == "queue":
        meta = {"queued": True, "internal_attempts": 1}
        sys.stdout.buffer.write(("__MIROFISH_META__" + json.dumps(meta) + "\n").encode())
        sys.stdout.buffer.flush()
        time.sleep(float(control.get("queue_sleep") or 0.05))
        sys.stdout.buffer.write(body.encode())
        sys.stdout.buffer.flush()
    elif mode == "queue_timeout":
        meta = {"queued": True, "internal_attempts": 1}
        sys.stdout.buffer.write(("__MIROFISH_META__" + json.dumps(meta) + "\n").encode())
        sys.stdout.buffer.flush()
        time.sleep(30)
    elif mode == "rate":
        meta = {"rate_limited": True, "http_status": 429, "internal_attempts": 1}
        exit_code = 1
    elif mode == "exit":
        exit_code = 2
    elif mode == "bad":
        body = "not-json"
    elif mode == "internal":
        meta = {"internal_attempts": 3}
    elif mode == "big":
        body = "x" * 8000
    elif mode == "crash":
        os.kill(os.getpid(), 9)
    if mode not in {"queue", "queue_timeout", "timeout", "crash"}:
        sys.stdout.buffer.write(("__MIROFISH_META__" + json.dumps(meta) + "\n").encode())
        sys.stdout.buffer.write(body.encode())
        sys.stdout.buffer.flush()
    raise SystemExit(exit_code)
finally:
    mark("end")
"""


class Clock:
    def __init__(self, now_ms: int = 1_700_000_000_000):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def day_key(self) -> str:
        return "2026-09-29"


class OllamaState:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.modes = ["ok"]
        self.headers = []
        self.models = []
        self.bodies = []
        self.sleep = 0.0
        self.concurrency = None

    def pop_mode(self) -> str:
        with self.lock:
            self.calls += 1
            if not self.modes:
                return "ok"
            return self.modes.pop(0)


def _max_overlap(path: Path) -> int:
    events = []
    if not path.exists():
        return 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        kind, stamp = line.split()
        events.append((float(stamp), -1 if kind == "end" else 1))
    events.sort()
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _start_ollama(state: OllamaState, concurrency: Path):
    state.concurrency = concurrency

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            mode = state.pop_mode()
            state.headers.append(dict(self.headers))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {}
            state.models.append(payload.get("model"))
            state.bodies.append(raw.decode("utf-8", errors="replace"))
            with open(concurrency, "a", encoding="utf-8") as handle:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(f"start {time.time():.6f}\n")
                handle.flush()
            try:
                if mode == "timeout":
                    time.sleep(30)
                else:
                    time.sleep(state.sleep)
                if mode == "429":
                    body = b'{"error":{"message":"rate"}}'
                    status = 429
                elif mode == "missing_usage":
                    body = json.dumps(
                        {"choices": [{"message": {"content": "{\"ok\":true}"}}]}
                    ).encode()
                    status = 200
                elif mode == "bad":
                    body = json.dumps(
                        {"choices": [{"message": {"content": "not-json"}}]}
                    ).encode()
                    status = 200
                elif mode == "big":
                    body = json.dumps(
                        {"choices": [{"message": {"content": "x" * 5000}}], "usage": {"total_tokens": 1}}
                    ).encode()
                    status = 200
                else:
                    body = json.dumps(
                        {
                            "choices": [{"message": {"content": "{\"ok\":true}"}}],
                            "usage": {"total_tokens": 3},
                        }
                    ).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with open(concurrency, "a", encoding="utf-8") as handle:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    handle.write(f"end {time.time():.6f}\n")
                    handle.flush()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _ready(tmp_path: Path, monkeypatch, *, cli: str = "grok") -> dict:
    control = tmp_path / "control.json"
    record = tmp_path / "record.json"
    counter = tmp_path / "counter.txt"
    concurrency = tmp_path / "concurrency.log"
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    script.chmod(0o755)
    grok = tmp_path / "grok"
    codex = tmp_path / "codex"
    shutil.copy(script, grok)
    shutil.copy(script, codex)
    grok.chmod(0o755)
    codex.chmod(0o755)
    login = tmp_path / "login"
    login.write_text("logged-in\n", encoding="utf-8")
    state = OllamaState()
    server = _start_ollama(state, concurrency)
    host, port = server.server_address
    base = f"http://{host}:{port}/v1"
    control.write_text(
        json.dumps(
            {
                "record": str(record),
                "counter": str(counter),
                "concurrency": str(concurrency),
                "modes": ["ok"],
                "sleep": 0,
                "queue_sleep": 0.05,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TWEET_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("TWEET_WORKER_MODE", "manual")
    monkeypatch.setenv("FAKE_CONTROL", str(control))
    monkeypatch.setenv("SUBSCRIPTION_CLI", cli)
    monkeypatch.setenv("GROK_CLI_PATH", str(grok))
    monkeypatch.setenv("CODEX_CLI_PATH", str(codex))
    monkeypatch.setenv("SUBSCRIPTION_CLI_LOGIN_PATH", str(login))
    monkeypatch.setenv("OLLAMA_BASE_URL", base)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.8:27b-mxfp8")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("XAI_API_KEY", SECRET)
    monkeypatch.setenv("LOGIN_EMAIL", EMAIL)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    return {
        "control": control,
        "record": record,
        "counter": counter,
        "concurrency": concurrency,
        "server": server,
        "state": state,
        "grok": grok,
        "codex": codex,
    }


def _write_control(env, **updates):
    current = json.loads(Path(env["control"]).read_text(encoding="utf-8"))
    current.update(updates)
    Path(env["control"]).write_text(json.dumps(current), encoding="utf-8")
    if "modes" in updates:
        Path(env["counter"]).write_text("0", encoding="utf-8")


def _app():
    app = create_app()
    app.config.update(TESTING=True)
    return app


def _create(client, *, profile="mixed", key="run-1", draft=DRAFT):
    response = client.post(
        "/api/tweet/runs",
        json={
            "idempotency_key": key,
            "draft_text": draft,
            "author_context": "作者背景",
            "audience_version": "zh_x_v1",
            "execution_profile": profile,
            "agent_count": 120,
            "round_count": 3,
        },
    )
    assert response.status_code == 202, response.json
    return response.json["run_id"]


def _records(env) -> list[dict]:
    path = Path(env["record"])
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8") or "[]")


def _profile(run_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT capability_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return load_profile(row["capability_json"])


def _capture_logs():
    stream = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            stream.append(record.getMessage())

    handler = ListHandler()
    logger = logging.getLogger("mirofish.gateway")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger, handler, stream


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    yield env
    env["server"].shutdown()


def test_role_routing_and_frozen_profile_for_both_eval_configs(stand_in):
    client = _app().test_client()
    mixed = _create(client, profile="mixed", key="mixed")
    only = _create(client, profile="subscription-only", key="only")
    mixed_profile = _profile(mixed)
    only_profile = _profile(only)
    assert mixed_profile["routes"]["persona"]["channel"] == "grok_cli"
    assert mixed_profile["routes"]["persona"]["model_id"] == "grok-4.7"
    assert mixed_profile["routes"]["agent"]["channel"] == "ollama"
    assert mixed_profile["routes"]["agent"]["model_id"] == "qwen3.8:27b-mxfp8"
    assert mixed_profile["routes"]["report"]["channel"] == "grok_cli"
    assert only_profile["routes"]["agent"]["channel"] == "grok_cli"
    assert only_profile["routes"]["ontology"]["channel"] == "ollama"
    for field in ("login", "model_id", "headless_invocation", "ollama_address"):
        assert mixed_profile["unverified"][field] == "待验"
    assert mixed_profile["silent_fallback"] is False

    clock = Clock()
    persona = generate(
        mixed,
        role="persona",
        logical_batch="persona",
        messages=[{"role": "user", "content": " ".join(f"slot-{index:03d}" for index in range(120))}],
        clock=clock,
    )
    wave = generate(
        mixed,
        role="agent",
        logical_batch="wave-1",
        messages=[{"role": "user", "content": json.dumps({"items": [{"agent_id": f"a{i}"} for i in range(12)]})}],
        clock=clock,
    )
    report = generate(
        mixed,
        role="report",
        logical_batch="report-main",
        messages=[{"role": "user", "content": "{\"report\":\"full\"}"}],
        clock=clock,
    )
    sub_wave = generate(
        only,
        role="agent",
        logical_batch="wave-1",
        messages=[{"role": "user", "content": "subscription wave"}],
        clock=clock,
    )
    ontology = generate(
        only,
        role="ontology",
        logical_batch="ontology",
        messages=[{"role": "user", "content": "ontology"}],
        clock=clock,
    )
    rules = generate(
        mixed,
        role="moderator",
        logical_batch="moderator",
        messages=[{"role": "user", "content": "no model"}],
        clock=clock,
    )
    assert persona.sent and persona.channel == "grok_cli" and persona.model_id == "grok-4.7"
    assert wave.sent and wave.channel == "ollama" and wave.model_id == "qwen3.8:27b-mxfp8"
    assert report.sent and report.channel == "grok_cli"
    assert sub_wave.sent and sub_wave.channel == "grok_cli"
    assert ontology.sent and ontology.channel == "ollama"
    assert rules.sent is False and rules.error_code == "rules_only"
    records = _records(stand_in)
    assert records[0]["slot_count"] == 120
    assert records[0]["sentinel"] is False
    assert records[0]["tools"] == "off"
    assert sum(1 for row in records if row["argv0"] == "grok") == 3
    assert stand_in["state"].models == ["qwen3.8:27b-mxfp8", "qwen3.8:27b-mxfp8"]
    assert all(not header.get("Authorization") for header in stand_in["state"].headers)
    before = stand_in["state"].calls
    stand_in["state"].modes = ["429"]
    limited = generate(
        mixed,
        role="agent",
        logical_batch="wave-2",
        messages=[{"role": "user", "content": "again"}],
        clock=clock,
    )
    assert limited.error_code == "rate_limited"
    assert limited.channel == "ollama"
    assert stand_in["state"].calls == before + 1
    assert all(row["argv0"] == "grok" for row in _records(stand_in))


def test_codex_is_used_only_when_chosen_before_the_run(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch, cli="codex")
    try:
        run_id = _create(_app().test_client(), key="codex-run")
        profile = _profile(run_id)
        assert profile["subscription_cli"] == "codex"
        assert profile["routes"]["persona"]["channel"] == "codex_cli"
        assert profile["routes"]["persona"]["model_id"] is None
        assert profile["routes"]["persona"]["model_id_status"] == "unknown"
        result = generate(
            run_id,
            role="persona",
            logical_batch="persona",
            messages=[{"role": "user", "content": "persona"}],
            clock=Clock(),
        )
        assert result.channel == "codex_cli"
        assert _records(env)[0]["argv0"] == "codex"
        monkeypatch.setenv("SUBSCRIPTION_CLI", "grok")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9/v1")
        agent = generate(
            run_id,
            role="agent",
            logical_batch="wave-1",
            messages=[{"role": "user", "content": "reaction"}],
            clock=Clock(),
        )
        report = generate(
            run_id,
            role="report",
            logical_batch="report-main",
            messages=[{"role": "user", "content": "report"}],
            clock=Clock(),
        )
        assert agent.channel == "ollama" and agent.sent
        assert env["state"].calls == 1
        assert report.channel == "codex_cli"
        assert [row["argv0"] for row in _records(env)] == ["codex", "codex"]
    finally:
        env["server"].shutdown()


def test_eight_contending_calls_stay_at_concurrency_one(stand_in):
    stand_in["state"].sleep = 0.08
    _write_control(stand_in, sleep=0.08)
    run_id = _create(_app().test_client(), key="concurrent")
    clock = Clock()
    errors = []

    def worker(index: int):
        try:
            role = "persona" if index < 4 else "agent"
            result = generate(
                run_id,
                role=role,
                logical_batch=f"batch-{index}",
                messages=[{"role": "user", "content": f"item-{index}"}],
                clock=clock,
            )
            if not result.sent:
                errors.append(result.error_code)
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    snapshot = usage(run_id, clock=clock)
    assert snapshot["counted"] <= 10
    assert snapshot["ledger_calls"] <= 10
    assert snapshot["counted"] == 10
    assert _max_overlap(stand_in["concurrency"]) <= 1
    assert stand_in["state"].calls == 4
    assert len(_records(stand_in)) == 4


def test_caps_attempts_deadline_report_reserve_and_day(stand_in):
    client = _app().test_client()
    run_id = _create(client, key="caps")
    clock = Clock()
    prepared = prepare_run(run_id, clock=clock)
    first_deadline = prepared["deadline_ms"]
    clock.now_ms += 5_000
    again = prepare_run(run_id, clock=clock)
    assert again["deadline_ms"] == first_deadline

    results = []
    for index in range(9):
        results.append(
            generate(
                run_id,
                role="agent",
                logical_batch=f"cap-{index}",
                messages=[{"role": "user", "content": "x"}],
                clock=clock,
            )
        )
    assert all(item.sent for item in results[:8])
    assert results[8].sent is False
    assert results[8].error_code == "run_call_cap"
    assert stand_in["state"].calls == 8
    assert usage(run_id, clock=clock)["counted"] == 10

    other = _create(client, key="attempts")
    first = generate(other, role="agent", logical_batch="wave-1", messages=[{"role": "user", "content": "a"}], clock=clock)
    second = generate(other, role="agent", logical_batch="wave-1", messages=[{"role": "user", "content": "b"}], clock=clock)
    third = generate(other, role="agent", logical_batch="wave-1", messages=[{"role": "user", "content": "c"}], clock=clock)
    assert first.sent and second.sent and second.attempt_no == 2
    assert third.sent is False and third.error_code == "batch_attempt_limit"
    calls_after_third = stand_in["state"].calls

    wall = _create(client, key="wall")
    wall_clock = Clock()
    prepared_wall = prepare_run(wall, clock=wall_clock)
    wall_clock.now_ms = prepared_wall["deadline_ms"] - 240_000 - 5_000
    blocked = generate(wall, role="agent", logical_batch="late-wave", messages=[{"role": "user", "content": "late"}], clock=wall_clock)
    report = generate(wall, role="report", logical_batch="report-main", messages=[{"role": "user", "content": "report"}], clock=wall_clock)
    assert blocked.error_code == "report_wall_reserved" and blocked.sent is False
    assert report.sent and report.channel == "grok_cli"
    assert stand_in["state"].calls == calls_after_third
    wall_clock.now_ms = prepared_wall["deadline_ms"]
    after = generate(wall, role="report", logical_batch="report-late", messages=[{"role": "user", "content": "too late"}], clock=wall_clock)
    assert after.sent is False and after.error_code == "deadline"
    assert len(_records(stand_in)) == 1

    day = _create(client, key="day-source")
    with connect() as conn:
        for index in range(48):
            conn.execute(
                """
                INSERT INTO provider_requests (
                    request_id, run_id, role, logical_batch, attempt_no, status,
                    result_known, created_at, day_key
                ) VALUES (?, ?, 'agent', ?, 1, 'completed', 1, 't', '2026-09-29')
                """,
                (f"day-{index}", day, f"old-{index}"),
            )
    fresh = _create(client, key="day-new")
    denied = generate(fresh, role="agent", logical_batch="today", messages=[{"role": "user", "content": "no"}], clock=clock)
    assert denied.sent is False and denied.error_code == "day_cap"


def test_visible_failures_cancel_restart_and_late_result(stand_in, monkeypatch):
    client = _app().test_client()
    clock = Clock()
    run_id = _create(client, key="failures")
    _write_control(stand_in, modes=["missing"])
    stand_in["state"].modes = ["missing_usage"]
    missing = generate(run_id, role="agent", logical_batch="tokens", messages=[{"role": "user", "content": "t"}], clock=clock)
    assert missing.sent and missing.token_usage_status == "missing"

    stand_in["state"].modes = ["bad", "ok"]
    repaired = generate_structured(
        run_id,
        role="agent",
        logical_batch="json-batch",
        messages=[{"role": "user", "content": "json"}],
        clock=clock,
    )
    assert repaired.json_ok is True
    assert repaired.attempt_no == 2
    third = generate(run_id, role="agent", logical_batch="json-batch", messages=[{"role": "user", "content": "nope"}], clock=clock)
    assert third.error_code == "batch_attempt_limit"

    _write_control(stand_in, modes=["exit"])
    exited = generate(run_id, role="persona", logical_batch="exit", messages=[{"role": "user", "content": "exit"}], clock=clock)
    assert exited.exit_code == 2 and exited.error_code == "cli_exit" and exited.channel == "grok_cli"

    _write_control(stand_in, modes=["queue"])
    queued = generate(run_id, role="persona", logical_batch="queue", messages=[{"role": "user", "content": "queue"}], clock=clock)
    assert queued.queue_status == "queued" and queued.sent

    _write_control(stand_in, modes=["queue_timeout"])
    queued_out = generate(
        run_id,
        role="persona",
        logical_batch="queue-timeout",
        messages=[{"role": "user", "content": "wait"}],
        clock=clock,
        timeout_seconds=0.3,
    )
    assert queued_out.status == "uncertain"
    assert queued_out.queue_status == "queued"
    assert queued_out.error_code == "timeout"

    stand_in["state"].modes = ["timeout"]
    timed = generate(
        run_id,
        role="agent",
        logical_batch="http-timeout",
        messages=[{"role": "user", "content": "slow"}],
        clock=clock,
        timeout_seconds=0.3,
    )
    assert timed.status == "uncertain" and timed.error_code == "timeout"
    assert apply_late_result(timed.request_id, "late success should not apply") is False
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM provider_requests WHERE request_id = ?",
            (timed.request_id,),
        ).fetchone()
    assert row["status"] == "uncertain"

    cancel_id = _create(client, key="cancel-before")
    client.post(f"/api/tweet/runs/{cancel_id}/cancel")
    cancelled = generate(cancel_id, role="agent", logical_batch="cancelled", messages=[{"role": "user", "content": "no"}], clock=clock)
    assert cancelled.sent is False and cancelled.error_code == "cancelled"

    live = _create(client, key="cancel-live")
    _write_control(stand_in, modes=["timeout"])
    box = {}

    def run_live():
        box["result"] = generate(
            live,
            role="persona",
            logical_batch="live",
            messages=[{"role": "user", "content": "cancel me"}],
            clock=clock,
            timeout_seconds=5,
        )

    thread = threading.Thread(target=run_live)
    thread.start()
    time.sleep(0.15)
    client.post(f"/api/tweet/runs/{live}/cancel")
    thread.join(timeout=10)
    assert box["result"].sent is False or box["result"].status in {"uncertain", "failed", "cancelled"}
    assert box["result"].error_code in {"cancelled", "timeout", "not_sent"}

    stale = _create(client, key="stale")
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = 'running', worker_lease_owner = 'dead', worker_lease_expires_ms = 1
            WHERE run_id = ?
            """,
            (stale,),
        )
        conn.execute(
            """
            INSERT INTO provider_requests (
                request_id, run_id, role, logical_batch, attempt_no, channel, status,
                result_known, created_at, day_key
            ) VALUES ('stale-req', ?, 'agent', 'stale-batch', 1, 'ollama', 'in_flight', 0, 't', '2026-09-29')
            """,
            (stale,),
        )
    deadline_before = prepare_run(stale, clock=clock)["deadline_ms"]
    assert recover_stale_leases(now_ms=1_000) >= 1
    with connect() as conn:
        request = conn.execute("SELECT status FROM provider_requests WHERE request_id = 'stale-req'").fetchone()
        copies = conn.execute(
            "SELECT COUNT(*) AS n FROM provider_requests WHERE run_id = ? AND logical_batch = 'stale-batch'",
            (stale,),
        ).fetchone()["n"]
    assert request["status"] == "uncertain"
    assert copies == 1
    deadline_after = prepare_run(stale, clock=Clock(clock.now_ms + 60_000))["deadline_ms"]
    assert deadline_after == deadline_before

    extra = _create(client, key="bounds")
    stand_in["state"].modes = ["big"]
    limited = generate(
        extra,
        role="agent",
        logical_batch="bytes",
        messages=[{"role": "user", "content": "big"}],
        clock=clock,
        max_output_bytes=64,
    )
    assert limited.truncated is True
    _write_control(stand_in, modes=["internal"])
    internal = generate(
        extra,
        role="persona",
        logical_batch="internal",
        messages=[{"role": "user", "content": "extra"}],
        clock=clock,
    )
    assert internal.internal_attempts == 3
    assert internal.internal_accounting == "reported"
    snapshot = usage(extra, clock=clock)
    assert snapshot["counted"] <= 10
    assert snapshot["ledger_calls"] <= 10
    assert snapshot["uncertain"] >= 2
    assert snapshot["dollar_columns"] == []


def test_logs_child_process_and_static_search_have_no_bypass(stand_in, monkeypatch):
    logger, handler, stream = _capture_logs()
    try:
        run_id = _create(_app().test_client(), key="logs")
        generate(
            run_id,
            role="persona",
            logical_batch="persona",
            messages=[{"role": "user", "content": f"{DRAFT} {EMAIL} {SECRET}"}],
            clock=Clock(),
        )
        logger.info("probe %s %s", EMAIL, SECRET)
        text = "\n".join(stream)
        assert DRAFT not in text
        assert EMAIL not in text
        assert SECRET not in text
        assert "sk-" not in text
        record = _records(stand_in)[0]
        assert record["sensitive_keys"] == []
        assert record["email_in_values"] is False
        assert record["secret_in_values"] is False
        with connect() as conn:
            diagnostics = conn.execute(
                "SELECT diagnostics_json FROM provider_requests WHERE logical_batch = 'persona'"
            ).fetchone()["diagnostics_json"]
        assert DRAFT not in (diagnostics or "")
        assert EMAIL not in (diagnostics or "")
    finally:
        logger.removeHandler(handler)

    blocked = []
    real_create = socket.create_connection

    def tracing_create(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in {"127.0.0.1", "localhost"}:
            blocked.append(host)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", tracing_create)
    client = LLMClient(api_key=SECRET, base_url="https://api.openai.com/v1")
    with pytest.raises(DirectModelCallBlocked):
        client.chat(messages=[{"role": "user", "content": DRAFT}])
    from app.services.oasis_profile_generator import OasisProfileGenerator
    from app.services.simulation_config_generator import SimulationConfigGenerator

    with pytest.raises(DirectModelCallBlocked):
        OasisProfileGenerator()._generate_profile_with_llm("n", "person", "s", {}, "c")
    with pytest.raises(DirectModelCallBlocked):
        SimulationConfigGenerator()._call_llm_with_retry("prompt", "system")
    assert blocked == []

    offenders = []
    patterns = ("OpenAI(", "ModelFactory", "api.openai.com", "api.x.ai", '["OPENAI_API_KEY"] =', "['OPENAI_API_KEY'] =")
    for root in (APP_ROOT, SCRIPTS):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                if pattern in text:
                    offenders.append(f"{path}:{pattern}")
    assert offenders == []

    completed = subprocess.run(
        [sys.executable, "-c", """
import os, sys
sys.path.insert(0, "scripts")
sys.path.insert(0, ".")
os.environ["OPENAI_API_KEY"] = "sk-should-not-be-required"
os.environ["LLM_API_KEY"] = "sk-should-not-be-required"
os.environ["LLM_BOOST_API_KEY"] = "sk-boost"
import run_parallel_simulation as parallel
import run_twitter_simulation as twitter
import run_reddit_simulation as reddit
from app.providers.blocked import DirectModelCallBlocked
for call in (
    lambda: parallel.create_model({}, True),
    lambda: twitter.TwitterSimulationRunner.__new__(twitter.TwitterSimulationRunner)._create_model(),
    lambda: reddit.RedditSimulationRunner.__new__(reddit.RedditSimulationRunner)._create_model(),
):
    try:
        call()
    except DirectModelCallBlocked as exc:
        text = str(exc)
        assert "sk-" not in text
        assert "OPENAI" not in text or "不会使用" in text
        continue
    raise SystemExit("model call was not blocked")
print("blocked")
"""],
        cwd=BACKEND,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "blocked" in completed.stdout


def test_provider_defaults_mark_real_channels_unverified():
    document = json.loads((APP_ROOT / "providers" / "providers.json").read_text(encoding="utf-8"))
    assert document["limits"]["run_max_requests"] == 10
    assert document["limits"]["run_max_wall_seconds"] == 600
    assert document["limits"]["request_timeout_seconds"] == 120
    assert document["limits"]["day_max_requests"] == 50
    assert document["limits"]["llm_concurrency"] == 1
    assert document["silent_fallback"] is False
    assert set(document["unverified"].values()) == {"待验"}
    assert document["routes"]["mixed"]["agent"] == "ollama"
    assert document["routes"]["subscription-only"]["agent"] == "subscription_cli"
