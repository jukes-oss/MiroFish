"""Host-owned tweet report.

Statistics, engagement tier, backlash risk, top-reply order, and usage are
computed here. One subscription call may organize rewrites. A failed repair
becomes a programmed degraded document. Uncalibrated text is a 模拟备忘.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from contracts.check_contracts import load_schemas, schema_errors

from ..providers.gateway import generate, usage
from ..providers.limits import PHYSICAL_STATUSES, RUN_MAX_REQUESTS, RUN_MAX_WALL_SECONDS
from ..providers.profile import load_profile
from .audience import build_slots
from .db import TERMINAL_STATUSES, connect
from .model_json import parse_model_object

logger = logging.getLogger("mirofish.tweet_report")

ENGAGEMENT_MEANING = "仅描述当前模拟样本的互动，不预测真实浏览量或点赞数。"
MEMO_LABEL = "模拟备忘"
BEHAVIOR_PRIOR = "silent_v1"
RISK_RULE = "risk_rules_v1"
REPAIR_MARKER = "MIROFISH_REPAIR"
_CHANNELS = ("grok_cli", "codex_cli", "ollama")
_VARIANTS = ("preserve_claim", "add_boundaries", "change_style")
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


def _parse_object(text: str | None) -> dict | None:
    return parse_model_object(text)


def report_object_schema() -> dict:
    """The report object already used to validate the stored document."""

    from contracts.check_contracts import SCHEMA_DIR, SCHEMA_FILES

    document = json.loads((SCHEMA_DIR / SCHEMA_FILES["report"]).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("type") != "object":
        raise RuntimeError("报告 schema 不是对象。")
    return document


def report_cli_extra_args() -> list[str]:
    """Flags after ``-p`` and the prompt for a tweet report generation or repair."""

    schema = json.dumps(report_object_schema(), ensure_ascii=False, separators=(",", ":"))
    return [
        "--max-turns",
        "1",
        "--json-schema",
        schema,
        "--disallowed-tools",
        "run_terminal_cmd,run_terminal_command",
        "--no-subagents",
        "--disable-web-search",
        "--no-plan",
        "--no-memory",
    ]


def _span_ok(draft: str, span) -> bool:
    if not isinstance(span, dict):
        return False
    start = span.get("start")
    end = span.get("end")
    snippet = span.get("text")
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(snippet, str):
        return False
    if not (0 <= start < end <= len(draft)):
        return False
    return draft[start:end] == snippet


def _fallback_span(draft: str) -> dict | None:
    if not draft:
        return None
    return {"start": 0, "end": 1, "text": draft[0]}


def engagement_tier(exposed: int, root_engaged: int) -> dict:
    """Point estimate on this sample. The model does not choose the tier."""

    if exposed <= 0:
        return {
            "tier": "insufficient_data",
            "tier_reason": "没有有效曝光，不能划分互动档位。",
            "rate_interval": None,
            "meaning": ENGAGEMENT_MEANING,
        }
    rate = root_engaged / exposed
    if rate < 0.15:
        tier = "low"
        reason = "当前模拟样本的原帖互动比例低于 0.15。"
    elif rate < 0.40:
        tier = "medium"
        reason = "当前模拟样本的原帖互动比例低于 0.40。"
    else:
        tier = "high"
        reason = "当前模拟样本的原帖互动比例达到 0.40 或更高。"
    return {
        "tier": tier,
        "tier_reason": reason,
        "rate_interval": {"lower": rate, "upper": rate},
        "meaning": ENGAGEMENT_MEANING,
    }


def order_top_replies(replies: list[dict]) -> list[dict]:
    """Like-rate, then like count, then action id. At most five replies."""

    def key(item: dict):
        shown = int(item["shown_to"])
        likes = int(item["simulated_likes"])
        rate = (likes / shown) if shown else 0
        return (-rate, -likes, item["action_id"])

    ordered = sorted(replies, key=key)
    return ordered[:5]


def rewrite_problems(model: dict | None, evidence: dict) -> list[str]:
    """Problems in the rewrites the host can keep. Citations are separate."""

    if not isinstance(model, dict):
        return ["json"]
    draft = evidence.get("draft") or ""
    problems: list[str] = []
    rewrites = model.get("rewrites")
    if not isinstance(rewrites, list) or not (2 <= len(rewrites) <= 3):
        problems.append("rewrite_count")
        rewrites = []
    variants = []
    texts = []
    for item in rewrites:
        if not isinstance(item, dict):
            problems.append("rewrite_shape")
            continue
        variant = item.get("variant")
        text = item.get("text")
        changed = item.get("what_changed")
        if variant not in _VARIANTS:
            problems.append("rewrite_variant")
        else:
            variants.append(variant)
        if not isinstance(text, str) or not text or text == draft or "这是预测" in text:
            problems.append("rewrite_text")
        else:
            texts.append(text)
        if not isinstance(changed, str) or not changed.strip() or "这是预测" in changed:
            problems.append("rewrite_change")
        spans = item.get("changed_spans")
        if not isinstance(spans, list) or not spans or any(not _span_ok(draft, span) for span in spans):
            problems.append("bad_span")
        effect = item.get("expected_effect")
        if not isinstance(effect, dict) or effect.get("simulation_verified") is not False:
            problems.append("simulation_verified")
    if len(variants) != len(set(variants)):
        problems.append("rewrite_variant")
    if len(texts) != len(set(texts)):
        problems.append("rewrite_text")
    return list(dict.fromkeys(problems))


def semantic_problems(model: dict | None, evidence: dict) -> list[str]:
    """Reject fake citations, the wrong circle, a quote shown as a reply, and a bad span."""

    problems = rewrite_problems(model, evidence)
    if not isinstance(model, dict):
        return problems
    draft = evidence.get("draft") or ""
    actions = evidence.get("actions") or {}
    problems.extend(_citation_problems(model, actions, draft))
    return list(dict.fromkeys(problems))


def _citation_problems(model: dict, actions: dict, draft: str) -> list[str]:
    problems = []
    for item in model.get("top_replies") or []:
        if not isinstance(item, dict):
            problems.append("fake_citation")
            continue
        row = actions.get(item.get("action_id"))
        if row is None:
            problems.append("fake_citation")
            continue
        if row["action"] == "quote":
            problems.append("quote_as_reply")
        elif row["action"] != "reply":
            problems.append("not_reply")
        if not _groups_match(item.get("group_ids"), {row["circle"]}):
            problems.append("wrong_group")
    for collection, id_key in (
        ("trigger_lines", "evidence_ids"),
        ("disagreement_views", "evidence_ids"),
    ):
        rows = model.get(collection) or []
        if collection == "disagreement_views":
            disagreement = model.get("disagreement")
            rows = disagreement.get("views") if isinstance(disagreement, dict) else []
        for item in rows or []:
            problems.extend(_evidence_item_problems(item, actions, id_key))
    risk = model.get("backlash_risk")
    if isinstance(risk, dict):
        for item in risk.get("issues") or []:
            problems.extend(_evidence_item_problems(item, actions, "evidence_ids"))
            for span in item.get("trigger_spans") or []:
                if not _span_ok(draft, span):
                    problems.append("bad_span")
    for item in model.get("trigger_lines") or []:
        if isinstance(item, dict) and not _span_ok(draft, item.get("span")):
            problems.append("bad_span")
    return problems


def _evidence_item_problems(item, actions: dict, id_key: str) -> list[str]:
    if not isinstance(item, dict):
        return ["fake_citation"]
    evidence_ids = item.get(id_key)
    if not isinstance(evidence_ids, list) or not evidence_ids:
        return ["fake_citation"]
    problems = []
    circles = set()
    for evidence_id in evidence_ids:
        row = actions.get(evidence_id)
        if row is None:
            problems.append("fake_citation")
            continue
        circles.add(row["circle"])
    if circles and not _groups_match(item.get("group_ids") or item.get("triggered_groups"), circles):
        problems.append("wrong_group")
    return problems


def _groups_match(group_ids, circles: set[str]) -> bool:
    if not isinstance(group_ids, list) or not group_ids or not circles:
        return False
    return set(group_ids) == circles


def _action_body(row) -> dict:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    action = payload.get("action") if isinstance(payload, dict) else None
    return action if isinstance(action, dict) else {}


def collect_evidence(run, *, seed: int, summary: dict) -> dict:
    """Counts, tier, risk, and ordered replies from stored actions."""

    run_id = run["run_id"]
    draft = run["draft_text"]
    slots = build_slots(int(run["agent_count"]), seed)
    circles = {slot["agent_id"]: slot["circle"] for slot in slots}
    with connect() as conn:
        exposure_rows = conn.execute(
            'SELECT agent_id, "round" AS round FROM exposures WHERE run_id = ?',
            (run_id,),
        ).fetchall()
        action_rows = conn.execute(
            "SELECT * FROM actions WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    exposed = len(exposure_rows)
    later_exposed = {}
    for row in exposure_rows:
        later_exposed[int(row["round"])] = later_exposed.get(int(row["round"]), 0) + 1
    counts = {"none": 0, "like": 0, "reply": 0, "repost": 0, "quote": 0}
    missing = 0
    parsed = []
    for row in action_rows:
        body = _action_body(row)
        record = {
            "action_id": row["action_id"],
            "agent_id": row["agent_id"],
            "round": int(row["round"] or 0),
            "outcome": row["outcome"],
            "action": row["action"],
            "circle": circles.get(row["agent_id"]) or "",
            "text": body.get("text") if isinstance(body.get("text"), str) else "",
            "stance": body.get("expressed_stance"),
            "target_id": body.get("target_id"),
            "trigger_span": body.get("trigger_span") if isinstance(body.get("trigger_span"), dict) else None,
        }
        parsed.append(record)
        if row["outcome"] == "missing" or row["action"] is None:
            missing += 1
        elif row["action"] in counts and row["outcome"] == "completed":
            counts[row["action"]] += 1
    root_engaged = counts["like"] + counts["reply"] + counts["repost"] + counts["quote"]
    evaluated = max(0, exposed - missing)
    none_rate = (counts["none"] / exposed) if exposed else None
    engagement_rate = (root_engaged / exposed) if exposed else None
    opposing = [
        item for item in parsed
        if item["outcome"] == "completed"
        and item["action"] in ("reply", "quote")
        and item["stance"] == "opposing"
        and item["circle"]
    ]
    if exposed <= 0:
        risk_level = "insufficient_data"
        risk_limit = "有效曝光不足，不能判断反对强度。"
        issues = []
    elif not opposing:
        risk_level = "low"
        risk_limit = "没有观察到负面发言，不代表真实发布没有风险。"
        issues = []
    else:
        risk_level = "medium" if len(opposing) <= 2 else "high"
        risk_limit = "反对条数来自这次模拟的公开文字，不是真实发布风险。"
        issues = []
        for item in opposing[:6]:
            span = item["trigger_span"] if _span_ok(draft, item["trigger_span"]) else _fallback_span(draft)
            if span is None:
                continue
            issues.append({
                "reason": "该账号的公开文字标为反对。",
                "triggered_groups": [item["circle"]],
                "trigger_spans": [span],
                "evidence_ids": [item["action_id"]],
            })
    replies = []
    for item in parsed:
        if item["outcome"] != "completed" or item["action"] != "reply":
            continue
        if not item["text"] or not item["circle"]:
            continue
        shown = sum(count for round_number, count in later_exposed.items() if round_number > item["round"])
        likes = sum(
            1 for other in parsed
            if other["outcome"] == "completed"
            and other["action"] == "like"
            and other["target_id"] == item["action_id"]
            and other["round"] > item["round"]
        )
        replies.append({
            "action_id": item["action_id"],
            "agent_id": item["agent_id"],
            "text": item["text"][:140],
            "group_ids": [item["circle"]],
            "simulated_likes": likes,
            "shown_to": shown,
            "rank_basis": "simulated_like_rate" if shown else "unvalidated_candidate",
            "why_it_might_resonate": "这条回复只在当前模拟样本里排序，属于模拟备忘。",
            "limitation": (
                "这条回复之后没有模拟曝光，赞数不能拿来比较。"
                if shown == 0
                else "排序只用了后续波次的模拟点赞，没有真实互动。"
            ),
        })
    reasons = []
    if summary.get("persona", {}).get("stopped"):
        reasons.append("人设调用没有发出，后续波次没有开始。")
    elif summary.get("persona", {}).get("fallback"):
        reasons.append("人设使用了短句模板，不是订阅通道生成。")
    if missing:
        reasons.append("有账号的反应缺失，没有补记为划走。")
    if summary.get("waves_skipped"):
        reasons.append("有波次没有开始，没有补写曝光。")
    completed_rounds = len(summary.get("waves") or [])
    return {
        "draft": draft,
        "actions": {
            item["action_id"]: item
            for item in parsed
            if item["action_id"]
        },
        "metrics": {
            "planned_agents": int(run["agent_count"]),
            "exposed_agents": exposed,
            "evaluated_agents": evaluated,
            "missing_agents": missing,
            "action_counts": counts,
            "root_engaged_agents": root_engaged,
            "none_rate": none_rate,
            "engagement_rate": engagement_rate,
        },
        "engagement": engagement_tier(exposed, root_engaged),
        "top_replies": order_top_replies(replies),
        "backlash_risk": {
            "level": risk_level,
            "rule_version": RISK_RULE,
            "issues": issues,
            "limitations": [risk_limit],
        },
        "degradation_reasons": reasons,
        "completed_rounds": completed_rounds,
        "evidence_truncated": bool(summary.get("waves_skipped")),
    }


def _channel_identity(profile: dict | None, channel: str) -> tuple[str | None, str]:
    routes = (profile or {}).get("routes") or {}
    for route in routes.values():
        if isinstance(route, dict) and route.get("channel") == channel:
            status = route.get("model_id_status") or "unknown"
            model = route.get("model_id")
            if status == "unknown" or not isinstance(model, str) or not model:
                return None, "unknown"
            return model, status
    return None, "unknown"


def _resolved_channel(row, profile: dict | None) -> str | None:
    channel = row["channel"]
    if channel in _CHANNELS:
        return channel
    route = ((profile or {}).get("routes") or {}).get(row["role"] or "")
    if isinstance(route, dict) and route.get("channel") in _CHANNELS:
        return route["channel"]
    return None


def usage_document(run, *, clock=None) -> dict:
    """Backfill usage from gateway rows after the report attempt."""

    snapshot = usage(run["run_id"], clock=clock)
    profile = load_profile(run["capability_json"])
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT request_id, role, channel, status
            FROM provider_requests
            WHERE run_id = ? AND status IN ({",".join("?" for _ in PHYSICAL_STATUSES)})
            """,
            (run["run_id"], *PHYSICAL_STATUSES),
        ).fetchall()
        walls = conn.execute(
            """
            SELECT request_id, COALESCE(SUM(wall_ms), 0) AS wall_ms
            FROM budget_ledger
            WHERE run_id = ? AND entry_kind = 'wall_observed'
            GROUP BY request_id
            """,
            (run["run_id"],),
        ).fetchall()
    wall_by_request = {row["request_id"]: int(row["wall_ms"]) for row in walls}
    grouped: dict[str, dict] = {}
    for row in rows:
        channel = _resolved_channel(row, profile) or "ollama"
        bucket = grouped.setdefault(channel, {"requests": 0, "elapsed_ms": 0, "roles": set()})
        bucket["requests"] += 1
        bucket["elapsed_ms"] += wall_by_request.get(row["request_id"], 0)
        if row["role"]:
            bucket["roles"].add(row["role"])
    channels = []
    for channel in sorted(grouped):
        bucket = grouped[channel]
        model_id, model_status = _channel_identity(profile, channel)
        channels.append({
            "channel": channel,
            "model_id": model_id,
            "model_id_status": model_status,
            "roles": sorted(bucket["roles"]) or ["report"],
            "requests": bucket["requests"],
            "elapsed_ms": bucket["elapsed_ms"],
        })
    return {
        "execution_profile": run["execution_profile"],
        "request_limit": RUN_MAX_REQUESTS,
        "physical_requests": int(snapshot["physical"]),
        "reserved_requests": int(snapshot["reserved"]),
        "uncertain_requests": int(snapshot["uncertain"]),
        "wall_limit_seconds": RUN_MAX_WALL_SECONDS,
        "wall_elapsed_ms": int(snapshot["wall_elapsed_ms"]),
        "channels": channels,
    }


def _base_document(run, evidence: dict, usage_body: dict, *, seed: int, status: str, reasons: list[str], rewrites: list) -> dict:
    forced_insufficient = status == "failed"
    engagement = dict(evidence["engagement"])
    risk = {
        "level": evidence["backlash_risk"]["level"],
        "rule_version": RISK_RULE,
        "issues": list(evidence["backlash_risk"]["issues"]),
        "limitations": list(evidence["backlash_risk"]["limitations"]),
    }
    if forced_insufficient:
        engagement = engagement_tier(0, 0)
        risk["level"] = "insufficient_data"
        risk["issues"] = []
        risk["limitations"] = ["任务没有完成，不能判断反对强度。"]
    limitations = [
        f"未校准结果标记为{MEMO_LABEL}。",
        "受众比例是工程假设，不是人口普查。",
    ]
    return {
        "schema_version": "2.0",
        "run_id": run["run_id"],
        "status": status,
        "scope": {
            "kind": "simulation_only",
            "audience_version": run["audience_version"],
            "behavior_prior_version": BEHAVIOR_PRIOR,
            "seed": int(seed),
            "planned_rounds": int(run["round_count"]),
            "completed_rounds": int(evidence["completed_rounds"]),
            "calibrated": False,
            "evidence_truncated": bool(evidence["evidence_truncated"]) if status != "complete" else False,
        },
        "metrics": evidence["metrics"],
        "usage": usage_body,
        "engagement": engagement,
        "trigger_lines": [],
        "top_replies": [] if forced_insufficient else list(evidence["top_replies"]),
        "backlash_risk": risk,
        "disagreement": {
            "views": [],
            "silent_agents_are_not_supporters": True,
            "note": f"沉默账号不是支持者。未校准结果是{MEMO_LABEL}。",
        },
        "rewrites": rewrites,
        "confidence": {
            "level": "low",
            "calibration_status": "uncalibrated",
            "basis": f"未校准的{MEMO_LABEL}。受众比例、推荐排序和真实环境都没有用历史结果校准。",
            "uncertainties": [
                "受众比例是工程假设，不是人口普查。",
                "真实推荐算法和发布环境未知。",
                "虚构账号样本不能代表中文平台总体。",
            ],
        },
        "limitations": limitations,
        "degradation_reasons": [] if status == "complete" else reasons,
    }


def _clean_rewrites(model: dict | None, evidence: dict) -> list:
    if not isinstance(model, dict) or rewrite_problems(model, evidence):
        return []
    kept = []
    for item in model.get("rewrites") or []:
        kept.append({
            "variant": item["variant"],
            "text": item["text"],
            "what_changed": item["what_changed"],
            "changed_spans": [
                {"start": span["start"], "end": span["end"], "text": span["text"]}
                for span in item["changed_spans"]
            ],
            "expected_effect": {
                "hypothesis": item["expected_effect"]["hypothesis"],
                "tradeoff": item["expected_effect"]["tradeoff"],
                "simulation_verified": False,
            },
        })
    return kept


def _store_report(run_id: str, document: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts (artifact_id, run_id, kind, body_json, created_at)
            VALUES (?, ?, 'report', ?, ?)
            """,
            (uuid.uuid4().hex, run_id, _dump(document), _now()),
        )


def _report_exists(run_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT artifact_id FROM artifacts WHERE run_id = ? AND kind = 'report' LIMIT 1",
            (run_id,),
        ).fetchone()
    return row is not None


def _finish_run(run_id: str, *, status: str, error_code: str | None, error_message: str | None) -> None:
    with connect() as conn:
        current = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if current is not None and current["status"] in TERMINAL_STATUSES and current["status"] != status:
            if status != "cancelled":
                conn.execute(
                    """
                    UPDATE runs
                    SET worker_lease_owner = NULL, worker_lease_expires_ms = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (_now(), run_id),
                )
                return
        conn.execute(
            """
            UPDATE runs
            SET status = ?, error_code = ?, error_message = ?, finished_at = ?,
                worker_lease_owner = NULL, worker_lease_expires_ms = NULL, updated_at = ?
            WHERE run_id = ?
            """,
            (status, error_code, error_message, _now(), _now(), run_id),
        )


def assemble_document(
    run,
    evidence: dict,
    *,
    seed: int,
    clock,
    narrative: dict | None,
    problems: list[str],
    complete_allowed: bool,
    extra_reasons: list[str] | None = None,
) -> dict:
    usage_body = usage_document(run, clock=clock)
    reasons = list(evidence["degradation_reasons"])
    reasons.extend(extra_reasons or [])
    rewrites = []
    if narrative is not None and not problems:
        rewrites = _clean_rewrites(narrative, evidence)
    if problems:
        reasons.append("报告修复一次后仍未通过校验，没有编造改写或证据。")
    reasons = [item for item in dict.fromkeys(reasons) if item]
    clean = complete_allowed and not reasons and len(rewrites) >= 2 and evidence["metrics"]["missing_agents"] == 0
    status = "complete" if clean else "degraded"
    if status != "complete" and not reasons:
        reasons.append("模拟没有达到完整报告条件。")
    document = _base_document(
        run,
        evidence,
        usage_body,
        seed=seed,
        status=status,
        reasons=reasons,
        rewrites=rewrites,
    )
    errors = schema_errors(_validators()["report"], document)
    if errors:
        logger.info("tweet report schema_reject count=%s", len(errors))
        fallback_reasons = list(dict.fromkeys(reasons + ["报告文档未通过结构校验，已改为降级结果。"]))
        document = _base_document(
            run,
            evidence,
            usage_body,
            seed=seed,
            status="degraded",
            reasons=fallback_reasons,
            rewrites=[],
        )
        document["top_replies"] = []
        document["backlash_risk"]["issues"] = []
        document["engagement"] = engagement_tier(
            0 if document["metrics"]["exposed_agents"] == 0 else document["metrics"]["exposed_agents"],
            0 if document["metrics"]["exposed_agents"] == 0 else document["metrics"]["root_engaged_agents"],
        )
        if document["metrics"]["exposed_agents"] == 0:
            document["engagement"] = engagement_tier(0, 0)
            document["backlash_risk"]["level"] = "insufficient_data"
    return document


def _report_prompt(run, evidence: dict) -> dict:
    return {
        "task": "report_stats",
        "schema_version": "2.0",
        "draft_text": run["draft_text"],
        "host_metrics": evidence["metrics"],
        "host_engagement_tier": evidence["engagement"]["tier"],
        "host_risk_level": evidence["backlash_risk"]["level"],
        "reply_ids": [item["action_id"] for item in evidence["top_replies"]],
        "instruction": (
            "整段回复就是一个 JSON 对象，不要先写一句中文。不要跑脚本数码点。"
            "键只有 rewrites。不要输出 top_replies、trigger_lines、"
            "disagreement、backlash_risk、evidence_ids，也不要输出第二个 JSON。"
            "rewrites 为 2 到 3 条，variant 取 preserve_claim、add_boundaries、change_style 中互不相同的值。"
            "每条含 variant、text、what_changed、changed_spans、expected_effect。"
            "text 必须和草稿不同，且不得包含“这是预测”。"
            "changed_spans 的 start、end、text 写在这个 JSON 里，text 等于草稿里从 start 到 end 的原文。"
            "expected_effect 含 hypothesis 和 tradeoff，simulation_verified 必须是 false。"
            f"未校准说明写成{MEMO_LABEL}。不要计算用量，不要编造证据编号。"
        ),
    }


def finalize_report(
    run,
    *,
    seed: int,
    summary: dict,
    clock=None,
    on_step=None,
    complete_allowed: bool = False,
) -> dict:
    """One report call, one repair, then a programmed document if that fails."""

    run_id = run["run_id"]
    if _report_exists(run_id):
        return {"run_status": run["status"], "report_call": {"sent": False, "error": "already_stored"}}
    cancelled = _is_cancelled(run_id)
    evidence = collect_evidence(run, seed=seed, summary=summary)
    narrative = None
    problems = ["not_sent"]
    sent = False
    error = None
    if not cancelled:
        with connect() as conn:
            conn.execute(
                "UPDATE runs SET status = 'reporting', updated_at = ? WHERE run_id = ? AND status NOT IN ({})".format(
                    ",".join("?" for _ in TERMINAL_STATUSES)
                ),
                (_now(), run_id, *TERMINAL_STATUSES),
            )
        if on_step is not None:
            on_step()
        prompt = _dump(_report_prompt(run, evidence))
        report_args = report_cli_extra_args()
        first = generate(
            run_id,
            role="report",
            logical_batch="report",
            messages=[{"role": "user", "content": prompt}],
            kind="generation",
            clock=clock,
            cli_extra_args=report_args,
        )
        sent = bool(first.sent)
        error = None if first.sent else first.error_code
        if first.sent and first.status == "completed":
            narrative = _parse_object(first.text)
            problems = rewrite_problems(narrative, evidence)
            if problems:
                if on_step is not None:
                    on_step()
                second = generate(
                    run_id,
                    role="report",
                    logical_batch="report",
                    messages=[
                        {"role": "user", "content": prompt},
                        {"role": "user", "content": _dump({
                            "task": "repair",
                            "marker": REPAIR_MARKER,
                            "errors": problems,
                            "rule": (
                                "整段回复就是要求的 JSON，不要先写一句中文。"
                                "不要跑脚本数码点。不要编造证据编号，不要把引用写成回复。"
                            ),
                        })},
                    ],
                    kind="repair",
                    clock=clock,
                    cli_extra_args=report_args,
                )
                sent = sent or bool(second.sent)
                if second.sent and second.status == "completed":
                    repaired = _parse_object(second.text)
                    repaired_problems = rewrite_problems(repaired, evidence)
                    if not repaired_problems:
                        narrative = repaired
                        problems = []
                    else:
                        narrative = None
                        problems = repaired_problems
                else:
                    narrative = None
        elif not first.sent:
            narrative = None
            problems = []
        else:
            narrative = None
            problems = []
            error = first.error_code or "unusable_response"
    else:
        problems = []
        error = "cancelled"
    extra = ["已取消，未发出新的模型请求。"] if cancelled else []
    if not cancelled and not sent:
        extra.append("报告调用没有发出。")
    elif not cancelled and narrative is None and not problems:
        extra.append("报告调用没有返回可用结果。")
    document = assemble_document(
        run,
        evidence,
        seed=seed,
        clock=clock,
        narrative=narrative,
        problems=problems,
        complete_allowed=complete_allowed and not cancelled and sent,
        extra_reasons=extra,
    )
    fresh_errors = schema_errors(_validators()["report"], document)
    if fresh_errors:
        logger.info("tweet report still_invalid count=%s", len(fresh_errors))
        document = _programmed_minimum(run, evidence, seed=seed, clock=clock, reasons=[
            "报告文档未通过结构校验，已改为降级结果。",
            *extra,
            *evidence["degradation_reasons"],
        ])
    _store_report(run_id, document)
    if _is_cancelled(run_id):
        cancelled = True
    if cancelled:
        run_status = "cancelled"
        error_code = "cancelled"
        message = "已取消。未开始的波次没有发空调用，也没有补写曝光。"
    elif document["status"] == "complete":
        run_status = "complete"
        error_code = None
        message = None
    else:
        run_status = "degraded"
        error_code = "loop_degraded"
        message = f"模拟循环已结束。未校准结果是{MEMO_LABEL}。"
    _finish_run(run_id, status=run_status, error_code=error_code, error_message=message)
    logger.info("tweet report finish status=%s sent=%s", run_status, sent)
    return {
        "run_status": run_status,
        "report_call": {
            "sent": sent,
            "error": error,
            "document_status": document["status"],
            "problems": problems,
        },
    }


def _programmed_minimum(run, evidence: dict, *, seed: int, clock, reasons: list[str]) -> dict:
    usage_body = usage_document(run, clock=clock)
    cleaned = [item for item in reasons if isinstance(item, str) and item.strip()]
    if not cleaned:
        cleaned = ["报告文档未通过结构校验，已改为降级结果。"]
    document = _base_document(
        run,
        evidence,
        usage_body,
        seed=seed,
        status="degraded",
        reasons=list(dict.fromkeys(cleaned)),
        rewrites=[],
    )
    document["top_replies"] = []
    document["backlash_risk"]["issues"] = []
    if evidence["metrics"]["exposed_agents"] <= 0:
        document["engagement"] = engagement_tier(0, 0)
        document["backlash_risk"]["level"] = "insufficient_data"
        document["backlash_risk"]["limitations"] = ["有效曝光不足，不能判断反对强度。"]
    return document


def _is_cancelled(run_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT status, cancel_requested FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return row is None or bool(row["cancel_requested"]) or row["status"] == "cancelled"


def write_closed_report(run_id: str, reason: str) -> None:
    """Programmed document after a worker exception. No model call and no replay."""

    if _report_exists(run_id):
        return
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return
    summary = {"persona": {}, "waves": [], "waves_skipped": [{"round": 1, "reason": "worker_exception"}]}
    try:
        evidence = collect_evidence(run, seed=1, summary=summary)
    except ValueError:
        evidence = {
            "draft": run["draft_text"],
            "actions": {},
            "metrics": {
                "planned_agents": int(run["agent_count"]),
                "exposed_agents": 0,
                "evaluated_agents": 0,
                "missing_agents": 0,
                "action_counts": {"none": 0, "like": 0, "reply": 0, "repost": 0, "quote": 0},
                "root_engaged_agents": 0,
                "none_rate": None,
                "engagement_rate": None,
            },
            "engagement": engagement_tier(0, 0),
            "top_replies": [],
            "backlash_risk": {
                "level": "insufficient_data",
                "rule_version": RISK_RULE,
                "issues": [],
                "limitations": ["任务没有完成，不能判断反对强度。"],
            },
            "degradation_reasons": [],
            "completed_rounds": 0,
            "evidence_truncated": True,
        }
    usage_body = usage_document(run, clock=None)
    document = _base_document(
        run,
        evidence,
        usage_body,
        seed=1,
        status="failed",
        reasons=[reason],
        rewrites=[],
    )
    document["engagement"] = engagement_tier(0, 0)
    document["backlash_risk"]["level"] = "insufficient_data"
    document["backlash_risk"]["issues"] = []
    document["backlash_risk"]["limitations"] = ["任务没有完成，不能判断反对强度。"]
    document["top_replies"] = []
    document["scope"]["evidence_truncated"] = True
    document["scope"]["completed_rounds"] = 0
    errors = schema_errors(_validators()["report"], document)
    if errors:
        logger.info("tweet report closed_invalid count=%s", len(errors))
    _store_report(run_id, document)
    if run["status"] not in TERMINAL_STATUSES:
        _finish_run(
            run_id,
            status="failed",
            error_code="worker_exception",
            error_message="工作进程中断。任务没有自动重放。",
        )


def finish_cancelled(run_id: str) -> None:
    """Stop a claimed run that was cancelled before the loop sent a model call."""

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return
    if not _report_exists(run_id) and run["status"] not in ("queued",):
        summary = {
            "persona": {"stopped": False, "fallback": False},
            "waves": [],
            "waves_skipped": [
                {"round": number, "reason": "cancelled"}
                for number in range(1, int(run["round_count"]) + 1)
            ],
        }
        try:
            evidence = collect_evidence(run, seed=1, summary=summary)
        except ValueError:
            evidence = None
        if evidence is not None:
            document = _programmed_minimum(
                run,
                evidence,
                seed=1,
                clock=None,
                reasons=["已取消，未发出新的模型请求。", *evidence["degradation_reasons"]],
            )
            errors = schema_errors(_validators()["report"], document)
            if not errors:
                _store_report(run_id, document)
    if run["status"] not in TERMINAL_STATUSES:
        _finish_run(
            run_id,
            status="cancelled",
            error_code="cancelled",
            error_message="已取消，未发出新的模型请求。",
        )
    else:
        with connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET worker_lease_owner = NULL, worker_lease_expires_ms = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )
