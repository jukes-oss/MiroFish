"""Extract one graph through the Ollama extractor role and the call gateway.

A failed chunk keeps its original text, error, and status. It may be repaired
once inside the same logical batch. A failed repair stays partial and can be
retried later as a new batch. Nothing is deleted when parsing fails.
"""

from __future__ import annotations

import json

from ..providers.gateway import generate
from .store import MemoryStore

_STOP = {
    "run_call_cap",
    "day_cap",
    "deadline",
    "report_wall_reserved",
    "batch_attempt_limit",
    "cancelled",
    "concurrency",
    "report_reserve_cap",
}
_IGNORED_KEYS = {"same_as", "merge", "merged_into", "duplicate_of"}


def extract_pending(store: MemoryStore, graph_id: str, run_id: str, *, clock=None, generate_fn=generate) -> dict:
    ontology = store.ontology(graph_id) or {}
    summary = {"complete": 0, "partial": 0, "pending": 0, "stopped": None}
    for chunk in store.chunks(graph_id, ("pending", "partial")):
        outcome = _one_chunk(store, graph_id, chunk, ontology, run_id, clock=clock, generate_fn=generate_fn)
        if outcome == "stopped":
            summary["stopped"] = "cap"
            break
        summary[outcome] = summary.get(outcome, 0) + 1
    remaining = store.chunks(graph_id, ("pending", "partial"))
    summary["pending"] = sum(1 for item in remaining if item["status"] == "pending")
    summary["partial"] = sum(1 for item in remaining if item["status"] == "partial")
    summary["complete"] = sum(1 for item in store.chunks(graph_id, ("complete",)) )
    return summary


def _one_chunk(store, graph_id, chunk, ontology, run_id, *, clock, generate_fn) -> str:
    attempt = int(chunk["repair_count"]) + 1
    batch = f"extract:{chunk['chunk_id']}:{attempt}"
    first = _ask(generate_fn, run_id, batch, _payload(chunk, ontology, "generation", None), "generation", clock)
    if first.get("stopped"):
        return "stopped"
    store.add_job(
        graph_id,
        chunk["chunk_id"],
        attempt_no=attempt,
        status="received" if first["sent"] else "not_sent",
        error=first.get("error"),
        raw_text=first.get("text"),
    )
    if not first["sent"]:
        if first.get("error_code") in _STOP:
            return "stopped"
        store.mark_chunk(chunk["chunk_id"], status="partial", error=first.get("error") or "抽取没有发出。")
        return "partial"
    parsed, problems = _interpret(first["text"], chunk["text"])
    if not problems:
        store.commit_records(graph_id, chunk, parsed)
        store.mark_chunk(chunk["chunk_id"], status="complete", error=None, repair_count=int(chunk["repair_count"]))
        return "complete"
    repair = _ask(
        generate_fn,
        run_id,
        batch,
        _payload(chunk, ontology, "repair", problems),
        "repair",
        clock,
    )
    store.add_job(
        graph_id,
        chunk["chunk_id"],
        attempt_no=attempt,
        status="repair_received" if repair["sent"] else "repair_not_sent",
        error=repair.get("error") or "；".join(problems),
        raw_text=repair.get("text"),
    )
    if repair.get("stopped") or (not repair["sent"] and repair.get("error_code") in _STOP):
        store.mark_chunk(
            chunk["chunk_id"],
            status="partial",
            error="修复没有发出：" + (repair.get("error") or "已到调用或时间上限"),
            repair_count=int(chunk["repair_count"]) + 1,
        )
        return "partial" if not repair.get("stopped") else "stopped"
    if not repair["sent"]:
        store.mark_chunk(
            chunk["chunk_id"],
            status="partial",
            error=repair.get("error") or "修复没有发出。",
            repair_count=int(chunk["repair_count"]) + 1,
        )
        return "partial"
    repaired, repair_problems = _interpret(repair["text"], chunk["text"])
    if repair_problems:
        store.mark_chunk(
            chunk["chunk_id"],
            status="partial",
            error="修复后仍不可用：" + "；".join(repair_problems),
            repair_count=int(chunk["repair_count"]) + 1,
        )
        return "partial"
    store.commit_records(graph_id, chunk, repaired)
    store.mark_chunk(chunk["chunk_id"], status="complete", error=None, repair_count=int(chunk["repair_count"]) + 1)
    return "complete"


def _ask(generate_fn, run_id, batch, payload, kind, clock) -> dict:
    result = generate_fn(
        run_id,
        role="extractor",
        logical_batch=batch,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        kind=kind,
        clock=clock,
    )
    error_code = getattr(result, "error_code", None)
    return {
        "sent": bool(getattr(result, "sent", False)),
        "text": getattr(result, "text", None),
        "error": getattr(result, "error_message", None),
        "error_code": error_code,
        "channel": getattr(result, "channel", None),
        "model_id": getattr(result, "model_id", None),
        "stopped": error_code in _STOP and not getattr(result, "sent", False),
    }


def _payload(chunk, ontology, attempt, problems) -> dict:
    body = {
        "task": "memory_extract",
        "attempt": attempt,
        "chunk_text": chunk["text"],
        "ontology": ontology,
        "instruction": (
            "只根据 chunk_text 抽取。span 的 start/end 是这段文本里的码点，"
            "text 必须等于切片。不要合并同名实体，不要输出 same_as 或 merge。"
            "valid_at 和 invalid_at 未知时用 null。"
        ),
    }
    if problems:
        body["errors"] = problems
    return body


def _interpret(raw: str | None, chunk_text: str) -> tuple[dict, list[str]]:
    loaded = _parse_object(raw)
    if loaded is None:
        return {}, ["返回不是一个 JSON 对象"]
    for key in _IGNORED_KEYS:
        loaded.pop(key, None)
    problems = []
    nodes = loaded.get("nodes")
    edges = loaded.get("edges")
    if nodes is None:
        nodes = []
    if edges is None:
        edges = []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return loaded, ["nodes 和 edges 必须是数组"]
    for item in nodes:
        if not isinstance(item, dict):
            problems.append("节点不是对象")
            continue
        if not str(item.get("name") or "").strip() or not str(item.get("type") or item.get("entity_type") or "").strip():
            problems.append("节点缺少名称或类型")
        span_error = _span_error(item.get("span"), chunk_text)
        if span_error:
            problems.append(span_error)
    for item in edges:
        if not isinstance(item, dict):
            problems.append("边不是对象")
            continue
        if not str(item.get("fact") or "").strip():
            problems.append("边缺少 fact")
        span_error = _span_error(item.get("span"), chunk_text)
        if span_error:
            problems.append(span_error)
    return loaded, problems


def _span_error(value, chunk_text: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "span 不是对象"
    start = value.get("start")
    end = value.get("end")
    snippet = value.get("text")
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(snippet, str):
        return "span 缺少码点或原文"
    if not (0 <= start < end <= len(chunk_text)) or chunk_text[start:end] != snippet:
        return "span 和原文对不上"
    return None


def _parse_object(raw: str | None) -> dict | None:
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        loaded, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
