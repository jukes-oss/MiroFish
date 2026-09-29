"""Check M0 schema files and fixtures.

This module only reads local JSON. It does not import the simulation engine
and it does not call a subscription CLI, Ollama, or a paid API.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

ROOT = Path(__file__).resolve().parent / "v2"
SCHEMA_DIR = ROOT / "schemas"
EXAMPLE_DIR = ROOT / "examples"

SCHEMA_FILES = {
    "persona_batch": "tweet_persona_batch.schema.json",
    "action": "tweet_action.schema.json",
    "action_batch_input": "tweet_action_batch_input.schema.json",
    "action_batch": "tweet_action_batch.schema.json",
    "report": "tweet_report.schema.json",
    "eval_manifest": "eval_manifest.schema.json",
}

DOLLAR_MARKERS = (
    "microusd",
    "billable_tokens",
    "price_version",
    "cost_basis",
    "budget_usd",
    '"currency"',
)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_schemas():
    schemas = {}
    resources = []
    for key, name in SCHEMA_FILES.items():
        schema = _read_json(SCHEMA_DIR / name)
        schemas[key] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    validators = {}
    for key, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        validators[key] = Draft202012Validator(schema, registry=registry)
    return schemas, validators


def schema_errors(validator, instance):
    errors = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path)):
        path = "/".join(str(part) for part in error.absolute_path)
        location = path or "<root>"
        errors.append(f"schema: {location}: {error.message}")
    return errors


def exact_id_coverage(actual_ids, expected_ids):
    errors = []
    if len(actual_ids) != len(set(actual_ids)):
        duplicates = sorted({item for item in actual_ids if actual_ids.count(item) > 1})
        errors.append(f"id_coverage: duplicate ids {duplicates}")
    actual = set(actual_ids)
    expected = set(expected_ids)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra or len(expected_ids) != len(set(expected_ids)):
        errors.append(
            "id_coverage: "
            f"missing={missing} extra={extra} "
            f"expected_unique={len(set(expected_ids)) == len(expected_ids)}"
        )
    return errors


def check_persona_batch(document, validators):
    output = document["output"]
    errors = schema_errors(validators["persona_batch"], output)
    if not isinstance(output, dict) or not isinstance(output.get("personas"), list):
        return errors
    actual = [
        item.get("agent_id")
        for item in output["personas"]
        if isinstance(item, dict)
    ]
    errors.extend(exact_id_coverage(actual, document["host_expected_agent_ids"]))
    return errors


def _index_items(items):
    by_id = {}
    ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        agent_id = item.get("agent_id")
        ids.append(agent_id)
        by_id[agent_id] = item
    return ids, by_id


def check_action_batch(document, validators):
    errors = schema_errors(validators["action_batch_input"], document.get("input"))
    errors.extend(schema_errors(validators["action_batch"], document.get("output")))
    action_input = document.get("input")
    output = document.get("output")
    if not isinstance(action_input, dict) or not isinstance(output, dict):
        return errors
    if action_input.get("round") != output.get("round"):
        errors.append("round_mismatch: input.round != output.round")
    items = action_input.get("items")
    results = output.get("results")
    if not isinstance(items, list) or not isinstance(results, list):
        return errors
    expected_ids, by_id = _index_items(items)
    actual_ids = [
        item.get("agent_id")
        for item in results
        if isinstance(item, dict)
    ]
    errors.extend(exact_id_coverage(actual_ids, expected_ids))
    for result in results:
        if not isinstance(result, dict):
            continue
        agent_id = result.get("agent_id")
        action = result.get("action")
        item = by_id.get(agent_id)
        if item is None or not isinstance(action, dict):
            continue
        chosen = action.get("action")
        candidate = item.get("candidate_action")
        if chosen not in (None, "none", candidate):
            errors.append(
                "candidate_action: "
                f"{agent_id} returned {chosen} for candidate {candidate}"
            )
        if chosen not in (None, "none"):
            visible = {
                entry.get("id")
                for entry in item.get("visible_timeline") or []
                if isinstance(entry, dict)
            }
            if action.get("target_id") not in visible:
                errors.append(
                    "target_visibility: "
                    f"{agent_id} target {action.get('target_id')} is not visible"
                )
    return errors


def check_usage_invariants(usage):
    if not isinstance(usage, dict):
        return ["usage_invariant: usage is missing"]
    errors = []
    physical = usage.get("physical_requests")
    reserved = usage.get("reserved_requests")
    uncertain = usage.get("uncertain_requests")
    limit = usage.get("request_limit")
    channels = usage.get("channels")
    if isinstance(channels, list) and all(isinstance(item, dict) for item in channels):
        channel_sum = sum(item.get("requests") or 0 for item in channels)
        if channel_sum != physical:
            errors.append(
                "usage_invariant: "
                f"sum(channels.requests)={channel_sum} != physical_requests={physical}"
            )
    if (
        isinstance(physical, int)
        and isinstance(reserved, int)
        and isinstance(limit, int)
        and physical + reserved > limit
    ):
        errors.append(
            "usage_invariant: physical_requests + reserved_requests > request_limit"
        )
    if isinstance(uncertain, int) and isinstance(physical, int) and uncertain > physical:
        errors.append("usage_invariant: uncertain_requests > physical_requests")
    return errors


def check_report(document, validators):
    errors = schema_errors(validators["report"], document)
    if isinstance(document, dict):
        errors.extend(check_usage_invariants(document.get("usage")))
    return errors


def check_eval_manifest(document, validators):
    errors = schema_errors(validators["eval_manifest"], document)
    if not isinstance(document, dict):
        return errors
    cases = document.get("cases")
    collection = document.get("collection") if isinstance(document.get("collection"), dict) else {}
    if document.get("dataset_status") == "frozen" and isinstance(cases, list):
        dev = sum(1 for item in cases if isinstance(item, dict) and item.get("split") == "dev")
        holdout = sum(
            1 for item in cases if isinstance(item, dict) and item.get("split") == "holdout"
        )
        if dev != collection.get("dev_count") or holdout != collection.get("holdout_count"):
            errors.append(
                "eval_split: "
                f"dev={dev} holdout={holdout} "
                f"expected {collection.get('dev_count')}/{collection.get('holdout_count')}"
            )
    return errors


CHECKERS = {
    "persona_batch": check_persona_batch,
    "action_batch": check_action_batch,
    "report": check_report,
    "eval_manifest": check_eval_manifest,
}


def admit_attempt(state):
    """Pure call-count and wall-clock gate used by contract fixtures.

    A recorded wall elapsed time may be greater than the limit. This function
    only decides whether another physical attempt may start.
    """
    reasons = []
    occupied = (
        state["physical_requests"]
        + state["reserved_requests"]
        + state["additional_reserve"]
    )
    if occupied > state["request_limit"]:
        reasons.append("call_count_exhausted")
    if state["now_monotonic_ms"] >= state["deadline_monotonic_ms"]:
        reasons.append("wall_clock_exhausted")
    return {"admitted": not reasons, "reasons": reasons}


def load_example(name):
    return _read_json(EXAMPLE_DIR / name)


def run_expectations(validators=None):
    if validators is None:
        _, validators = load_schemas()
    rows = _read_json(EXAMPLE_DIR / "expectations.json")
    failures = []
    for row in rows:
        document = load_example(row["file"])
        errors = CHECKERS[row["kind"]](document, validators)
        accepted = not errors
        if row["expect"] == "accept" and not accepted:
            failures.append((row["file"], "expected accept", errors))
        elif row["expect"] == "reject" and accepted:
            failures.append((row["file"], "expected reject", []))
    return failures


def assert_no_dollar_fields():
    hits = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for marker in DOLLAR_MARKERS:
            if marker in text:
                hits.append(f"{path.name}: {marker}")
    return hits


def main():
    try:
        schemas, validators = load_schemas()
    except (SchemaError, Unresolvable, json.JSONDecodeError) as exc:
        print(f"schema parse failed: {exc}")
        return 1
    dollar_hits = assert_no_dollar_fields()
    if dollar_hits:
        print("dollar fields still present: " + ", ".join(dollar_hits))
        return 1
    failures = run_expectations(validators)
    if failures:
        for name, expected, errors in failures:
            print(f"{name}: {expected}")
            for error in errors:
                print(f"  {error}")
        return 1
    print(
        "通过："
        f"{len(schemas)} 个 schema 可解析，"
        f"{len(_read_json(EXAMPLE_DIR / 'expectations.json'))} 个示例符合预期。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
