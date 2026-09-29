# 人设批次模板 v2

宿主先冻结最多 240 个槽位。输入不含草稿、真实历史标签或其他运行的草稿。模型只返回已给出的 `agent_id`，不能增删槽位。

12 人放在一次 Grok CLI 调用里时，调用会在 120 秒的单次上限被杀掉。因此在调用次数还够留给每一波和报告预留时，每次人设调用最多 4 人。120 人 3 波放不下这种拆分，仍是一次调用。单次上限不提高。

## 宿主输入

`audience_version`、`slot_ids` 和每个槽位的公开标签。`slot_ids` 就是检查时的 `host_expected_agent_ids`。

## 模型输出

必须符合 `schemas/tweet_persona_batch.schema.json`：`schema_version` 为 `"2.0"`，`personas` 最多 240 项。每人字段仍是 `agent_id`、`display_name`、`bio`、`persona`、`avoid_speaking_when`。`persona` 为 60–120 个 Unicode 码点，显示名以“虚构”开头。

宿主在校验前写入冻结槽位列表。输出 ID 集合必须与该列表完全一致且无重复。模型如果自行附加期望 ID 列表，因额外字段被拒绝。
