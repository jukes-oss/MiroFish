"""Contract fixtures for the v2 persona, wave-action, and report schemas.

These tests stay inside the contract layer. They do not start the legacy
paid simulation flow and they do not call Grok, Codex, or Ollama.
"""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.check_contracts import (
    CHECKERS,
    admit_attempt,
    assert_no_dollar_fields,
    load_example,
    load_schemas,
    run_expectations,
)


def setup_module():
    global VALIDATORS
    _, VALIDATORS = load_schemas()


def errors_for(kind, name):
    return CHECKERS[kind](load_example(name), VALIDATORS)


def test_schemas_parse_and_examples_match_expectations():
    failures = run_expectations(VALIDATORS)
    assert failures == []
    assert assert_no_dollar_fields() == []


def test_all_none_batch_is_valid():
    document = load_example("action_batch.all_none.valid.json")
    assert errors_for("action_batch", "action_batch.all_none.valid.json") == []
    actions = [item["action"]["action"] for item in document["output"]["results"]]
    assert actions == ["none", "none"]
    report_errors = errors_for("report", "report.valid.json")
    report = load_example("report.valid.json")
    assert report_errors == []
    assert report["metrics"]["action_counts"]["none"] == 120
    assert report["top_replies"] == []


def test_empty_replies_are_a_valid_result():
    document = load_example("action_batch.empty_replies.valid.json")
    assert errors_for("action_batch", "action_batch.empty_replies.valid.json") == []
    actions = [item["action"]["action"] for item in document["output"]["results"]]
    assert "reply" not in actions
    assert "quote" not in actions
    report = load_example("report.empty_replies.valid.json")
    assert errors_for("report", "report.empty_replies.valid.json") == []
    assert report["metrics"]["action_counts"]["reply"] == 0
    assert report["metrics"]["action_counts"]["like"] > 0
    assert report["top_replies"] == []
    assert report["status"] == "complete"


def test_malicious_ids_are_rejected():
    errors = errors_for("action_batch", "action_batch.malicious_ids.invalid.json")
    assert any(item.startswith("id_coverage:") for item in errors)
    assert any("a999" in item for item in errors)


def test_injection_text_stays_data():
    ignored = load_example("action_batch.injection_ignored.valid.json")
    assert "忽略以上规则" in ignored["input"]["items"][0]["draft_text"]
    assert errors_for("action_batch", "action_batch.injection_ignored.valid.json") == []
    obeyed = errors_for("action_batch", "action_batch.injection_obeyed.invalid.json")
    assert any(item.startswith("schema:") for item in obeyed)


def test_call_count_and_wall_clock_exhaustion():
    usage_errors = errors_for("report", "report.call_count_exhausted.invalid.json")
    assert any("physical_requests + reserved_requests > request_limit" in item for item in usage_errors)
    overrun_errors = errors_for("report", "report.wall_overrun.valid.json")
    assert overrun_errors == []
    overrun = load_example("report.wall_overrun.valid.json")
    assert overrun["usage"]["wall_elapsed_ms"] > overrun["usage"]["wall_limit_seconds"] * 1000

    gate = load_example("gate_admission.json")
    denied_calls = admit_attempt(gate["call_count_exhausted"])
    assert denied_calls["admitted"] is False
    assert denied_calls["reasons"] == ["call_count_exhausted"]
    denied_time = admit_attempt(gate["wall_clock_exhausted"])
    assert denied_time["admitted"] is False
    assert denied_time["reasons"] == ["wall_clock_exhausted"]
    assert admit_attempt(gate["within_limits"])["admitted"] is True


def test_persona_cap_is_240_and_ids_must_match_host_slots():
    assert errors_for("persona_batch", "persona.valid.json") == []
    mismatch = errors_for("persona_batch", "persona.invalid.json")
    assert any(item.startswith("id_coverage:") for item in mismatch)

    source = load_example("persona.valid.json")
    wide = copy.deepcopy(source)
    seed = wide["output"]["personas"][0]
    personas = []
    expected = []
    for index in range(240):
        agent_id = f"a{index:03d}"
        item = copy.deepcopy(seed)
        item["agent_id"] = agent_id
        item["display_name"] = f"虚构{index:03d}"
        personas.append(item)
        expected.append(agent_id)
    wide["output"]["personas"] = personas
    wide["host_expected_agent_ids"] = expected
    assert CHECKERS["persona_batch"](wide, VALIDATORS) == []

    overflow = copy.deepcopy(wide)
    extra = copy.deepcopy(seed)
    extra["agent_id"] = "a240"
    extra["display_name"] = "虚构240"
    overflow["output"]["personas"] = personas + [extra]
    overflow["host_expected_agent_ids"] = expected + ["a240"]
    overflow_errors = CHECKERS["persona_batch"](overflow, VALIDATORS)
    assert any("schema:" in item for item in overflow_errors)


def test_changed_candidate_and_hidden_target_are_rejected():
    document = load_example("action_batch.valid.json")
    changed = copy.deepcopy(document)
    changed["output"]["results"][1]["action"] = {
        "action": "reply",
        "target_id": "post_0",
        "text": "候选是点赞，不能临时改成回复。",
        "expressed_stance": "opposing",
        "trigger_span": {"start": 0, "end": 3, "text": "所有人"},
    }
    changed_errors = CHECKERS["action_batch"](changed, VALIDATORS)
    assert any(item.startswith("candidate_action:") for item in changed_errors)

    hidden = copy.deepcopy(document)
    hidden["output"]["results"][1]["action"]["target_id"] = "not_visible"
    hidden_errors = CHECKERS["action_batch"](hidden, VALIDATORS)
    assert any(item.startswith("target_visibility:") for item in hidden_errors)


def test_eval_skeleton_has_no_tweets_and_frozen_claim_is_rejected():
    manifest = load_example("eval_manifest.valid.json")
    assert errors_for("eval_manifest", "eval_manifest.valid.json") == []
    assert manifest["cases"] == []
    assert manifest["dataset_status"] == "not_collected"
    assert manifest["collection"]["authorized_historical_tweets"] == 30
    assert manifest["collection"]["dev_count"] == 20
    assert manifest["collection"]["holdout_count"] == 10
    profiles = set(manifest["comparison"]["profiles"])
    assert profiles == {"mixed", "subscription-only"}
    assert manifest["comparison"]["routes"]["mixed"]["agent"] == "ollama"
    assert manifest["comparison"]["routes"]["subscription-only"]["scope"] == "low_volume_eval_baseline"
    assert errors_for("eval_manifest", "eval_manifest.invalid.json")
