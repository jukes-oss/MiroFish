# MiroFish 中文优先版开发计划（严格审查定稿）

版本：2026-09-29。目标仓库：`jukes-oss/MiroFish`。本文与 `prompts/` 一起替换初稿；这是实施计划与契约，没有修改或运行应用代码。所有新路径、接口和配置明确属于计划。原草稿保留不动。

## 1. 产品目标与核实边界

首个场景是「推文预演」：粘贴 X 草稿，选择中文 X 受众，运行多样化虚构账号的短程模拟，返回模拟互动档、原文触发句及群体、可能获得点赞的回复候选、反噬风险、2–3 个改写与不确定性。界面简体中文，模拟原话允许繁体、中英夹杂。互动档初版只对本次样本有意义，不能等同真实传播量；没有有效回复时可以明确显示“本次无回复”。

首版面向单用户本机使用，不直接发到 X，不需要 X 登录，不自动抓链接。保留通用五步模拟的次入口；推文预演使用轻量独立动作循环，通用模式保留 OASIS。两者复用角色模型层、费用账本、任务生命周期和本地存储，不硬套同一个模拟引擎。完整改造完成必须包含通用模式的本地图替换，不能把推文绕过 Zep 当作已经替换 Zep。

### 1.1 本次查证方式

逐文件通过 `curl` 获取上游 `main`、CAMEL/OASIS 对应版本的 raw 源码，无 clone。GitHub tree API 只调用一次，返回 HTTP 403。审查工作区里的 upstream_tree.json 记录了这次失败，不是假目录树；upstream_manifest.json 与 external_manifest.json 保存了 URL、成功/失败和 SHA-256。这三份文件都没有放进本仓库。获取的是可变分支快照，**没有取得上游固定 commit SHA**，后续 M0 必须在用户已有工作区固定基线。

抽查 fork 的 `config.py`、`pyproject.toml`、`run_parallel_simulation.py` 与此次上游文件逐字一致；当时 PR 分支上的计划与审查工作区中的初稿逐字一致。该初稿副本没有单独放进本仓库。其余 fork 文件、PR 当前 merge 状态与整仓差异**未核实**，不能推断整个 fork 与上游完全相同。

### 1.2 已核实的现状与纠错

| 项目 | 已核实结果与实施影响 | 直接证据 |
| --- | --- | --- |
| 前后端与 API | Vue 3、Vue Router、vue-i18n、Vite；Flask 应用工厂注册 graph/simulation/report 三个蓝图，健康端点 `/health`。是前后端加模拟子进程的部署，仍依赖云图谱，不能简称“没有数据库” | [前端依赖](https://raw.githubusercontent.com/666ghj/MiroFish/main/frontend/package.json)、[应用工厂](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/__init__.py) |
| 中文现状 | 默认和 fallback 已为 `zh`，语言包在根 `locales/`。主要工作是补硬编码和改产品入口，不是重做 i18n | [i18n](https://raw.githubusercontent.com/666ghj/MiroFish/main/frontend/src/i18n/index.js)、[中文包](https://raw.githubusercontent.com/666ghj/MiroFish/main/locales/zh.json) |
| 前端连后端 | Vite 有 `/api` 代理，但 Axios 默认 `http://localhost:5001`，实际默认直连；只写“开发代理可不配”会掩盖远程访问问题 | [Vite](https://raw.githubusercontent.com/666ghj/MiroFish/main/frontend/vite.config.js)、[Axios](https://raw.githubusercontent.com/666ghj/MiroFish/main/frontend/src/api/index.js) |
| 启动硬依赖 | `run.py` 调 `Config.validate()`，缺 LLM/Zep 密钥会退出；工厂函数本身不执行此验证。前端静态页能启动不等于后端全链路可用 | [启动](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/run.py)、[配置](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/config.py) |
| 存储与任务 | 项目、模拟、报告保存于 uploads；OASIS 使用 SQLite；TaskManager 是进程内字典。重启恢复、持久预算需另做 | [项目](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/models/project.py)、[任务](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/models/task.py)、[模拟管理](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/simulation_manager.py) |
| 执行脚本 | runner 按平台选择 `run_twitter_simulation.py`、`run_reddit_simulation.py` 或 `run_parallel_simulation.py`；不是总启动双平台脚本。默认配置双平台可启用 | [runner](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/simulation_runner.py)、[模拟 API](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/api/simulation.py) |
| OASIS 行动 | 三个脚本各有动作表，不能只改 Config；Twitter 启用列表无评论，Reddit 有。已核实依赖有 create_comment 方法，不等于本项目 Twitter 回复、展示、点赞链路已跑通 | [双平台脚本](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/scripts/run_parallel_simulation.py)、[OASIS 平台](https://raw.githubusercontent.com/camel-ai/oasis/v0.2.5/oasis/social_platform/platform.py) |
| 图谱与本体 | 本体提示要求 10 种实体、6–10 种关系；默认分块 500 字符；Zep 云端抽取，本地项目记录 graph_id，没有本地权威图数据库 | [本体](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/ontology_generator.py)、[建图](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/graph_builder.py)、[分块](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/text_processor.py) |
| Zep 范围 | 云图建/读/搜、实体扩展、人设上下文、行为写回、删除均依赖 Zep；固定云 URL，设置 ZEP_API_URL 被拒绝。本地替代要覆盖消费者而不只换客户端 | [Zep 工具](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/zep_tools.py)、[客户端](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/utils/zep.py)、[写回](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/zep_graph_memory_updater.py)、[删除](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/api/graph.py) |
| LLM 调用点 | 本体/报告/工具主要用 LLMClient；人设、模拟配置直接建 OpenAI 客户端；三个 runner 经 CAMEL 发行动请求，boost 仅按平台拆分，不是角色路由 | [人设](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/oasis_profile_generator.py)、[模拟配置](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/simulation_config_generator.py)、[LLMClient](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/utils/llm_client.py) |
| 密钥注入 | 脚本确实写进程级 OPENAI 环境变量；但 CAMEL 0.2.78 的 ModelFactory 已支持 `api_key`/`url`，OpenAIModel 初始化保存到实例。没有证据证明现有实例运行时一定串钥匙；应改显式注入并测试，不能再把是否支持列为未知 | [ModelFactory](https://raw.githubusercontent.com/camel-ai/camel/v0.2.78/camel/models/model_factory.py)、[OpenAIModel](https://raw.githubusercontent.com/camel-ai/camel/v0.2.78/camel/models/openai_model.py) |
| 费用隐患 | `chat_json` 内容重试会取消输出上限；兼容助手只按 `gpt-5` 前缀选择 token 参数，不能推定任意 GPT/Grok 都可直接换模型名 | [LLMClient](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/utils/llm_client.py)、[兼容层](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/utils/openai_chat_compat.py) |
| 报告 | ReACT 报告按章节取证；每章工具调用常量 5，MAX_REFLECTION_ROUNDS 常量 3 未见独立反思循环；Config 的 REPORT_AGENT 参数未被该类使用，不能当有效费用开关 | [报告](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/report_agent.py)、[IPC](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/app/services/simulation_ipc.py) |
| 行动提示词 | OASIS 系统模板英文、每轮提示鼓励不要只点赞；SocialAgent 支持自定义 user_info_template，生成 Twitter agent 的 helper 没暴露该参数。换 system 不会自动改每轮提示 | [agent](https://raw.githubusercontent.com/camel-ai/oasis/v0.2.5/oasis/social_agent/agent.py)、[生成器](https://raw.githubusercontent.com/camel-ai/oasis/v0.2.5/oasis/social_agent/agents_generator.py)、[UserInfo](https://raw.githubusercontent.com/camel-ai/oasis/v0.2.5/oasis/social_platform/config/user.py) |
| 依赖与部署 | Python >=3.11,<3.13；根 Node engine >=18；camel-oasis 0.2.5、camel-ai 0.2.78、zep-cloud 3.25.0；锁文件含 sentence-transformers/torch。compose 拉上游镜像且仅挂 uploads；Dockerfile 执行 npm run dev | [pyproject](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/pyproject.toml)、[锁文件](https://raw.githubusercontent.com/666ghj/MiroFish/main/backend/uv.lock)、[根配置](https://raw.githubusercontent.com/666ghj/MiroFish/main/package.json)、[compose](https://raw.githubusercontent.com/666ghj/MiroFish/main/docker-compose.yml)、[Dockerfile](https://raw.githubusercontent.com/666ghj/MiroFish/main/Dockerfile) |

补充核实：默认 max rounds 配置是 10，脚本可按 72 小时/步长算轮数再由传入上限截断；是否每个入口都传上限须 M0 检查实际基线。脚本 `semaphore=30` 是环境级并发值，双平台不能把它当作整个进程的总请求上限。一次 `LLMAction` 也不是可计费请求数契约，费用必须在真实出站层统计。

### 1.3 不沿用为本次事实的内容

草稿声称安装成功、130 项 pytest 通过、前端 build 成功、2 个 npm 漏洞以及具体机器版本：这是原作者自述，缺少可复现日志，本次**未核实、未重跑**，不能写成当前验收完成。本次环境 Python 为 3.13.5，不满足上游范围，因此没有尝试装完整应用。Docker、Zep 建图、OASIS 真实运行、付费模型兼容性/价格/延迟/质量、历史推文准确性全部**未实测**。README 的免费额度和“平均 5 美元/次”仅是项目文案，不能拿来定价或做性能承诺。

## 2. 实施架构与边界

### 2.1 先解耦启动和持久任务

新增 `MODE_DEFAULT=tweet`、`MEMORY_BACKEND=local|zep`（默认 local）；配置按被启用功能验证。无 Zep 密钥可启动推文及健康检查；无 LLM 密钥也能打开界面和查看历史，发起付费运行返回明确配置错误。Zep SDK 延迟导入、客户端延迟初始化，避免改了 validate 仍在 import 时失败。通用本地图尚未完成时，界面明确显示该模式不可用，不能暗中退回云端。

新建 `uploads/state.sqlite` 存 `runs, exposures, actions, provider_requests, budget_ledger, artifacts`，WAL、外键、事务、单工作队列。任务状态为 `queued → preparing → running → reporting → complete|degraded|failed|cancelled`。取消后不发新请求；已发请求仍结算。启动时回收失效 worker 租约，未知请求保留预留并记待核账；不自动重放可能收费的请求。初版一台机器、一个执行 worker，最多一个运行任务；API 可并发读取。

拟新增接口：`POST /api/tweet/runs`（幂等键，返回 202 与 run_id）、`GET /api/tweet/runs/<id>`、`POST /api/tweet/runs/<id>/cancel`、`GET /api/tweet/runs/<id>/report`、`DELETE /api/tweet/runs/<id>`。校验草稿 1–2000 个 Unicode 码点、作者背景 ≤500、受众从允许模板选择，人数/波次由服务端夹限。此长度是产品限制，不宣称等同 X 当前发布限制。重试同幂等键返回同一任务；不同输入复用同键返回 409。

### 2.2 角色模型层与供应商能力

新增 `providers/`：不可变的 `ProviderConfig`、`RoleConfig`、`CapabilityProfile`；按角色获取客户端，所有实际请求走预算网关。角色覆盖 `persona, agent, report, ontology, extractor, config, interview`；`moderator` 初版为规则，不配模型。旧全局环境配置仅作迁移兜底，并在运行前冻结解析结果；跨供应商不得混用默认密钥和另一个 base URL。

| 角色 | 推荐运行时选择 | 默认每物理请求输入 / 输出 token 上限 |
| --- | --- | --- |
| persona | OpenAI GPT 家族中通过中文短人设/schema 测试的模型；批量 20 个 | 6000 / 6000 |
| agent | xAI Grok 家族中通过动作测试且实测单次成本最低的可用模型 | 2400 / 600 |
| report | OpenAI GPT 家族中通过证据与改写 eval 的模型 | 10000 / 6000 |
| ontology / extractor / config / interview | 可先复用 persona 的模型；独立预算配置 | 按功能有界，默认不得无限输出 |

这里的运行时 API `model_id` 必须来自账户可用列表与官方能力核验，缺失则禁止开始运行。**里程碑指定的 Cursor/Codex 执行模型不自动等于可付费调用的 API 型号，也不代表其单价。**默认供应商分工是建议，可全部使用同一已验证供应商；不能因为名叫 Grok 就假定更便宜或更懂中文 X。

配置建议采用 `providers.json`（无密钥）引用环境变量 `XAI_API_KEY`、`OPENAI_API_KEY`；角色指向明确 provider/model/capability/price 版本。保留 `.env.example` 说明旧 `LLM_*`/`LLM_BOOST_*` 的迁移规则；不把密钥写入报告、URL、模拟 JSON 或前端。供应商地址只由本机管理员配置，不接受草稿里的地址。

M2 用 mock 和小额独立冒烟核实：Chat Completions、JSON/schema 支持、token 上限参数、是否接受 temperature、reasoning token 计费、usage 字段、429/超时/拒绝行为；用能力表而非模型名前缀猜测。结构化输出仍须本地校验。[OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs) 与 [xAI](https://docs.x.ai/developers/model-capabilities/text/structured-outputs) 均有相关接口说明，但账户权限和实际型号兼容性尚未实测。

通用 OASIS 路径改三份脚本及 CAMEL 适配器：显式传 key/url，拦截每个物理请求、禁用隐藏重试，覆盖内部工具轮次。若无法做到同一预算网关，通用付费模式保持不可用；不能宣传全局费用保护却给 OASIS 留旁路。Zep 云服务另有外部费用，选择 zep 时不得把 LLM 上限当作总账单保证。

### 2.3 推文循环与证据模型

采用 `prompts/` 中的固定契约。默认 120 人、3 波（40/40/40），最大 240 人、4 波；每人一次原帖曝光。大多数曝光由规则生成 none，不调用模型，默认预期约 12 次行动请求。人设分 6 批生成、固定人群可缓存。冷启动的人设费用必须包含在预估中。

核心动作 `none|like|reply|repost|quote`。独立本地循环直接支持回复及其父帖，不接受“引用转发暂代回复”的产品降级。无新帖、关注、真实发帖或无限对话；不承诺实现整个社交平台。该循环仍需事务、可见性、失败恢复、去重、预算和指标测试，不能按“一两百行”估工期。

候选动作先验与完整配额见 [audience.zh.md](prompts/audience.zh.md)，曝光/调度与档位见 [tweet_round_moderator.zh.md](prompts/tweet_round_moderator.zh.md)。先验可让模型放弃，不能让它临时改动作类型；保留内容对接受率与措辞的影响，但传播上限受到设计约束，必须在评估中披露。轮次是采样波次，不虚构为真实小时。

引用用原稿码点跨度和 action_id。报告不仅验证 ID 存在，还验证“该账号看过什么、做过什么、文本是否支持断言”。群体标签、分母、去重、计数、费用、档位由程序计算；不让报告模型自由统计。失败记 missing，未曝光不计沉默，沉默不计支持。

### 2.4 本地记忆替换 Zep（完整改造必做）

建立 `MemoryStore` 接口：`ingest, get_nodes, get_edges, search, append_events, delete_graph, export_graph`。接口 DTO 与 Zep SDK 类型解耦；节点/边/episode 带稳定 ID、来源跨度、created_at、valid_at/invalid_at（未知用 null）、schema_version。通用 API 适配器保持 GraphPanel 所需字段，不把“JSON 形状兼容”当作搜索质量等价。

SQLite 表：`graphs, documents, chunks, nodes, edges, episodes, extraction_jobs`，独立于 OASIS 平台数据库。抽取由 extractor 角色执行，验证类型/引用/实体关系端点；失败块保留原文、错误和状态，可在预算内修复一次；修复失败标 partial 并可重跑，**不能静默丢弃**。实体去重先规范化名称与类型，冲突保留多候选；不由模型随意合并同名人。写入幂等，事件 append-only；更新有效时间不覆盖原始来源。

中文搜索不能只宣称“FTS5 必须有”：默认 unicode61 不进行中文分词。采用版本化中文分词后的独立检索列 + FTS5，保留原文和偏移；简繁归一化只用于索引。trigram 可作子串辅助，但 1–2 字查询需显式 fallback 或分词索引，不可漏掉。用中文、繁体、中英混合、两字实体名测试召回。[SQLite FTS5 官方说明](https://www.sqlite.org/fts5.html)。初版暂不下载 embedding 模型；未来若加向量搜索需固定模型、许可证、下载位置、内存与离线失败策略，不能因依赖中有 sentence-transformers 就当免费成熟能力。

改动覆盖 graph_builder、实体 reader、人设上下文检索、memory updater、report tools、删除和启动路径。把 `zep_tools` 抽象成与后端无关的证据查询工具；历史报告不得将缺失字段补成云端等效事实。现有云项目保持原 `backend=zep`，切默认不自动迁移、不删除云数据；提供可选只读导出导入与计数/哈希核对，失败可回滚。只有显式删除对应项目才删除其云图。

M6 完成后，新项目默认全本地，无 ZEP_API_KEY 可走通本体→抽取→人设→OASIS→行为记忆→报告。保留 zep 适配器仅为兼容；可以后续把 zep-cloud 改 optional extra，不能在未消除导入依赖前直接删包。

### 2.5 中文界面、输入边界与部署

保留 vue-i18n，清理 MainView 的英文状态、四个 View 的 Error/Step、Step3 的平台统计文字、人设英文兜底。所有机器枚举保持英文。推文入口优先，通用模式次入口；默认报告与解释简体中文，模拟原话不强制翻译。显示受众假设、样本规模、缺失数据、互动档含义、回复证据、费用预估/实际与停止原因。

默认同源 `/api` 与显式反向代理；开发 Vite 代理，部署时静态前端与后端走同域，保留环境覆盖。不再让远程浏览器连它自己的 localhost。前端把模型文本当纯文本渲染，不用未清洗的 v-html；不执行生成链接/脚本。请求日志移除草稿正文和 Authorization，关闭 debug 请求体日志。单机默认绑定 127.0.0.1、限制 CORS；需要远程访问再加认证、任务归属、配额与数据隔离。

草稿、人设、日志、导出与缓存默认保存 7 天，可一键删除及导出；删除同步清理索引/缓存，备份按单独保留策略标明。提示用户草稿会发送到选定 LLM 供应商；“本地记忆”不等于“数据不出机器”。品牌暂保留 MiroFish，副标题“中文推文预演”，主 GitHub 链接改 fork 并保留上游致谢。

## 3. 成本与硬性费用控制

### 3.1 配置和估算公式

建议默认：`RUN_MAX_USD=1.00`、用户界面最高可选 `5.00`、日总额 `5.00`（本机管理员可改）；`RUN_MAX_TOTAL_TOKENS=150000`、`RUN_MAX_REQUESTS=80`、`AGENT_MAX_REQUESTS=60`（含全部重试）、`LLM_CONCURRENCY=4`。人数 120 / 上限 240，波次 3 / 上限 4。大规模配置可能被预算拒绝，不能保证“240 人一定能在 1 美元内完成”。美元是内部账本单位，人民币展示若无更新汇率仅显示用户设置的参考汇率。

对角色 r：`C_r = ((I_r - H_r) * P_in_r + H_r * P_cached_r + O_r * P_out_r) / 1e6 + F_r`；`I` 输入 token，`H` 是供应商确认已计入 I 的缓存 token，`O` 是全部计费输出（包含推理，避免重复加 reasoning_tokens），`F` 是其他已知费用。总费用为所有物理请求之和；没有确认缓存计费就按未缓存算。不同供应商 usage 语义由适配器统一，不能机械相加 total_tokens、completion_tokens、reasoning_tokens。

预估调用数：`ceil(N/20)` 人设批次 + `sum_i P_i(non_none)` 行动 + 1 报告；重试作为单独上界项。token 预估：`T ≈ Σ_calls (input_tokens + output_tokens)`。冷缓存与热缓存分列；本地人设缓存命中才可减人设调用，供应商缓存折扣未确认不能预扣。

**纯算术示例，不是当前模型报价或实测**：假设 persona/report 单价为输入 $2、输出 $8 / 1M，agent 为 $0.5/$2；人设 6×(3000 in+4500 out)，行动 12×(1800 in+300 out)，报告 1×(7000 in+4000 out)，则冷缓存 81,200 tokens、$0.3160，热人设缓存 36,200 tokens、$0.0640，均不含重试。实际须用当日有效 price 表重新计算；最坏输出和重试可能明显更高。

### 3.2 发请求前的费用闸门

账本用整数微美元或 Decimal，不能用 float 累计。每次请求在 SQLite 同一事务中预留费用与 token：

```text
spent + uncertain + reserved + new_upper_bound <= run_cap
并且同样满足 day_cap、token_cap、request_cap
```

`new_upper_bound` 根据该模型已验证的输入计数上界、全部计费输出硬上限和冻结单价计算；无可靠 tokenizer 时使用经验证的保守计数上界，无法建立上界则拒绝该模型。未知价格/过期报价/不支持输出硬限/存在未计价工具费用时 fail closed。禁用供应商搜索等额外工具。价格表带日期和来源，超过 7 天默认要求管理员刷新，不能擅自拿旧价格称“硬上限”。

预先圈定报告及一次修复的保守费用；运行请求不能花掉这部分。人设/行动若余额不足即停止后续请求，用剩余报告额度生成带缺失说明的报告；连报告额度都不足则程序生成 degraded JSON。使用 reserved 池子避免把报告预留再计算两遍。已发送超时、断连、取消以及进程崩溃的请求保留最大预留至核账，不当免费、不盲目重放。

账本状态互斥：预留后为 reserved，收到确定 usage 后转 charged，收费未知时将同一笔预留转 uncertain，不在两栏重复记账；后续核账只做状态转移与差额调整。每次网络尝试独立账单项；SDK/CAMEL 内部重试设为 0，由网关控制总次数 ≤2。重试维持输出上限。usage 返回后按实际结算，未返回按预留上界记 uncertain。并发请求先拿预留再出网，持久化后才提交；重启不清空账本。程序保证的是按冻结价格与已验证 token 上界的授权支出；供应商账单差错或运行外其他调用不在本地闸门控制范围，另在供应商账户设置额度。

缓存键至少包含 provider/model/capability、prompt/schema hash、受众/槽位版本、语言、seed 与输入 hash。人设批量生成可共用一个请求；行动只做 HTTP 并发或供应商的独立条目批处理，不合并私有人设。在线预演不依赖有长等待窗口的 Batch API；离线 eval 才考虑，经费用预留后提交。

## 4. 提示词、报告与评估

### 4.1 人群与沉默

完整分层配额及跨维度规则在 [audience.zh.md](prompts/audience.zh.md)。12 个主圈层覆盖技术、币圈、海外/大陆/港台、键政、女权/反女权、段子、营销、路人及机器人；关系、活跃度、语言、影响力、先验态度独立分配。配额是可配置假设，不宣称真实人口统计。人设生成不看草稿，避免为当前内容定制一群必然有反应的人。

默认候选分布 none 90%、like 7%、reply 1.5%、repost 0.8%、quote 0.7%；活动乘数调整后归一。候选 none 零调用，其余只允许执行或放弃。不能强制每轮出现反对、至少一条回复、至少两种 stance；否则制造虚假共识或虚假分歧。小样本熵只作诊断，不能作为必达数值。

### 4.2 固定 schema 和证据链

[完整 JSON Schema](prompts/tweet_report.schema.json) 定义所有字段、类型、枚举、必填、额外字段拒绝和条件约束。包含 scope/status、分母与动作统计、engagement、trigger_lines（原文跨度+群体+反应+证据）、top_replies（原话+模拟赞+被看到次数）、backlash_risk（等级+原因+群体+证据）、disagreement、2–3 rewrites（改动+预期+代价）、confidence/uncertainties 与降级原因。

宿主确定档位与风险；报告模型仅组织解释。初版 uncalibrated 置信度为 low，不输出“87%会翻车”等未经校准概率。高赞预测展示为“模拟回复候选”，零赞或曝光不可比时明确未验证。改写均标 `simulation_verified=false`；若用户主动重跑改写，创建新预算的新 run，不能偷偷多跑两轮验证改写。

本地依次做 JSON/schema/证据/统计/状态校验；一次修复仍失败则降级，同一 schema 返回、原因可见。complete 必须 2–3 个改写；degraded/failed 可少于 2，不能为了凑字段编造。模板和修复策略详见 [README](prompts/README.zh.md)、[报告模板](prompts/tweet_report.zh.md)。

### 4.3 eval harness

完整流程见 [eval.zh.md](prompts/eval.zh.md)。先搭确定性契约集，再收集 30 条带真实反响证据的授权历史推文，冻结 20 dev / 10 holdout；原文、窗口、互动数、曝光、作者基线、真实触发句与风险标签分开存储。真实标签绝不进 prompt。当前没有真实数据，不得宣称固定集已建成或预测已验证。

同输入同 seed 重跑 N=5：档位/风险众数一致率各 ≥0.8、编码样本方差 ≤0.3、不出现 low↔high；至少 80% 可判定样本达标，覆盖率 ≥90%。再用 N=10 不同 seed 报告抽样波动，不能要求同样稳定。证据和预算不变量 100%；真实对照报告 macro-F1、ordinal MAE、相关性、风险召回、触发句跨度 F1、回复主题命中和改写保意。数值阈值是拟定门禁，不是已实现能力；未达标保留实验标记，不改数据凑通过。

## 5. 按依赖排序的里程碑

执行模型是**开发任务推荐**，仅使用用户指定名单；不作模型能力/价格的实测排名。每项可独立 review，以下编号即推荐串行交付顺序。M0 同时启动真实数据收集；eval 契约从 M0 存在，不拖到末尾才设计。

| 里程碑 | 依赖 | 难度 | 推荐执行模型 | 理由 |
| --- | --- | --- | --- | --- |
| M0 基线、契约与评估骨架 | 无 | 中 | `gpt-6-sol` | 整理现有路径、可复现环境、schema 与 mock |
| M1 无 Zep 启动与持久任务 | M0 | 高 | `gpt-6-astra` high | 启动依赖、状态机、重启和幂等跨层联动 |
| M2 角色 provider 与费用网关 | M1 | 很高 | `gpt-6-astra` xhigh | 并发预留、失败账务、CAMEL 多入口覆盖 |
| M3 分层人群与推文动作循环 | M2 | 很高 | `gpt-6-astra` xhigh | 抽样偏差、沉默、可见性与证据语义耦合 |
| M4 报告、提示词与回归闭环 | M3 | 高 | `gpt-6-astra` high | schema 外的语义校验及有限证据的表达 |
| M5 中文产品页与单机体验 | M4 | 中 | `grok-4.7` | 在稳定 API 上完成常规 Vue 文案与交互 |
| M6 通用模式本地图替换 | M2；按 M5 后交付 | 很高 | `gpt-6-astra` xhigh | 抽取、中文检索、时间来源、旧数据兼容 |
| M7 历史验证、部署与合规发布 | M4、M5、M6、真实数据 | 高 | `gpt-6-astra` high | 跨链路验收、统计解释、版本与源码对应 |

### M0：冻结事实与契约

在用户已有 fork 工作区记录 commit、lockfile、实际运行命令和环境；用 Python 3.11/3.12、当前锁文件可工作的 Node 环境验证。不要把最低 Node 18 要求当成推荐长期部署版本。以 mock 测试全 none、无回复、恶意 ID、注入、预算耗尽等。引入 schema/模板/eval manifest，真实样本收集规则先定。

基线命令在已有 fork 工作区执行：根目录 `npm ci`，前端 `npm ci --prefix frontend`、`npm run build --prefix frontend`；backend 目录 `uv sync --frozen`、`uv run pytest -q`。选择满足依赖范围的 Python 后执行，锁文件不自动升级。无凭据不执行付费上游流程。

验收：后端既有测试与前端构建结果有原始日志；若失败记录基线缺陷、处理相关回归，不写死“130 个必须通过”。每条契约有合法与非法样例，schema 可解析并通过检查；源码基线 SHA 与依赖锁版本可追溯。没有密钥可完成此里程碑；上游真实云链路不作为此步阻塞项。

### M1：配置隔离、任务与本地账本基础

实现 2.1 的模式校验、延迟依赖、持久运行状态、幂等接口、取消/删除与失效任务处理，为 M2 留 budget_ledger 表。

验收：不设置 ZEP_API_KEY 且阻断 Zep 网络，应用可启动、health 200、创建 mock 推文任务可完成；缺 LLM 配置返回中文配置错误而不是进程退出。相同幂等键不建重复任务，取消后无新增模拟动作，重启仍能读取状态，失效任务转 failed/degraded 而不永久 running。输入上限服务端生效。

### M2：调用与费用不可绕过

实现角色路由、能力档案、原子预留、计费结算与冻结价格；移除无上限重试。覆盖普通 LLMClient、直接 OpenAI、三份 OASIS 脚本、工具内部调用；所有费用/日志不含 secret。

验收：两个 fake HTTP endpoint 验证不同角色路由；并发 8 个请求抢不足余额时，总已花费+预留+未知费用永不超限；无 usage、超时、崩溃重启、429 重试、报告保留额与 token 参数均有有界结果；第三次物理请求被拒。静态查找直连点并用网络替身验证无旁路。真实两供应商各一次小额 schema 冒烟单列结果，未提供凭据标“待验”，不冒充通过。

### M3：模拟循环

实现 120 槽位、多维配额、批量短人设、独立行动调用、三波公开时间线、本地 reply/quote/like、缺失与统计。接入 rule none 与受限候选，不调用主持人模型。

验收：配额精确总和、固定 seed 的规则结果可复现；10,000 次规则曝光动作频率误差 ≤1 个百分点（活动权重另测）。四账号 mock 有回复、引用、划走和缺失各一条，用于覆盖分支，**不要求真实小样本必有这些动作**。未来/不可见 ID、自赞、重复提交被拒绝；prompt 中无其他人私人信息。默认 120×3 波的 mock 成功和预算截断场景均通过。

### M4：报告与提示词闭环

实现 schema 与语义校验、确定性档位/风险、top_replies 排序、有限修复与程序降级；接入 eval CLI 的确定性层。

验收：complete 报告有 2–3 个不同保意改写；全部划走也合法完成且 top_replies 为空；假引用、错群体、quote 冒充 reply、emoji 偏移错误被拦截。失败修复一次后产生合法 degraded JSON，没有伪造补位。对固定真实或授权测试输入做 N=5 的调用实验并留记录；质量未过阈值须标实验，不能用漂亮 mock 证明预测效果。

### M5：中文可用产品

完成粘贴→预估→运行/取消→报告→证据查看/导出/删除；中文界面补漏、主/次入口和同源 API。关键状态均可从 mock 回放，不需付费点通所有页面。

验收：桌面和窄屏浏览器完成完整流程；默认中文时无未列入白名单的用户可见英文残片（模型名/字段/代码除外）。预算、缺失与实验标记始终可见；提示注入文本按纯文本显示，无 XSS；移动端不横向挤出主要报告。用实际浏览器或 E2E 渲染检查，不能用 vite build 代替 UI 验收。

### M6：本地图完整替代

实现 2.4 全部接口与消费者适配，保留云后端只用于旧项目。中文抽取与来源、FTS 检索、删除及恢复作为主要风险处理。

验收：阻断 Zep 域名且不提供其 key，1–2 KB 中文夹具走完整通用链路（mock LLM）并生成可追溯报告；抽取失败块可见且能重试。已标注 20 条中/繁/混合检索查询 recall@5 ≥0.9，包含两字实体与时间失效事实过滤。并发读写不丢事件；删除同时移除该图索引/缓存；旧云项目不被默认切换或误删。Zep mock 契约保留。再用真实模型跑 1 个小夹具验证抽取质量，费用受 M2 约束；失败则不宣称本地替代完成。

### M7：历史验证与可分发版本

完成真实数据集、冻结版本历史对照、两供应商冒烟、单机部署文档、来源和许可证入口。构建自己的镜像或本地可复现运行产物，不能用上游 latest 冒充 fork。

验收：确定性硬门禁全过；真实 eval 输出指标/分子分母/区间和基线对比，达不到质量目标时只发布明确的实验备忘功能，不使用预测营销。独立 holdout 不参与调参。首次安装不需 Zep，可完成默认预演并查看/删除历史。发布工件包含可对应此版本的完整源码、构建安装脚本、LICENSE 和修改记录；对外服务有明显源码获取入口。没有真实数据/凭据时 M7 标未完成，不能勾成 done。

## 6. AGPL、数据和发行边界

仓库 [LICENSE](https://raw.githubusercontent.com/666ghj/MiroFish/main/LICENSE) 是 AGPLv3 文本。私下运行和修改不自动要求公开给所有人；分发修改版须遵守相应源码、声明与许可证要求；修改版提供远程网络交互时，应向这些用户显著提供该版本 Corresponding Source 的免费获取方式（第 13 条），不能只链接未修改上游。修改说明、构建脚本和必要安装材料纳入版本源码。AGPL 并不一概禁止收费或商业托管。[GNU 原文](https://www.gnu.org/licenses/agpl-3.0.txt)。

软件许可证不自动取得历史推文、头像、商标或模型输出的再分发权。真实 eval 原始数据可以留在私有本地；公开仓库用合法夹具。保留上游版权/许可证与致谢；模型供应商条款、依赖许可证和品牌权利另列清单。远程开放前另做认证隔离与配额，不把“有 AGPL 源码链接”当作安全部署完成。

## 7. 需要用户拍板的默认方案

下表合并草稿第 6 节**实际 9 条**与本次新增事项；未回应不阻塞本计划定稿，实施默认按推荐值推进，涉及真实密钥/数据的验收仍如实待验。相同清单附于 REVIEW_NOTES 末尾。

| ID | 决策 | 推荐默认值 | 理由 |
| --- | --- | --- | --- |
| D1 | 运行时供应商与模型 | persona/report 用 OpenAI GPT，agent 用 xAI Grok；API 型号按账户可用性、费用表与 eval 锁定；规则 moderator | 角色可替换，避免把开发执行模型名当已验证 API 或价格 |
| D2 | 人数、轮次与费用 | 120 人/3 波，上限 240/4；$1/次、$5/日，UI 单次最高 $5 | 增加沉默样本而不线性增加行动调用；硬闸门优先 |
| D3 | 回复能力与引擎 | 独立本地动作循环，首版真 reply；通用模式保留 OASIS | 避免引用代回复和依赖平台行为的不确定性 |
| D4 | 受众与先验 | `zh_x_v1` 多圈层配额；90% none 候选先验；非真实人口分布 | 默认可审计，后续按历史数据校准 |
| D5 | 产品范围 | 推文入口优先，保留通用次入口；其他模式暂不实现 | 保持聚焦，避免提前做泛化插件系统 |
| D6 | 本地记忆与旧数据 | SQLite + 中文分词 FTS5；无向量模型；旧 Zep 图不自动迁移/删除 | 降低服务复杂度并避免不可逆数据损失 |
| D7 | 品牌和链接 | 保留 MiroFish，副标题中文推文预演，主链接改 fork，保留上游致谢 | 先完成产品差异，减少品牌改造范围 |
| D8 | 历史评估数据 | 用户自有/获授权 30 条，20 dev/10 holdout，固定窗口与来源证据 | 能对照真实结果且不靠爬虫或记忆编标签 |
| D9 | 使用方式与 AGPL | 先本机单用户；对外开放前完成认证隔离和对应源码入口 | 控制部署范围并落实网络交互义务 |
| D10 | 预测承诺与失败降级 | 未校准时仅模拟档/风险备忘，允许无回复；失败用同 schema degraded | 不把小样本、缺失数据写成确定预测 |
| D11 | 数据保留与出站 | 本地 7 天可删除；仅发送到选定 LLM，关闭正文日志与自动抓链 | 草稿可能敏感，本地记忆仍有模型出站 |
| D12 | 多样性与一致意见 | 保留配额和私有上下文，不强制反对、不强制发言 | 避免为了防趋同反而制造虚假分歧 |
| D13 | 模型/价格变更 | 显式版本、价格 7 天刷新、能力探测与回归；禁止静默 fallback | 质量和费用都依赖具体版本 |
