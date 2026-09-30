# 人设批次模板 v2

宿主先冻结最多 240 个槽位。输入不含草稿、真实历史标签或其他运行的草稿。模型只返回已给出的 `agent_id`，不能增删槽位。

12 人放在一次 Grok CLI 调用里时，调用会在 120 秒的单次上限被杀掉。因此在调用次数还够留给每一波和报告预留时，每次人设调用最多 4 人。120 人 3 波放不下这种拆分，仍是一次调用。单次上限不提高。

4 人一批时，模型常返回一个 JSON 数组、带换行的字符串，或包在说明和围栏里，而不是一个干净的对象。宿主接受这些已经写出的条目，不为此再发一次完整的修复调用。修复会把原提示再发一遍，并且会占满 120 秒。若这次修复按 120 秒计、后面的人设调用和每一波也按 120 秒计，会挤进报告预留，宿主就不发这次修复，波次仍然发出。缺的槽位仍用短句模板，并记为降级。

人设生成和人设自己的修复都会启动进程。命令行是 `grok -p`、提示，然后 `--max-turns 1`、`--json-schema`（现有人设数组，字段仍是 `agent_id`、`display_name`、`bio`、`persona`、`avoid_speaking_when`）、`--disallowed-tools run_terminal_cmd,run_terminal_command`、`--no-subagents`、`--disable-web-search`、`--no-plan`、`--no-memory`。传给 `--json-schema` 的 display_name 不含 pattern。报告调用仍是 `grok -p` 加提示，不加这些开关。提示写明输入已完整，只依据 slots，直接返回裸 JSON 数组，人设 60 到 120 个码点、不追求正好 60，不查文件、不跑脚本、不写准备说明。原先 shape 里的「不要说明」和「例如「虚构甲」」打架，全名会留在思考里，JSON 只剩 schema 模式里的「虚构」；这句「不要说明」已去掉。display_name 改为：想好的全名必须原样写进 JSON，不能只写前缀；错的是 "虚构"，对的是 "虚构甲"。短于 60 个码点仍不垫长，短名字也不在代码里垫长。纯说明仍然无效，不编造人设。整段都锁不上时不再发第二次人设调用。已经锁上一部分时，修复仍会启动，命令行与生成相同。波次命令行不变。

## 宿主输入

`audience_version`、`slot_ids` 和每个槽位的公开标签。`slot_ids` 就是检查时的 `host_expected_agent_ids`。

## 模型输出

必须符合 `schemas/tweet_persona_batch.schema.json`：`schema_version` 为 `"2.0"`，`personas` 最多 240 项。每人字段仍是 `agent_id`、`display_name`、`bio`、`persona`、`avoid_speaking_when`。`persona` 为 60–120 个 Unicode 码点。`display_name` 的 pattern 仍是 `^虚构`，`minLength` 仍是 3，`maxLength` 仍是 30。传给 `--json-schema` 的那份拿掉了 `display_name` 的 pattern，不再把 `^虚构` 交给模型；`minLength` 3 和 `maxLength` 30 仍留在这份 CLI schema 里。本地这份 schema 文件没有改。

宿主在校验前写入冻结槽位列表。输出 ID 集合必须与该列表完全一致且无重复。模型如果自行附加期望 ID 列表，因额外字段被拒绝。
