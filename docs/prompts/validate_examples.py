"""校验本目录的契约及合成示例；不代表应用或真实模型已通过验收。"""
import copy
import json
from pathlib import Path
from collections import Counter
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent

def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))

def semantic_report(report, data):
    """对示例检验证据、分母、点赞与可见性，不承担自然语言蕴含判断。"""
    draft = data["draft_text"]
    events = data["events"]
    by_id = {e["action_id"]: e for e in events}
    assert len(by_id) == len(events), "事件 ID 重复"
    assert len({e["exposure_id"] for e in events}) == len(events), "曝光重复"
    completed = [e for e in events if e["outcome"] == "completed"]
    counts = Counter(e["action"] for e in completed)
    m = report["metrics"]
    assert m["exposed_agents"] == len({e["agent_id"] for e in events})
    assert m["evaluated_agents"] == len(completed)
    assert m["missing_agents"] == len(events) - len(completed)
    assert m["action_counts"] == {k: counts[k] for k in m["action_counts"]}
    root_engaged = sum(e["action"] != "none" and e["target_id"] == "post_0" for e in completed)
    assert m["root_engaged_agents"] == root_engaged
    assert m["engagement_rate"] == root_engaged / len(completed)
    assert m["none_rate"] == counts["none"] / len(completed)
    def span_ok(span):
        assert 0 <= span["start"] < span["end"] <= len(draft)
        assert draft[span["start"]:span["end"]] == span["text"], "原文跨度不匹配"
    def evidence(ids, groups):
        assert all(i in by_id and by_id[i]["outcome"] == "completed" for i in ids)
        actual = {g for i in ids for g in by_id[i]["group_ids"]}
        assert set(groups) == actual, "群体与证据不匹配"
    for e in completed:
        if e["action"] != "none":
            assert e["target_id"] in e["visible_ids"], "目标不可见"
        if e["trigger_span"]:
            span_ok(e["trigger_span"])
    for t in report["trigger_lines"]:
        span_ok(t["span"])
        evidence(t["evidence_ids"], t["triggered_groups"])
        assert all(by_id[i]["trigger_span"] == t["span"] for i in t["evidence_ids"])
    for r in report["top_replies"]:
        e = by_id[r["action_id"]]
        assert e["action"] == "reply", "引用不能冒充回复"
        assert e["text"] == r["text"] and e["agent_id"] == r["agent_id"]
        assert e["group_ids"] == r["group_ids"]
        shown = {x["agent_id"] for x in events if e["action_id"] in x["visible_ids"]}
        likes = {x["agent_id"] for x in completed if x["action"] == "like" and x["target_id"] == e["action_id"] and x["agent_id"] != e["agent_id"]}
        assert likes <= shown
        assert r["shown_to"] == len(shown) and r["simulated_likes"] == len(likes)
    for issue in report["backlash_risk"]["issues"]:
        evidence(issue["evidence_ids"], issue["triggered_groups"])
        for sp in issue["trigger_spans"]:
            span_ok(sp)
        assert all(by_id[i]["action"] in {"reply", "quote"} and by_id[i]["expressed_stance"] == "opposing" for i in issue["evidence_ids"])
    for view in report["disagreement"]["views"]:
        evidence(view["evidence_ids"], view["group_ids"])
    for rewrite in report["rewrites"]:
        for sp in rewrite["changed_spans"]:
            span_ok(sp)


def main():
    validators = {}
    for kind in ("persona", "action", "report"):
        schema = read(f"tweet_{kind}.schema.json")
        Draft202012Validator.check_schema(schema)
        validators[kind] = Draft202012Validator(schema)
    samples = ["action.none", "action.reply", "report.complete", "report.degraded", "report.evidence"]
    for sample in samples:
        validators[sample.split(".")[0]].validate(read(f"examples/{sample}.json"))
    report = read("examples/report.evidence.json")
    data = read("examples/report.evidence.input.json")
    semantic_report(report, data)
    negatives = []
    x = read("examples/report.complete.json"); x["rewrites"] = x["rewrites"][:1]; negatives.append(("report", x))
    x = read("examples/report.degraded.json"); x["degradation_reasons"] = []; negatives.append(("report", x))
    x = read("examples/report.complete.json"); x["invented_field"] = True; negatives.append(("report", x))
    x = read("examples/report.complete.json"); x["confidence"]["level"] = "medium"; negatives.append(("report", x))
    x = read("examples/action.none.json"); x["text"] = "凭空出现的回复"; negatives.append(("action", x))
    x = read("examples/action.reply.json"); x["action"] = "create_post"; negatives.append(("action", x))
    for kind, bad in negatives:
        assert list(validators[kind].iter_errors(bad)), "非法示例未被拒绝"
    semantic_negatives = []
    x = copy.deepcopy(report); x["trigger_lines"][0]["evidence_ids"] = ["missing_id"]; semantic_negatives.append(x)
    x = copy.deepcopy(report); x["trigger_lines"][0]["span"]["end"] = 2; semantic_negatives.append(x)
    x = copy.deepcopy(report); x["top_replies"][0]["action_id"] = "q001"; semantic_negatives.append(x)
    x = copy.deepcopy(report); x["top_replies"][0]["simulated_likes"] = 999; semantic_negatives.append(x)
    x = copy.deepcopy(report); x["metrics"]["action_counts"]["none"] = 120; semantic_negatives.append(x)
    for bad in semantic_negatives:
        try:
            semantic_report(bad, data)
        except (AssertionError, KeyError):
            pass
        else:
            raise AssertionError("非法证据示例未被拒绝")
    print("通过：3 个 schema、5 个合法示例、6 个结构反例、5 个证据反例及完整合成行为计数。")

if __name__ == "__main__":
    main()
