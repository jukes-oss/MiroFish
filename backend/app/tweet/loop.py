"""One tweet simulation loop: slots, one persona call, one batch per wave.

The loop uses the existing gateway. It does not call a moderator model and
it does not send one request per account. The worker enters this loop from
``advance_once``. The report document is finalized by ``report.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timezone

from contracts.check_contracts import exact_id_coverage, load_schemas, schema_errors

from ..providers.gateway import generate
from ..providers.limits import REPORT_RESERVE_CALLS, REQUEST_TIMEOUT_SECONDS, RUN_MAX_REQUESTS
from .audience import build_slots, split_waves
from .db import connect
from .model_json import parse_model_object
from .sampling import draw_candidates, model_items

logger = logging.getLogger("mirofish.tweet_loop")

ROOT_POST_ID = "post_0"
REPAIR_MARKER = "MIROFISH_REPAIR"
PUBLIC_KINDS = {"reply": "reply", "quote": "quote", "repost": "repost"}
# Run 1900bc237dc94582a76689d515260094: one Grok CLI call for 12 personas
# was still running when the 120s per-request cap killed it. Each new call
# asks for at most one third of that batch.
FAILED_SINGLE_CALL_SLOTS = 12
PERSONA_CALL_MAX_SLOTS = FAILED_SINGLE_CALL_SLOTS // 3
_VALIDATORS = None


def _validators():
    global _VALIDATORS
    if _VALIDATORS is None:
        _schemas, _VALIDATORS = load_schemas()
    return _VALIDATORS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_run(run_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


def _update_run(run_id: str, **fields) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE runs SET {assignments}, updated_at = ? WHERE run_id = ?",
            (*fields.values(), _now(), run_id),
        )


def _artifact(run_id: str, kind: str, body) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts (artifact_id, run_id, kind, body_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, run_id, kind, _dump(body), _now()),
        )


def _cancelled(run_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT status, cancel_requested FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return row is None or bool(row["cancel_requested"]) or row["status"] == "cancelled"


def fit_code_points(text: str, low: int, high: int) -> str:
    if len(text) < low:
        text = text + ("。" * (low - len(text)))
    return text[:high]


def fallback_persona(slot: dict) -> dict:
    """Short template used only after persona repair fails. Not a model success."""

    agent_id = slot["agent_id"]
    persona = fit_code_points(
        (
            f"槽位{agent_id}使用短句模板。公开圈层是{slot['circle_label']}，"
            f"关系是{slot['relation_label']}，活跃度是{slot['activity_label']}。"
            "这不是订阅通道生成的人设。"
        ),
        60,
        120,
    )
    return {
        "agent_id": agent_id,
        "display_name": fit_code_points(f"虚构{agent_id}", 3, 30),
        "bio": "模板人设，非订阅生成",
        "persona": persona,
        "avoid_speaking_when": "没有具体句子时保持沉默。",
        "persona_source": "persona_fallback",
    }


def build_persona_payload(slots: list[dict], *, audience_version: str, seed: int) -> dict:
    """Public labels only. The draft tweet is not an input."""

    public = []
    for slot in slots:
        public.append({
            "agent_id": slot["agent_id"],
            "circle": slot["circle_label"],
            "relation": slot["relation_label"],
            "activity": slot["activity_label"],
            "language": slot["language_label"],
            "influence": slot["influence_label"],
            "prior_stance": slot["prior_stance_label"],
        })
    return {
        "task": "persona_batch",
        "schema_version": "2.0",
        "audience_version": audience_version,
        "language": "zh",
        "seed": seed,
        "slots": public,
        "output_contract": {
            "schema_version": "2.0",
            "shape": "只返回一个 JSON 对象，不要说明。",
            "display_name": "以虚构开头，不超过 12 个码点。",
            "bio": "不超过 16 个码点。",
            "persona": "60 个码点，写满即停，不要写到 120。",
            "avoid_speaking_when": "不超过 16 个码点。",
        },
    }


def persona_call_room(round_count: int) -> int:
    """Persona generations that still leave one call per wave and the report reserve."""

    return max(1, RUN_MAX_REQUESTS - REPORT_RESERVE_CALLS - max(1, round_count))


def persona_slots_per_call(agent_count: int, round_count: int) -> int:
    """Slots in one persona call. Shrink a 12-slot call only when the cap allows it."""

    if agent_count < 1:
        return 1
    needed = math.ceil(agent_count / PERSONA_CALL_MAX_SLOTS)
    if needed <= persona_call_room(round_count):
        return PERSONA_CALL_MAX_SLOTS
    return agent_count


def persona_groups(slots: list[dict], round_count: int) -> list[list[dict]]:
    size = persona_slots_per_call(len(slots), round_count)
    return [slots[index:index + size] for index in range(0, len(slots), size)]


def persona_call_seconds(slot_count: int) -> float:
    """Time implied by the 12-slot call that was still running at the cap."""

    if slot_count < 0:
        raise ValueError("slot_count must be non-negative")
    return REQUEST_TIMEOUT_SECONDS * slot_count / FAILED_SINGLE_CALL_SLOTS


def persona_cache_key(profile: dict, payload: dict) -> str:
    route = profile["routes"]["persona"]
    material = {
        "channel": route.get("channel"),
        "model_id": route.get("model_id"),
        "model_id_status": route.get("model_id_status"),
        "cli_version": "待验",
        "schema": "tweet_persona_batch.v2",
        "audience_version": payload["audience_version"],
        "language": payload["language"],
        "seed": payload["seed"],
        "slots": payload["slots"],
        "prompt_hash": hashlib.sha256(_dump(payload).encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(_dump(material).encode("utf-8")).hexdigest()


def _cache_get(cache_key: str) -> list[dict] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT body_json FROM persona_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if row is None:
        return None
    loaded = json.loads(row["body_json"])
    return loaded if isinstance(loaded, list) else None


def _cache_put(cache_key: str, personas: list[dict]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO persona_cache (cache_key, body_json, created_at)
            VALUES (?, ?, ?)
            """,
            (cache_key, _dump(personas), _now()),
        )


def _parse_object(text: str | None) -> dict | None:
    return parse_model_object(text)


_PERSONA_FIELDS = ("agent_id", "display_name", "bio", "persona", "avoid_speaking_when")
_ACTION_FIELDS = ("action", "target_id", "text", "expressed_stance", "trigger_span")
_SPAN_FIELDS = ("start", "end", "text")


def _normalize_persona_item(item: dict) -> dict:
    """Keep known fields. Prefix 虚构 and trim overlong text. Do not pad a short persona."""

    kept = {key: item[key] for key in _PERSONA_FIELDS if key in item}
    name = kept.get("display_name")
    if isinstance(name, str):
        stripped = name.strip()
        if stripped and not stripped.startswith("虚构"):
            stripped = "虚构" + stripped
        kept["display_name"] = stripped[:30]
    for key, limit in (("bio", 40), ("persona", 120), ("avoid_speaking_when", 60)):
        value = kept.get(key)
        if isinstance(value, str) and len(value) > limit:
            kept[key] = value[:limit]
    return kept


def _normalize_action(action: dict) -> dict:
    kept = {key: action[key] for key in _ACTION_FIELDS if key in action}
    span = kept.get("trigger_span")
    if isinstance(span, dict):
        kept["trigger_span"] = {key: span[key] for key in _SPAN_FIELDS if key in span}
    return kept


def _version_ok(document: dict, collection_key: str) -> bool:
    if document.get("schema_version") == "2.0":
        return True
    if "schema_version" not in document and isinstance(document.get(collection_key), list):
        return True
    return False


def _same_round(value, round_number: int) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int) and value == round_number:
        return True
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) == round_number
    return False


def _result_rows(document: dict) -> list | None:
    if isinstance(document.get("results"), list):
        return document["results"]
    if "results" not in document and isinstance(document.get("actions"), list):
        return document["actions"]
    return None


def _action_errors(item: dict, action: dict, draft: str) -> list[str]:
    codes = []
    if set(action) - {"action", "target_id", "text", "expressed_stance", "trigger_span"}:
        codes.append("schema")
    codes.extend(
        "schema" for _error in schema_errors(_validators()["action"], action)
    )
    chosen = action.get("action")
    candidate = item.get("candidate_action")
    if chosen not in ("none", candidate):
        codes.append("candidate_action")
    visible = {
        entry.get("id")
        for entry in item.get("visible_timeline") or []
        if isinstance(entry, dict)
    }
    target = action.get("target_id")
    if chosen == "like" and target == item.get("agent_id"):
        codes.append("self_like")
    if chosen not in (None, "none") and target not in visible:
        codes.append("invisible_or_future_target")
    span = action.get("trigger_span")
    if chosen in ("reply", "quote"):
        if not isinstance(span, dict):
            codes.append("trigger_span")
        else:
            start = span.get("start")
            end = span.get("end")
            snippet = span.get("text")
            if not isinstance(start, int) or not isinstance(end, int) or end < start:
                codes.append("trigger_span")
            elif draft[start:end] != snippet:
                codes.append("trigger_span")
    return list(dict.fromkeys(codes))


def interpret_personas(text: str | None, slots: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    expected = [slot["agent_id"] for slot in slots]
    document = _parse_object(text)
    if document is None:
        return {}, [{"agent_id": agent_id, "codes": ["json"]} for agent_id in expected]
    personas = document.get("personas")
    if not _version_ok(document, "personas") or not isinstance(personas, list):
        return {}, [{"agent_id": agent_id, "codes": ["schema"]} for agent_id in expected]
    actual = [item.get("agent_id") if isinstance(item, dict) else None for item in personas]
    duplicates = {agent_id for agent_id in actual if actual.count(agent_id) > 1}
    locked: dict[str, dict] = {}
    errors = []
    expected_set = set(expected)
    for agent_id in expected:
        matches = [
            item for item in personas
            if isinstance(item, dict) and item.get("agent_id") == agent_id
        ]
        if agent_id in duplicates or len(matches) != 1:
            code = "duplicate_id" if agent_id in duplicates else "missing_id"
            errors.append({"agent_id": agent_id, "codes": [code]})
            continue
        item = _normalize_persona_item(matches[0])
        item_errors = schema_errors(_validators()["persona_batch"], {
            "schema_version": "2.0",
            "personas": [item],
        })
        if item_errors or agent_id not in expected_set:
            errors.append({"agent_id": agent_id, "codes": ["schema"]})
            continue
        item["persona_source"] = "subscription_cli"
        locked[agent_id] = item
    extra = sorted({agent_id for agent_id in actual if agent_id not in expected_set and agent_id})
    if extra:
        errors.append({"agent_id": None, "codes": ["unexpected_id"], "ids": extra})
    coverage = exact_id_coverage([item["agent_id"] for item in locked.values()], expected)
    if coverage and not errors:
        errors.append({"agent_id": None, "codes": ["id_coverage"]})
    return locked, [item for item in errors if item.get("agent_id") not in locked]


def interpret_actions(
    text: str | None,
    *,
    round_number: int,
    items: list[dict],
    draft: str,
) -> tuple[dict[str, dict], list[dict]]:
    expected = [item["agent_id"] for item in items]
    by_id = {item["agent_id"]: item for item in items}
    document = _parse_object(text)
    if document is None:
        return {}, [{"agent_id": agent_id, "codes": ["json"]} for agent_id in expected]
    results = _result_rows(document)
    version_ok = _version_ok(document, "results") or _version_ok(document, "actions")
    if not version_ok or not _same_round(document.get("round"), round_number) or not isinstance(results, list):
        return {}, [{"agent_id": agent_id, "codes": ["schema"]} for agent_id in expected]
    if not expected:
        actual_ids = [
            item.get("agent_id") for item in results if isinstance(item, dict)
        ]
        if actual_ids:
            return {}, [{"agent_id": None, "codes": ["unexpected_id"], "ids": actual_ids}]
        return {}, []
    actual = [item.get("agent_id") if isinstance(item, dict) else None for item in results]
    duplicates = {agent_id for agent_id in actual if agent_id and actual.count(agent_id) > 1}
    locked: dict[str, dict] = {}
    errors = []
    for agent_id in expected:
        matches = [
            item for item in results
            if isinstance(item, dict) and item.get("agent_id") == agent_id
        ]
        if agent_id in duplicates or len(matches) != 1:
            code = "duplicate_id" if agent_id in duplicates else "missing_id"
            errors.append({"agent_id": agent_id, "codes": [code]})
            continue
        action = matches[0].get("action")
        if not isinstance(action, dict):
            errors.append({"agent_id": agent_id, "codes": ["schema"]})
            continue
        action = _normalize_action(action)
        codes = _action_errors(by_id[agent_id], action, draft)
        if codes:
            errors.append({"agent_id": agent_id, "codes": codes})
            continue
        locked[agent_id] = action
    return locked, [item for item in errors if item.get("agent_id") not in locked]


def private_bleed(items: list[dict], results: list[dict]) -> list[dict]:
    """Flag reply text that copies another account's private persona."""

    findings = []
    visible_blob = {}
    for item in items:
        parts = [entry.get("text") or "" for entry in item.get("visible_timeline") or []]
        parts.append(item.get("draft_text") or "")
        parts.append(item.get("persona") or "")
        visible_blob[item["agent_id"]] = "\n".join(parts)
    for result in results:
        if not isinstance(result, dict):
            continue
        action = result.get("action") if isinstance(result.get("action"), dict) else {}
        text = action.get("text") or ""
        actor = result.get("agent_id")
        if not isinstance(text, str) or len(text) < 12 or actor not in visible_blob:
            continue
        for other in items:
            if other["agent_id"] == actor:
                continue
            persona = other.get("persona") or ""
            for start in range(0, max(0, len(persona) - 11)):
                piece = persona[start:start + 12]
                if piece and piece in text and piece not in visible_blob[actor]:
                    findings.append({
                        "agent_id": actor,
                        "source_agent_id": other["agent_id"],
                        "match_length": 12,
                    })
                    break
    return findings


def shared_context_risk(item_count: int, findings: list[dict]) -> dict:
    if item_count >= 2:
        residual = "同一次模型上下文能读到同批私有人设，不能把这条请求称为物理隔离。"
    elif item_count == 1:
        residual = "这一波只有一个非沉默候选，没有第二份私有人设进入同一次请求。"
    else:
        residual = "这一波没有非沉默候选，空批次没有携带私有人设。"
    return {
        "physical_isolation": False,
        "shared_model_context": item_count >= 2,
        "private_personas_in_one_request": item_count,
        "private_bleed": findings,
        "residual_risk": residual,
    }


def _repair_note(errors: list[dict]) -> str:
    return _dump({
        "task": "repair",
        "marker": REPAIR_MARKER,
        "errors": errors,
        "rule": "只补尚未接受的条目。不要改写已经接受的条目，不要把缺失填成 none。",
    })


def _call_once(run_id, *, role, logical_batch, messages, kind, clock, on_step=None):
    if on_step is not None:
        on_step()
    logger.info("tweet loop call role=%s batch=%s kind=%s", role, logical_batch, kind)
    return generate(
        run_id,
        role=role,
        logical_batch=logical_batch,
        messages=messages,
        kind=kind,
        clock=clock,
    )


def _call_with_one_repair(run_id, *, role, logical_batch, messages, clock, accept, on_step=None):
    first = _call_once(
        run_id,
        role=role,
        logical_batch=logical_batch,
        messages=messages,
        kind="generation",
        clock=clock,
        on_step=on_step,
    )
    if not first.sent:
        return {"sent": False, "error": first.error_code, "locked": {}, "errors": [], "attempts": 0}
    locked, errors = accept(first.text)
    attempts = 1
    if errors and first.status == "completed":
        second = _call_once(
            run_id,
            role=role,
            logical_batch=logical_batch,
            messages=messages + [{"role": "user", "content": _repair_note(errors)}],
            kind="repair",
            clock=clock,
            on_step=on_step,
        )
        attempts = 2
        if second.sent and second.status == "completed":
            more_locked, more_errors = accept(second.text)
            for agent_id, value in more_locked.items():
                locked.setdefault(agent_id, value)
            errors = [item for item in more_errors if item.get("agent_id") not in locked]
        elif not second.sent:
            errors = errors + [{"agent_id": None, "codes": [second.error_code or "repair_not_sent"]}]
    return {"sent": True, "error": None, "locked": locked, "errors": errors, "attempts": attempts}


def _public_timeline(run_id: str, draft: str, before_round: int) -> list[dict]:
    timeline = [{"id": ROOT_POST_ID, "kind": "root_post", "text": draft}]
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT action_id, action, payload_json
            FROM actions
            WHERE run_id = ? AND "round" < ? AND outcome = 'completed'
            ORDER BY "round", action_id
            """,
            (run_id, before_round),
        ).fetchall()
    for row in rows:
        kind = PUBLIC_KINDS.get(row["action"])
        if kind is None:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        text = action.get("text") or ""
        timeline.append({"id": row["action_id"], "kind": kind, "text": text})
    return timeline


def _wave_input(round_number: int, items: list[dict], personas: dict[str, dict], timeline: list[dict], draft: str, author: str) -> dict:
    encoded = []
    for slot in items:
        persona = personas[slot["agent_id"]]
        encoded.append({
            "agent_id": slot["agent_id"],
            "persona": persona["persona"],
            "candidate_action": slot["candidate_action"],
            "visible_timeline": timeline,
            "author_context": author,
            "draft_text": draft,
        })
    return {
        "schema_version": "2.0",
        "round": round_number,
        "items": encoded,
        "output_contract": {
            "schema_version": "2.0",
            "round": round_number,
            "shape": "只返回一个 JSON 对象，键是 schema_version、round、results。不要输出第二个 JSON。",
            "results": "每条含 agent_id 和 action。action 只能是该条目的 candidate_action，或 none。不要把缺失填成 none。",
            "none": "target_id、text、trigger_span 为 null，expressed_stance 为 unexpressed。",
            "like_or_repost": "text 为 null，expressed_stance 为 unexpressed，target_id 必须是可见时间线上的 id。repost 的 target_id 是 post_0。",
            "reply_or_quote": "target_id 必须是 post_0。text 为 1 到 140 个码点。trigger_span.text 必须等于草稿里对应的码点切片。不要编造时间线上不存在的目标。",
        },
    }


def _insert_wave(run_id: str, round_number: int, wave_slots: list[dict], candidates: dict[str, str], locked: dict[str, dict], errors: list[dict]) -> None:
    error_by_id = {}
    for item in errors:
        agent_id = item.get("agent_id")
        if agent_id:
            error_by_id.setdefault(agent_id, []).extend(item.get("codes") or [])
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for slot in wave_slots:
            agent_id = slot["agent_id"]
            exposure_id = f"exp-{run_id[:12]}-{agent_id}"
            action_id = f"act-{run_id[:12]}-r{round_number}-{agent_id}"
            conn.execute(
                """
                INSERT INTO exposures (exposure_id, run_id, agent_id, "round")
                VALUES (?, ?, ?, ?)
                """,
                (exposure_id, run_id, agent_id, round_number),
            )
            candidate = candidates[agent_id]
            if candidate == "none":
                payload = {
                    "source": "rule",
                    "action": {
                        "action": "none",
                        "target_id": None,
                        "text": None,
                        "expressed_stance": "unexpressed",
                        "trigger_span": None,
                    },
                }
                conn.execute(
                    """
                    INSERT INTO actions (
                        action_id, run_id, exposure_id, agent_id, "round",
                        source, outcome, action, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'rule', 'completed', 'none', ?, ?)
                    """,
                    (action_id, run_id, exposure_id, agent_id, round_number, _dump(payload), now),
                )
                continue
            if agent_id in locked:
                action = locked[agent_id]
                payload = {"source": "model", "action": action}
                conn.execute(
                    """
                    INSERT INTO actions (
                        action_id, run_id, exposure_id, agent_id, "round",
                        source, outcome, action, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'model', 'completed', ?, ?, ?)
                    """,
                    (
                        action_id,
                        run_id,
                        exposure_id,
                        agent_id,
                        round_number,
                        action.get("action"),
                        _dump(payload),
                        now,
                    ),
                )
                continue
            payload = {
                "source": "model",
                "action": None,
                "errors": error_by_id.get(agent_id) or ["missing_id"],
            }
            conn.execute(
                """
                INSERT INTO actions (
                    action_id, run_id, exposure_id, agent_id, "round",
                    source, outcome, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'model', 'missing', NULL, ?, ?)
                """,
                (action_id, run_id, exposure_id, agent_id, round_number, _dump(payload), now),
            )
        conn.commit()


def _ensure_personas(run, slots, *, seed: int, clock, on_step=None) -> tuple[dict[str, dict] | None, dict]:
    profile = json.loads(run["capability_json"])
    payload = build_persona_payload(slots, audience_version=run["audience_version"], seed=seed)
    cache_key = persona_cache_key(profile, payload)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("tweet loop persona cache=hit")
        return {item["agent_id"]: item for item in cached}, {
            "cache": "hit",
            "calls": 0,
            "fallback": False,
            "stopped": False,
        }
    expected = [slot["agent_id"] for slot in slots]
    groups = persona_groups(slots, int(run["round_count"]))
    locked: dict[str, dict] = {}
    errors: list[dict] = []
    attempts = 0
    sent_any = False
    stopped_error = None
    for index, group in enumerate(groups, start=1):
        payload = build_persona_payload(group, audience_version=run["audience_version"], seed=seed)
        _artifact(run["run_id"], "persona_prompt", payload)
        batch = "persona" if len(groups) == 1 else f"persona-{index}"

        def accept(text: str | None, group=group):
            return interpret_personas(text, group)

        result = _call_with_one_repair(
            run["run_id"],
            role="persona",
            logical_batch=batch,
            messages=[{"role": "user", "content": _dump(payload)}],
            clock=clock,
            accept=accept,
            on_step=on_step,
        )
        if not result["sent"]:
            stopped_error = result["error"]
            if not sent_any:
                return None, {
                    "cache": "miss",
                    "calls": 0,
                    "fallback": False,
                    "stopped": True,
                    "error": stopped_error,
                }
            break
        sent_any = True
        attempts += result["attempts"]
        locked.update(result["locked"])
        errors.extend(result["errors"])
    personas = dict(locked)
    fallback_ids = [agent_id for agent_id in expected if agent_id not in personas]
    slot_by_id = {slot["agent_id"]: slot for slot in slots}
    for agent_id in fallback_ids:
        personas[agent_id] = fallback_persona(slot_by_id[agent_id])
    ordered = [personas[agent_id] for agent_id in expected]
    _artifact(run["run_id"], "persona_batch", {
        "fallback_ids": fallback_ids,
        "errors": errors,
        "personas": ordered,
    })
    if not fallback_ids:
        _cache_put(cache_key, ordered)
    logger.info("tweet loop persona cache=miss fallback=%s", bool(fallback_ids))
    return personas, {
        "cache": "miss",
        "calls": attempts,
        "fallback": bool(fallback_ids),
        "fallback_ids": fallback_ids,
        "stopped": False,
        "errors": errors,
    }


def execute_loop(
    run_id: str,
    *,
    seed: int = 1,
    clock=None,
    candidates: dict[str, str] | None = None,
    after_persona=None,
    on_step=None,
) -> dict:
    """Run personas, waves, and one report finalization for a persisted run."""

    from .report import finalize_report, finish_cancelled

    run = _load_run(run_id)
    if _cancelled(run_id):
        finish_cancelled(run_id)
        return {"status": "cancelled", "run_id": run_id}
    with connect() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM exposures WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
    if existing:
        return {"status": run["status"], "run_id": run_id, "already_executed": True}

    slots = build_slots(int(run["agent_count"]), seed)
    if candidates is None:
        drawn = draw_candidates(slots, seed)
    else:
        drawn = dict(candidates)
        missing = [slot["agent_id"] for slot in slots if slot["agent_id"] not in drawn]
        if missing:
            raise ValueError("候选覆盖必须包含每个槽位。")
    _update_run(run_id, status="preparing")
    if on_step is not None:
        on_step()
    personas, persona_meta = _ensure_personas(run, slots, seed=seed, clock=clock, on_step=on_step)
    if after_persona is not None:
        after_persona()
    summary = {
        "run_id": run_id,
        "seed": seed,
        "agent_count": len(slots),
        "round_count": int(run["round_count"]),
        "wave_sizes": [len(wave) for wave in split_waves(slots, int(run["round_count"]))],
        "persona": persona_meta,
        "waves": [],
        "waves_skipped": [],
        "report_call": None,
        "mock_only": ["model_text"],
        "shared_context_risk": [],
    }
    if persona_meta.get("stopped") or personas is None:
        for round_number in range(1, int(run["round_count"]) + 1):
            summary["waves_skipped"].append({
                "round": round_number,
                "reason": persona_meta.get("error") or "persona_not_sent",
            })
        outcome = finalize_report(
            run,
            seed=seed,
            summary=summary,
            clock=clock,
            on_step=on_step,
            complete_allowed=False,
        )
        summary["status"] = outcome["run_status"]
        summary["report_call"] = outcome["report_call"]
        _artifact(run_id, "loop_summary", summary)
        return summary

    _update_run(run_id, status="running")
    draft = run["draft_text"]
    author = run["author_context"]
    stop_error = None
    for round_number, wave in enumerate(split_waves(slots, int(run["round_count"])), start=1):
        if _cancelled(run_id):
            summary["waves_skipped"].append({"round": round_number, "reason": "cancelled"})
            stop_error = "cancelled"
            break
        selected = model_items(wave, drawn)
        timeline = _public_timeline(run_id, draft, round_number)
        for slot in selected:
            slot["candidate_action"] = drawn[slot["agent_id"]]
        payload = _wave_input(round_number, selected, personas, timeline, draft, author)
        _artifact(run_id, "wave_input", payload)
        messages = [{"role": "user", "content": _dump(payload)}]

        def accept(text: str | None, payload=payload, round_number=round_number):
            return interpret_actions(
                text,
                round_number=round_number,
                items=payload["items"],
                draft=draft,
            )

        result = _call_with_one_repair(
            run_id,
            role="agent",
            logical_batch=f"wave-{round_number}",
            messages=messages,
            clock=clock,
            accept=accept,
            on_step=on_step,
        )
        if not result["sent"]:
            stop_error = result["error"] or "not_sent"
            summary["waves_skipped"].append({"round": round_number, "reason": stop_error})
            for later in range(round_number + 1, int(run["round_count"]) + 1):
                summary["waves_skipped"].append({"round": later, "reason": stop_error})
            break
        findings = private_bleed(payload["items"], [
            {"agent_id": agent_id, "action": action}
            for agent_id, action in result["locked"].items()
        ])
        risk = shared_context_risk(len(payload["items"]), findings)
        summary["shared_context_risk"].append({"round": round_number, **risk})
        _insert_wave(run_id, round_number, wave, drawn, result["locked"], result["errors"])
        summary["waves"].append({
            "round": round_number,
            "slots": len(wave),
            "model_items": len(payload["items"]),
            "attempts": result["attempts"],
            "errors": result["errors"],
            "physical_isolation": False,
            "shared_model_context": risk["shared_model_context"],
            "private_bleed": findings,
        })

    complete_allowed = (
        stop_error != "cancelled"
        and not _cancelled(run_id)
        and not summary["waves_skipped"]
        and not summary["persona"].get("fallback")
        and not any(wave.get("errors") for wave in summary["waves"])
    )
    outcome = finalize_report(
        run,
        seed=seed,
        summary=summary,
        clock=clock,
        on_step=on_step,
        complete_allowed=complete_allowed,
    )
    summary["status"] = outcome["run_status"]
    summary["report_call"] = outcome["report_call"]
    _artifact(run_id, "loop_summary", summary)
    logger.info("tweet loop finish status=%s", outcome["run_status"])
    return summary
