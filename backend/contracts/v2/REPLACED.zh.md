# 被 v2 替换的只读契约

下列文件是只读参考，位于文档 PR 分支 `cursor/dev-plan-zh-dabd` 的 `docs/prompts/`。本里程碑不修改该分支，也不把旧样例的通过结果当成 v2 通过。旧文件里没有被本节点名的证据字段、动作枚举和受众配额仍然保留在新 schema 中。

| 只读契约 | 被替换的条款 | v2 位置 |
| --- | --- | --- |
| `docs/prompts/tweet_persona.schema.json` | `personas.maxItems` 为 20，没有一批覆盖全部槽位的要求 | `schemas/tweet_persona_batch.schema.json`：`maxItems` 240，检查器用宿主槽位做精确 ID 覆盖 |
| `docs/prompts/audience.defaults.json` | `persona_batch_size` 为 20 | 不再作为分批宽度。人数上限仍是 240，由人设数组上限表达 |
| `docs/prompts/tweet_persona.zh.md` | 每批最多 20 人、默认 6 批 | `templates/persona_batch.zh.md`：调用次数够用时每次最多 4 人；数组和带换行的 CLI 文本可以收下；修复若会挤掉波次就不发；120 人 3 波仍是一次调用；人设命令行是单轮并摘掉终端 |
| `docs/prompts/tweet_action.schema.json` | 作为一次只含一个账号的模型响应外层 | 同名字段保留为 `schemas/tweet_action.schema.json`，只嵌在 `results[].action` |
| `docs/prompts/tweet_agent_action.zh.md` | 一请求一账号、HTTP 并发 4、禁止合并私有人设、默认 600 token 输出上限 | `templates/action_wave_batch.zh.md` 与波次输入/输出 schema。共享一次上下文，不再声称物理隔离 |
| `docs/prompts/tweet_report.schema.json` 的 `usage` | `currency`、全部 `*_microusd`、`billable_tokens`、`price_version`、`cost_basis` | `schemas/tweet_report.schema.json` 的调用次数与墙钟 `usage`。不把这些美元字段填 0 留作假账 |
| `docs/prompts/README.zh.md` 的费用结算段，以及 `docs/prompts/eval.zh.md` 的 `--budget-usd`、价格版本和费用分位数 | 美元闸门与费用回归 | `schemas/eval_manifest.schema.json`：同一模拟循环上对照 `mixed` 与低量 `subscription-only`，上限是调用次数和墙钟 |

`docs/prompts/tweet_report.schema.json` 里除 `usage` 和 `schema_version` 以外的证据、状态和条件约束按原结构保留，`schema_version` 改为字符串 `"2.0"`。`docs/prompts/eval.zh.md` 里 30 条授权历史推文、20 条 dev、10 条 holdout 的收集规则写入新的评估清单，但清单不要求这些推文已经存在。
