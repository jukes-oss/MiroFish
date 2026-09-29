# 报告用量模板 v2

报告正文仍使用固定证据字段。宿主在报告和修复结束之后写入最终 `usage`，再按 `schemas/tweet_report.schema.json` 重新校验。模型不计算调用次数、墙钟或通道统计。

`usage` 只含 `execution_profile`、`request_limit`、`physical_requests`、`reserved_requests`、`uncertain_requests`、`wall_limit_seconds`、`wall_elapsed_ms` 和 `channels`。没有美元、单价或计费 token 字段。`execution_profile` 为 `mixed` 或 `subscription-only`。通道只记录 `grok_cli`、`codex_cli`、`ollama`。

`physical_requests` 包含已发送但结果未知的尝试。`reserved_requests` 只计尚未发送的占位。两者之和不能超过 `request_limit`，也不能把未知尝试再加一次。`wall_elapsed_ms` 是运行实耗；超过 `wall_limit_seconds` 时照实保存。
