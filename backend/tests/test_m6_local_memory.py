"""Local memory: SQLite store, failed chunks, Chinese recall, and a no-Zep walk.

JSON records may use the old graph keys. That is not Zep search quality.
Model calls still go through the two channels, so the walk is not offline.
"""

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import zep_cloud

from app import create_app
from app.config import Config
from app.memory.extract import extract_pending
from app.memory.pipeline import run_local_walk
from app.memory.segment import SEGMENT_VERSION, segment
from app.memory.store import SCHEMA_VERSION, MemoryStore, normalize_name, normalize_type
from app.models.project import Project, ProjectManager, ProjectStatus
from app.tweet.db import connect, init_db
from app.tweet.service import create_run, list_runs
from app.tweet.worker import advance_once


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


class Clock:
    def __init__(self, now_ms: int = 1_700_000_000_000):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def day_key(self) -> str:
        return "2026-09-29"


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


def _parse_task(prompt: str) -> dict:
    start = prompt.find("{")
    if start < 0:
        return {}
    try:
        loaded, _end = json.JSONDecoder().raw_decode(prompt[start:])
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _span(text: str, snippet: str) -> dict:
    start = text.find(snippet)
    assert start >= 0, snippet
    return {"start": start, "end": start + len(snippet), "text": snippet}


def _extract_json(text: str) -> str:
    nodes = []
    if "李明" in text:
        nodes.append({
            "name": "李明",
            "type": "人物",
            "summary": "记者",
            "attributes": {"city": "北京"},
            "span": _span(text, "李明"),
            "valid_at": None,
            "invalid_at": None,
        })
        nodes.append({
            "name": "李明",
            "type": "人物",
            "summary": "另一位同名的人",
            "attributes": {"city": "深圳", "same_as": "不要合并"},
            "span": _span(text, "李明"),
            "valid_at": None,
            "invalid_at": None,
        })
    if "华为" in text:
        nodes.append({
            "name": "华为",
            "type": "组织",
            "summary": "公司",
            "attributes": {},
            "span": _span(text, "华为"),
            "valid_at": None,
            "invalid_at": None,
        })
    edges = []
    if len(nodes) >= 2:
        edges.append({
            "name": "相关",
            "fact": "记者李明到场",
            "source_name": "李明",
            "source_type": "人物",
            "target_name": "华为",
            "target_type": "组织",
            "span": _span(text, "李明"),
            "valid_at": None,
            "invalid_at": None,
        })
    return json.dumps(
        {"nodes": nodes, "edges": edges, "same_as": ["李明"], "merge": True},
        ensure_ascii=False,
    )


def _render(prompt: str, mode: str) -> str:
    data = _parse_task(prompt)
    if mode == "bad":
        return "不是 JSON"
    if mode == "bad-span":
        return json.dumps(
            {"nodes": [{"name": "李明", "type": "人物", "span": {"start": 0, "end": 1, "text": "错"}}]},
            ensure_ascii=False,
        )
    task = data.get("task")
    if task == "memory_ontology":
        return json.dumps(
            {"entity_types": [{"name": "人物"}, {"name": "组织"}], "edge_types": [{"name": "相关"}]},
            ensure_ascii=False,
        )
    if task == "memory_extract":
        return _extract_json(str(data.get("chunk_text") or ""))
    if task == "memory_persona":
        return json.dumps({"personas": [{"name": "李明", "bio": "记者"}]}, ensure_ascii=False)
    if task == "memory_oasis":
        return json.dumps({"profiles": [{"name": "李明", "user_profile": "记者"}]}, ensure_ascii=False)
    if task == "memory_report":
        return json.dumps(
            {"title": "模拟备忘", "summary": "未校准结果", "evidence_count": len(data.get("evidence") or [])},
            ensure_ascii=False,
        )
    return json.dumps({"task": task, "ok": False}, ensure_ascii=False)


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
            body = _render(prompt, brain.next_mode()).encode("utf-8")
            if not self.path.startswith("/synthesize"):
                body = json.dumps(
                    {"choices": [{"message": {"content": body.decode("utf-8")}}], "usage": {"total_tokens": 3}},
                    ensure_ascii=False,
                ).encode("utf-8")
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


def _ready(tmp_path, monkeypatch) -> dict:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    script.chmod(0o755)
    grok = tmp_path / "grok"
    grok.write_text(FAKE_CLI, encoding="utf-8")
    grok.chmod(0o755)
    login = tmp_path / "login"
    login.write_text("logged-in\n", encoding="utf-8")
    brain = Brain()
    server = _start_brain(brain)
    host, port = server.server_address
    monkeypatch.setenv("TWEET_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("MEMORY_STATE_DB", str(tmp_path / "memory.sqlite"))
    monkeypatch.setenv("TWEET_WORKER_MODE", "manual")
    monkeypatch.setenv("SUBSCRIPTION_CLI", "grok")
    monkeypatch.setenv("GROK_CLI_PATH", str(grok))
    monkeypatch.setenv("SUBSCRIPTION_CLI_LOGIN_PATH", str(login))
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://{host}:{port}/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.8:27b-mxfp8")
    monkeypatch.setenv("FAKE_BRAIN", f"http://{host}:{port}/synthesize")
    monkeypatch.setenv("MEMORY_BACKEND", "local")
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    return {"server": server, "brain": brain}


def _forbid_cloud(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("本地走查不能改用云端图谱或被阻断的直连模型。")

    monkeypatch.setattr("app.services.ontology_generator.OntologyGenerator.generate", boom)
    monkeypatch.setattr(
        "app.services.oasis_profile_generator.OasisProfileGenerator.generate_profiles_from_entities",
        boom,
    )
    monkeypatch.setattr("app.services.report_agent.ReportAgent.generate_report", boom)
    monkeypatch.setattr("app.services.zep_entity_reader.ZepEntityReader.__init__", boom)


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.sqlite")


def _hits(store: MemoryStore, graph_id: str, query: str, snippet: str) -> list[dict]:
    found = []
    for hit in store.search(graph_id, query):
        if snippet in hit["text"] or any(item["token"] == snippet for item in hit["offsets"]):
            found.append(hit)
    return found


def test_store_is_idempotent_and_keeps_conflicts(tmp_path):
    store = _store(tmp_path)
    graph_id = store.create_graph(name="本地图谱", project_id="proj_local")
    other = store.create_graph(name="另一张图")
    text = ("甲" * 20) + "李明到场"
    first = store.ingest(graph_id, text, source_name="doc", chunk_size=20, chunk_overlap=0)
    second = store.ingest(graph_id, text, source_name="doc", chunk_size=20, chunk_overlap=0)
    assert first == second
    assert len(store.chunks(graph_id)) == 2

    chunk = next(item for item in store.chunks(graph_id) if item["text"].startswith("李明"))
    assert chunk["text"] == "李明到场"
    assert chunk["start_offset"] == 20
    payload = {
        "nodes": [
            {
                "name": "李明",
                "type": "人 物",
                "summary": "记者",
                "attributes": {"city": "北京", "duplicate_of": "丢掉"},
                "span": {"start": 0, "end": 2, "text": "李明"},
                "valid_at": None,
                "invalid_at": "不是时间",
            },
            {
                "name": "李明",
                "type": "人物",
                "summary": "另一位",
                "attributes": {"city": "深圳", "merge": "不要合并"},
                "span": {"start": 0, "end": 2, "text": "李明"},
                "valid_at": "2024-05-01T00:00:00+00:00",
                "invalid_at": None,
            },
            {
                "name": "華為",
                "type": "组织",
                "summary": "传统字形",
                "attributes": {},
                "span": None,
            },
        ],
        "edges": [
            {
                "name": "相关",
                "fact": "李明到场",
                "source_name": "李明",
                "source_type": "人物",
                "target_name": "華為",
                "target_type": "组织",
                "span": {"start": 0, "end": 2, "text": "李明"},
            },
            {
                "name": "相关",
                "fact": "名字还在，端点未定",
                "source_name": "李明",
                "target_name": "不存在",
                "span": {"start": 2, "end": 4, "text": "到场"},
            },
        ],
        "same_as": ["李明"],
        "merge": True,
    }
    written = store.commit_records(graph_id, chunk, payload)
    again = store.commit_records(graph_id, chunk, payload)
    assert written["nodes"] == again["nodes"]
    nodes = store.read_nodes(graph_id)
    assert len(nodes) == 3
    by_city = {item["attributes"].get("city"): item for item in nodes if item["normalized_type"] == "人物"}
    assert set(by_city) == {"北京", "深圳"}
    assert by_city["北京"]["node_id"] != by_city["深圳"]["node_id"]
    assert "duplicate_of" not in by_city["北京"]["attributes"]
    assert "merge" not in by_city["深圳"]["attributes"]
    assert by_city["北京"]["normalized_name"] == "李明"
    assert normalize_name("Li  Ming") == normalize_name("li ming") == "li ming"
    assert normalize_type("人 物") == "人物"
    assert normalize_name("華為") != normalize_name("华为")
    assert by_city["北京"]["source_span"]["start"] == 20
    assert by_city["北京"]["source_span"]["end"] == 22
    assert by_city["北京"]["source_span"]["text"] == "李明"
    assert by_city["北京"]["valid_at"] is None
    assert by_city["北京"]["invalid_at"] is None
    assert by_city["深圳"]["valid_at"] == "2024-05-01T00:00:00+00:00"
    assert by_city["深圳"]["invalid_at"] is None
    assert {item["schema_version"] for item in nodes} == {SCHEMA_VERSION}
    assert all(item["created_at"] for item in nodes)
    traditional = next(item for item in nodes if item["name"] == "華為")
    assert traditional["normalized_name"] == "華為".casefold()
    assert traditional["node_id"] != by_city["北京"]["node_id"]

    edges = store.read_edges(graph_id)
    linked = [item for item in edges if item["fact"] == "李明到场"]
    assert len(linked) == 2
    assert {item["source_node_id"] for item in linked} == {by_city["北京"]["node_id"], by_city["深圳"]["node_id"]}
    dangling = next(item for item in edges if item["target_name"] == "不存在")
    assert dangling["target_node_id"] is None
    assert dangling["source_span"]["text"] == "到场"
    assert dangling["valid_at"] is None
    assert dangling["schema_version"] == SCHEMA_VERSION

    episode = store.append_episode(graph_id, kind="behavior", text="一条行为记录", valid_at=None, invalid_at=None)
    assert store.append_episode(graph_id, kind="behavior", text="一条行为记录") == episode
    assert len(store.read_episodes(graph_id, "behavior")) == 1
    created = store.read_episodes(graph_id, "behavior")[0]["created_at"]
    assert store.append_episode(graph_id, kind="behavior", text="另一条行为记录") != episode
    saved = store.read_episodes(graph_id, "behavior")
    assert len(saved) == 2
    first_event = next(item for item in saved if item["text"] == "一条行为记录")
    assert first_event["created_at"] == created
    assert first_event["schema_version"] == SCHEMA_VERSION
    assert first_event["valid_at"] is None
    assert first_event["invalid_at"] is None

    store.ingest(other, "只属于另一张图", source_name="other")
    exported = store.export_graph(graph_id)
    assert exported["schema_version"] == SCHEMA_VERSION
    assert exported["segment_version"] == SEGMENT_VERSION
    assert exported["search_equivalence"] is False
    assert exported["documents"][0]["text"] == text
    assert any(item["text"] == "李明到场" for item in exported["chunks"])
    assert store.delete_graph(graph_id) is True
    assert store.has_graph(graph_id) is False
    assert store.has_graph(other) is True
    assert store.search(graph_id, "李明") == []
    assert store.delete_graph("zep-graph-keep") is False
    with pytest.raises(KeyError):
        store.export_graph(graph_id)
    assert store.export_graph(other)["documents"][0]["text"] == "只属于另一张图"
    assert type(nodes[0]) is dict
    assert "zep" not in type(nodes[0]).__module__


def test_failed_chunk_keeps_text_and_can_be_rerun(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    try:
        store = MemoryStore()
        graph_id = store.create_graph(name="修复")
        original = "记者李明到场。华为在深圳发布手机。"
        store.ingest(graph_id, original, source_name="repair")
        store.set_ontology(graph_id, {"entity_types": [{"name": "人物"}]})
        from app.memory.gateway_run import open_gateway_run

        run_id = open_gateway_run(label=graph_id)
        env["brain"].script = ["bad", "bad-span"]
        summary = extract_pending(store, graph_id, run_id, clock=Clock())
        chunk = store.chunks(graph_id)[0]
        assert summary["partial"] == 1
        assert summary["complete"] == 0
        assert chunk["status"] == "partial"
        assert chunk["text"] == original
        assert chunk["error"]
        jobs = store.jobs(chunk["chunk_id"])
        assert [item["raw_text"] for item in jobs] == [
            "不是 JSON",
            json.dumps(
                {"nodes": [{"name": "李明", "type": "人物", "span": {"start": 0, "end": 1, "text": "错"}}]},
                ensure_ascii=False,
            ),
        ]
        assert store.read_nodes(graph_id) == []
        exported = store.export_graph(graph_id)
        assert exported["chunks"][0]["text"] == original
        assert exported["extraction_jobs"]

        with connect() as conn:
            batches = [
                row["logical_batch"]
                for row in conn.execute(
                    """
                    SELECT logical_batch FROM provider_requests
                    WHERE run_id = ? AND role = 'extractor' AND status = 'completed'
                    ORDER BY created_at
                    """,
                    (run_id,),
                )
            ]
        assert len(batches) == 2
        assert batches[0] == batches[1]

        summary = extract_pending(store, graph_id, run_id, clock=Clock())
        chunk = store.chunks(graph_id)[0]
        assert summary["complete"] == 1
        assert chunk["status"] == "complete"
        assert chunk["text"] == original
        assert chunk["error"] is None
        nodes = store.read_nodes(graph_id)
        assert len([item for item in nodes if item["name"] == "李明"]) == 2
        assert {item["attributes"].get("city") for item in nodes if item["name"] == "李明"} == {"北京", "深圳"}
        assert all("same_as" not in item["attributes"] for item in nodes)
        kept = store.jobs(chunk["chunk_id"])
        assert any(item["raw_text"] == "不是 JSON" for item in kept)
        assert store.export_graph(graph_id)["documents"][0]["text"] == original
    finally:
        env["server"].shutdown()


def test_cap_and_send_failure_do_not_drop_the_chunk(tmp_path):
    store = _store(tmp_path)
    graph_id = store.create_graph(name="上限")
    original = "这段原文必须留下。"
    store.ingest(graph_id, original, source_name="cap")
    chunk_id = store.chunks(graph_id)[0]["chunk_id"]

    def stopped(*_args, **_kwargs):
        return type("Result", (), {
            "sent": False,
            "text": None,
            "error_message": "本机今日共用调用次数已达到上限",
            "error_code": "day_cap",
            "channel": "ollama",
            "model_id": "qwen3.8:27b-mxfp8",
        })()

    summary = extract_pending(store, graph_id, "run-cap", generate_fn=stopped)
    chunk = store.chunks(graph_id)[0]
    assert summary["stopped"] == "cap"
    assert chunk["status"] == "pending"
    assert chunk["text"] == original
    assert chunk["error"] is None
    assert store.jobs(chunk_id) == []

    calls = []

    def failed(*_args, **kwargs):
        calls.append(kwargs["kind"])
        return type("Result", (), {
            "sent": False,
            "text": "通道错误但原文还在",
            "error_message": "Ollama 返回错误",
            "error_code": "http_error",
            "channel": "ollama",
            "model_id": "qwen3.8:27b-mxfp8",
        })()

    summary = extract_pending(store, graph_id, "run-fail", generate_fn=failed)
    chunk = store.chunks(graph_id)[0]
    assert calls == ["generation"]
    assert summary["partial"] == 1
    assert chunk["status"] == "partial"
    assert chunk["text"] == original
    assert "Ollama" in chunk["error"]
    assert store.jobs(chunk_id)[0]["raw_text"] == "通道错误但原文还在"
    assert len(store.export_graph(graph_id)["chunks"]) == 1


def test_chinese_recall_keeps_original_text_and_offsets(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    store = _store(tmp_path)
    samples = {
        "simplified": "华为在深圳发布手机",
        "traditional": "華為在臺灣開會",
        "mixed": "OpenAI与北京大学合作",
        "entity": "嘉宾赵强出席",
        "name": "记者李明到场",
    }
    graphs = {}
    for name, text in samples.items():
        graph_id = store.create_graph(name=name)
        graphs[name] = graph_id
        store.ingest(graph_id, text, source_name=name)

    def assert_hit(graph_id: str, query: str, snippet: str):
        hits = _hits(store, graph_id, query, snippet)
        assert hits, (query, snippet, store.search(graph_id, query))
        hit = hits[0]
        assert snippet in hit["text"]
        assert hit["segment_version"] == SEGMENT_VERSION
        matched = [
            item for item in hit["offsets"]
            if hit["text"][item["start"]:item["end"]] == snippet
        ]
        assert matched, hit
        assert matched[0]["token"] == snippet

    assert_hit(graphs["simplified"], "华为", "华为")
    assert_hit(graphs["simplified"], "手机", "手机")
    assert_hit(graphs["traditional"], "华为", "華為")
    assert_hit(graphs["traditional"], "台湾", "臺灣")
    assert_hit(graphs["traditional"], "开会", "開會")
    assert_hit(graphs["mixed"], "OpenAI", "OpenAI")
    assert_hit(graphs["mixed"], "北京大学", "北京大学")
    assert_hit(graphs["entity"], "赵强", "赵强")
    assert_hit(graphs["name"], "李明", "李明")

    with store.connect() as conn:
        stored = conn.execute(
            "SELECT text FROM documents WHERE graph_id = ?",
            (graphs["traditional"],),
        ).fetchone()["text"]
        folded = conn.execute(
            """
            SELECT token_folded, original_token FROM search_segments
            WHERE graph_id = ? AND original_token = '華為'
            """,
            (graphs["traditional"],),
        ).fetchone()
    assert stored == samples["traditional"]
    assert "华为" not in stored
    assert folded["token_folded"] == "华为"
    assert folded["original_token"] == "華為"

    plain = sqlite3.connect(":memory:")
    plain.execute("CREATE VIRTUAL TABLE plain USING fts5(body, tokenize = 'unicode61 remove_diacritics 0')")
    plain.execute("INSERT INTO plain(body) VALUES (?)", (samples["simplified"],))
    plain.execute("INSERT INTO plain(body) VALUES (?)", (samples["entity"],))
    plain.execute("CREATE VIRTUAL TABLE plain_vocab USING fts5vocab(plain, 'row')")
    terms = [row[0] for row in plain.execute("SELECT term FROM plain_vocab")]
    assert "华为" not in terms
    assert "赵强" not in terms
    assert any("华为" in term and len(term) > len("华为") for term in terms)
    assert plain.execute("SELECT rowid FROM plain WHERE plain MATCH ?", ('"华为"',)).fetchall() == []
    assert plain.execute("SELECT rowid FROM plain WHERE plain MATCH ?", ('"赵强"',)).fetchall() == []

    view = store.graph_view(graphs["simplified"])
    assert view["search_equivalence"] is False
    assert "检索质量并不等价" in view["search_note"]
    assert "unicode61" in view["search_note"]
    assert "embedding" not in Path(__file__).resolve().parents[1].joinpath("app/memory/store.py").read_text(encoding="utf-8").lower()

    monkeypatch.setenv("MEMORY_STATE_DB", str(store.path))
    monkeypatch.setenv("TWEET_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setattr(Config, "ZEP_API_KEY", None)
    client = create_app().test_client()
    response = client.get(f"/api/graph/data/{graphs['simplified']}")
    assert response.status_code == 200
    assert response.json["data"]["search_equivalence"] is False
    missing = client.get("/api/graph/data/zep-graph-keep")
    assert missing.status_code == 500


def test_new_projects_default_local_without_migrating_cloud(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    old = projects / "proj_old"
    old.mkdir(parents=True)
    payload = {
        "project_id": "proj_old",
        "name": "旧云端项目",
        "status": "graph_completed",
        "created_at": "2020-01-01T00:00:00",
        "updated_at": "2020-01-01T00:00:00",
        "graph_id": "zep-graph-keep",
    }
    (old / "project.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(projects))
    monkeypatch.setenv("MEMORY_BACKEND", "local")
    loaded = ProjectManager.get_project("proj_old")
    assert loaded is not None
    assert loaded.memory_backend == "zep"
    assert loaded.graph_id == "zep-graph-keep"
    assert "local" not in (old / "project.json").read_text(encoding="utf-8")

    blank = Project(
        project_id="proj_blank",
        name="未标注",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2020-01-01T00:00:00",
        updated_at="2020-01-01T00:00:00",
        graph_id="zep-graph-keep",
    )
    assert blank.memory_backend == "zep"
    assert Project.from_dict(payload).memory_backend == "zep"
    assert Project.from_dict({**payload, "memory_backend": "cloud"}).memory_backend == "zep"

    created = ProjectManager.create_project("新本地项目")
    assert created.memory_backend == "local"
    saved = json.loads((projects / created.project_id / "project.json").read_text(encoding="utf-8"))
    assert saved["memory_backend"] == "local"
    assert ProjectManager.get_project("proj_old").graph_id == "zep-graph-keep"

    monkeypatch.setenv("MEMORY_BACKEND", "zep")
    cloud = ProjectManager.create_project("仍走云端")
    assert cloud.memory_backend == "zep"
    assert ProjectManager.get_project("proj_old").memory_backend == "zep"


def test_local_walk_uses_fake_channels_and_no_zep_key(tmp_path, monkeypatch):
    env = _ready(tmp_path, monkeypatch)
    _forbid_cloud(monkeypatch)
    monkeypatch.setattr(Config, "ZEP_API_KEY", None)
    try:
        assert zep_cloud.Zep
        init_db()
        assert advance_once() is False
        store = MemoryStore()
        result = run_local_walk(
            text="记者李明到场。华为在深圳发布手机。",
            requirement="看看本地图谱能不能走完",
            activity="李明转发了这条帖子",
            query="李明",
            store=store,
            clock=Clock(),
        )
        assert result["memory_backend"] == "local"
        assert result["zep_called"] is False
        assert result["offline"] is False
        assert result["extraction"]["complete"] == 1
        assert len([item for item in result["nodes"] if item["name"] == "李明"]) == 2
        assert result["personas"][0]["name"] == "李明"
        assert result["oasis_profiles"][0]["name"] == "李明"
        behavior = store.read_episodes(result["graph_id"], "behavior")
        assert behavior[0]["text"] == "李明转发了这条帖子"
        assert behavior[0]["episode_id"] == result["behavior_episode_id"]
        assert any(item["record_kind"] in {"chunk", "episode", "node"} for item in result["search_hits"])
        assert any("李明" in item["text"] for item in result["search_hits"])
        assert "模拟备忘" in json.dumps(result["report"], ensure_ascii=False)
        assert result["channels"] == {
            "ontology": "ollama",
            "persona": "grok_cli",
            "oasis": "ollama",
            "report": "grok_cli",
        }
        assert env["brain"].ollama_calls >= 3
        assert env["brain"].brain_calls >= 2
        assert "ZEP_API_KEY" not in __import__("os").environ

        with connect() as conn:
            memory = conn.execute(
                "SELECT status, run_kind FROM runs WHERE run_id = ?",
                (result["run_id"],),
            ).fetchone()
            roles = {
                row["role"]: row["channel"]
                for row in conn.execute(
                    """
                    SELECT role, channel FROM provider_requests
                    WHERE run_id = ? AND status = 'completed'
                    """,
                    (result["run_id"],),
                )
            }
        assert memory["status"] == "gateway"
        assert memory["run_kind"] == "memory"
        assert roles["ontology"] == "ollama"
        assert roles["extractor"] == "ollama"
        assert roles["config"] == "ollama"
        assert roles["persona"] == "grok_cli"
        assert roles["report"] == "grok_cli"
        assert advance_once() is False

        tweet, created = create_run(
            {
                "draft_text": "所有人都该用AI写作。",
                "author_context": "作者背景",
                "audience_version": "zh_x_v1",
                "agent_count": 10,
                "round_count": 1,
                "execution_profile": "mixed",
            },
            idempotency_key="tweet-still-listed",
        )
        assert created is True
        listed = list_runs()
        assert [item["run_id"] for item in listed] == [tweet["run_id"]]
        assert all("本地图谱" not in (item.get("draft_preview") or "") for item in listed)
        assert store.path.name == "memory.sqlite"
        assert store.path.name not in {"twitter_simulation.db", "reddit_simulation.db", "state.sqlite"}
        with store.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"graphs", "documents", "chunks", "nodes", "edges", "episodes", "extraction_jobs"} <= tables
    finally:
        env["server"].shutdown()
