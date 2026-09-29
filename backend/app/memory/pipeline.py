"""Local ontology, extraction, personas, OASIS profiles, behavior, and report.

Model calls use the subscription CLI and Ollama through the gateway. This path
is not offline, and it does not call Zep.
"""

from __future__ import annotations

import json

from ..providers.gateway import generate
from .extract import extract_pending
from .gateway_run import open_gateway_run
from .store import MemoryStore


def run_local_walk(
    *,
    text: str,
    requirement: str,
    activity: str,
    query: str,
    store: MemoryStore | None = None,
    clock=None,
) -> dict:
    memory = store or MemoryStore()
    graph_id = memory.create_graph(name="本地图谱")
    run_id = open_gateway_run(label=graph_id)
    channels = {}

    ontology_result = _call(
        run_id,
        role="ontology",
        batch="memory-ontology",
        payload={
            "task": "memory_ontology",
            "requirement": requirement,
            "text": text,
        },
        clock=clock,
    )
    channels["ontology"] = ontology_result.channel
    ontology = _object(ontology_result.text)
    if not isinstance(ontology.get("entity_types"), list):
        raise RuntimeError("本体没有返回实体类型，没有改用云端图谱。")
    memory.set_ontology(graph_id, ontology)

    memory.ingest(graph_id, text, source_name="walk")
    extraction = extract_pending(memory, graph_id, run_id, clock=clock)
    nodes = memory.read_nodes(graph_id)

    persona_result = _call(
        run_id,
        role="persona",
        batch="memory-persona",
        payload={"task": "memory_persona", "nodes": [{"name": item["name"], "type": item["entity_type"]} for item in nodes]},
        clock=clock,
    )
    channels["persona"] = persona_result.channel
    personas = _object(persona_result.text).get("personas") or []
    if not isinstance(personas, list) or not personas:
        raise RuntimeError("人设没有生成，没有改用云端图谱。")
    for persona in personas:
        memory.append_episode(graph_id, kind="persona", text=json.dumps(persona, ensure_ascii=False))

    oasis_result = _call(
        run_id,
        role="config",
        batch="memory-oasis",
        payload={"task": "memory_oasis", "personas": personas},
        clock=clock,
    )
    channels["oasis"] = oasis_result.channel
    profiles = _object(oasis_result.text).get("profiles") or []
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError("OASIS 档案没有生成，没有改用云端图谱。")
    for profile in profiles:
        memory.append_episode(graph_id, kind="oasis_profile", text=json.dumps(profile, ensure_ascii=False))

    behavior_id = memory.append_episode(graph_id, kind="behavior", text=activity)
    hits = memory.search(graph_id, query)
    report_result = _call(
        run_id,
        role="report",
        batch="memory-report",
        payload={
            "task": "memory_report",
            "requirement": requirement,
            "evidence": [{"text": item["text"], "kind": item["record_kind"]} for item in hits[:8]],
            "note": "未校准结果是模拟备忘。这一步仍要调用模型，不是离线完成。",
        },
        clock=clock,
    )
    channels["report"] = report_result.channel
    report = _object(report_result.text)
    return {
        "graph_id": graph_id,
        "run_id": run_id,
        "memory_backend": "local",
        "ontology": ontology,
        "extraction": extraction,
        "nodes": nodes,
        "personas": personas,
        "oasis_profiles": profiles,
        "behavior_episode_id": behavior_id,
        "search_hits": hits,
        "report": report,
        "channels": channels,
        "zep_called": False,
        "offline": False,
    }


def execute_local_build(
    *,
    graph_name: str,
    project_id: str,
    text: str,
    ontology: dict,
    chunk_size: int,
    chunk_overlap: int,
    clock=None,
) -> dict:
    """Ingest and extract one new local graph. Does not read or delete Zep."""

    memory = MemoryStore()
    graph_id = memory.create_graph(name=graph_name, project_id=project_id)
    memory.set_ontology(graph_id, ontology or {})
    memory.ingest(
        graph_id,
        text,
        source_name=project_id,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    run_id = open_gateway_run(label=graph_id)
    summary = extract_pending(memory, graph_id, run_id, clock=clock)
    view = memory.graph_view(graph_id)
    view["extraction"] = summary
    view["gateway_run_id"] = run_id
    return view


def _call(run_id: str, *, role: str, batch: str, payload: dict, clock):
    result = generate(
        run_id,
        role=role,
        logical_batch=batch,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        kind="generation",
        clock=clock,
    )
    if not result.sent:
        raise RuntimeError(result.error_message or "模型调用没有发出，没有改用云端图谱。")
    return result


def _object(raw: str | None) -> dict:
    if not raw:
        return {}
    start = raw.find("{")
    if start < 0:
        return {}
    try:
        loaded, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
