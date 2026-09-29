# 推文预演契约 v2

本目录是 M0 落地的版本化契约，不是运行中的模拟引擎，也不会发起订阅 CLI、Ollama 或按量付费 API 请求。校验只读取本地 JSON。

在 `backend/` 执行：

```text
uv run python -m contracts.check_contracts
uv run pytest -q tests/test_m0_contracts.py
```

`jsonschema` 使用当前 `uv.lock` 里已有的传递依赖，本里程碑不新增依赖、不改锁文件。

## 文件

| 路径 | 作用 |
| --- | --- |
| `schemas/tweet_persona_batch.schema.json` | 一批人设，`schema_version` 为 `"2.0"`，数组上限 240 |
| `schemas/tweet_action.schema.json` | 原单账号动作对象，现只作为 `results[].action` |
| `schemas/tweet_action_batch_input.schema.json` | 一波非 none 候选的输入封装 |
| `schemas/tweet_action_batch.schema.json` | 一波反应输出封装 |
| `schemas/tweet_report.schema.json` | 报告。证据字段沿用旧约束，用量改为调用次数与墙钟 |
| `schemas/eval_manifest.schema.json` | 评估清单。真实推文可以尚未存在 |
| `examples/` | 合法与非法样例。`expectations.json` 记录期望的接受或拒绝 |
| `templates/` | 替换旧分批人设与单账号请求的批量模板 |
| `REPLACED.zh.md` | 被本目录替换的只读旧契约 |

人设样例里的 `host_expected_agent_ids`、动作样例里的 `input` 由宿主持有。模型输出分别只应是人设 schema 的 `output` 和动作 schema 的 `output`。宿主用冻结槽位或该波候选覆盖模型自报的 ID 后再检查，模型不能靠自己写一份 ID 列表通过覆盖校验。

动作输入的封闭字段是 `agent_id`、`persona`、`candidate_action`、`visible_timeline`、`author_context`、`draft_text`。`visible_timeline` 的每一项只有 `id`、`kind`、`text`。`candidate_action` 只能是 `like`、`reply`、`repost`、`quote`；规则产生的 `none` 不进入模型批次。空 `items` 配空 `results` 表示该波没有非 none 候选，仍然是一次批次记录。

报告 `usage` 的跨字段约束由检查器执行：`sum(channels.requests)=physical_requests`，`physical_requests+reserved_requests<=request_limit`，`uncertain_requests<=physical_requests`。墙钟实耗可以大于上限，用来留下超时记录，不能为了通过校验把实耗裁成上限。是否允许再发一次尝试由 `admit_attempt` 单独判断。
