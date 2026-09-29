# MiroFish 中文版改造计划（推文预演优先）

本文只做摸底和方案，不改应用代码。后续实现以本仓库 `jukes-oss/MiroFish` 的 `main` 为基线，不要把改动提交回上游 `666ghj/MiroFish`。

许可证是 AGPL-3.0（见根目录 `LICENSE`）。如果以后把改过的程序提供给别人用，需要按 AGPL 保留协议和对应源码。这不是法律意见，只是提醒后续分发时不要把它当成私有闭源服务。

总目标：把这个 fork 收成中文优先的个人版本，第一个能用的场景是「推文预演」。用户贴上一则 X/Twitter 草稿（中文或英文），系统生成一小组立场不同的模拟用户，让他们划走、点赞、转发、引用或回复，然后给出一份固定格式的发布前备忘：热度档、哪几句会惹人、可能被顶上来的回复、反噬风险、两三条改写。后面还想做游戏购买反应、播客选题、故事续写，所以这次要把「模式」留成可插拔的，不要把推文逻辑写死在唯一入口里。

难度记号：S 是局部改动，M 是要动一条链路，L 是跨前后端、还要替换外部服务或改模拟循环。

---

## 1. 现状摸底

### 1.1 它是怎么拼起来的

这是一个单体应用，没有独立数据库服务。

- 前端：Vue 3 + Vue Router + vue-i18n + Vite，入口在 `frontend/src/main.js`，路由在 `frontend/src/router/index.js`。开发时 Vite 把 `/api` 代理到 `http://localhost:5001`（`frontend/vite.config.js`）。
- 后端：Flask 应用工厂 `backend/app/__init__.py`。启动脚本是 `backend/run.py`，默认监听 `0.0.0.0:5001`。
- 三个 API 蓝图：
  - `/api/graph`：上传种子、生成本体、建图谱（`backend/app/api/graph.py`）
  - `/api/simulation`：建模拟、生成人设、开跑、采访（`backend/app/api/simulation.py`）
  - `/api/report`：写报告、和报告助手聊天（`backend/app/api/report.py`）
- 状态几乎都是本地文件，放在 `backend/uploads/`：
  - 项目：`uploads/projects/`（`backend/app/models/project.py` 的 `ProjectManager`）
  - 模拟：`uploads/simulations/`（`backend/app/services/simulation_manager.py`）
  - 报告：`uploads/reports/`（`backend/app/services/report_agent.py` 里的 `ReportManager`）
  - OASIS 自己还有两个 SQLite：`twitter_simulation.db`、`reddit_simulation.db`
- 任务进度（本体、建图、准备模拟）存在内存里的 `TaskManager`（`backend/app/models/task.py`）。进程一重启，进行中的任务状态就没了，虽然项目 JSON 还在。
- 社交模拟不在 Flask 进程里跑。`backend/app/services/simulation_runner.py` 用子进程启动 `backend/scripts/run_parallel_simulation.py`。这个脚本同时跑 Twitter 和 Reddit 两套 OASIS 环境。
- 界面是五步向导：首页上传 → 图谱构建 → 环境搭建 → 开始模拟 → 报告 → 深度互动。对应 `frontend/src/views/Home.vue`、`MainView.vue`，以及 `Step1` 到 `Step5` 组件。模拟跑起来之后会跳到 `/simulation/:id`、`/report/:id`、`/interaction/:id`。

依赖里真正重的是 `camel-oasis==0.2.5` 和 `camel-ai==0.2.78`（`backend/pyproject.toml`）。它们会带上 PyTorch 和 sentence-transformers。图谱记忆则是付费 SaaS：`zep-cloud==3.25.0`。

### 1.2 一次完整模拟怎么走

下面是现在的主路径。推文预演以后不应该被迫走完全程，但通用模式还要留着。

1. **种子**。首页用自然语言写「模拟需求」，并上传 PDF / MD / TXT。`POST /api/graph/ontology/generate` 把文件存进项目目录，抽出纯文本。允许的扩展名在 `backend/app/config.py` 的 `ALLOWED_EXTENSIONS`。
2. **本体**。`OntologyGenerator`（`backend/app/services/ontology_generator.py`）让大模型设计正好 10 种实体、6 到 10 种关系。类型名强制英文 PascalCase，因为后面要交给 Zep。
3. **建图**。`POST /api/graph/build` 调用 `GraphBuilderService`（`backend/app/services/graph_builder.py`）。文本按约 500 字切块（`backend/app/services/text_processor.py`），成批送给 Zep Cloud。Zep 在云端做实体和关系抽取。本地只保存 `graph_id`，图本身不在本地。
4. **建模拟壳**。`POST /api/simulation/create` 记下 `project_id`、`graph_id`，以及是否开启 Twitter / Reddit。默认两个都开。
5. **准备环境**。`POST /api/simulation/prepare` 做三件费钱的事：
   - `ZepEntityReader` 把云端节点读回来（`backend/app/services/zep_entity_reader.py`）
   - `OasisProfileGenerator` 给每个实体写一份人设（`backend/app/services/oasis_profile_generator.py`）。个人和机构用不同提示词，人设要求约 2000 字
   - `SimulationConfigGenerator` 再让模型生成时间表、初始帖、每个 agent 的活跃度和立场（`backend/app/services/simulation_config_generator.py`）
6. **开跑**。子进程里 `generate_twitter_agent_graph` / `generate_reddit_agent_graph` 读人设文件。每一轮先按作息挑出「醒着」的 agent（`get_active_agents_for_round`），再对他们执行 `LLMAction()`，也就是每人至少一次大模型调用。初始帖用 `ManualAction` 直接写入，不经过模型。
7. **边跑边写回 Zep**。`ZepGraphMemoryUpdater`（`backend/app/services/zep_graph_memory_updater.py`）盯着动作日志，把行为拼成自然语言，再 `graph.add` 回 Zep。报告阶段才能「搜到模拟中发生的事」。
8. **报告**。`ReportAgent`（`backend/app/services/report_agent.py`）先规划 2 到 5 个章节，再按 ReACT 循环调用 Zep 工具：`insight_forge`、`panorama_search`、`quick_search`、`interview_agents`（实现在 `backend/app/services/zep_tools.py`）。采访会通过 IPC 打进还没关闭的 OASIS 进程（`backend/app/services/simulation_ipc.py`）。
9. **互动**。模拟环境可以留着，前端继续单人采访；报告助手也可以再搜一次图。

Twitter 侧启用的动作在 `backend/app/config.py` 的 `OASIS_TWITTER_ACTIONS`：发帖、点赞、转发、关注、什么都不做、引用。没有「回复」。Reddit 才有评论。OASIS 0.2.5 的 `ActionType` 里虽然有 `CREATE_COMMENT`，但本仓库的 Twitter 列表没启用它。推文预演如果要「回复」，要么改用引用，要么先做一次小实验确认 Twitter 平台的 SQLite 能写评论。这是后面 M5 的风险，不是现在就能当成已支持的功能。

### 1.3 大模型是在哪里被调用的

已经有一个 OpenAI 兼容客户端 `backend/app/utils/llm_client.py`。密钥和地址来自环境变量，不是写死的某一家。兼容层在 `backend/app/utils/openai_chat_compat.py`（处理 `response_format` 不被支持、以及思考模型把内容放在别的字段里的情况）。

但是「一个客户端」不等于「按角色选模型」：

| 调用点 | 用的客户端 | 现在的模型从哪来 |
| --- | --- | --- |
| 本体、报告、Zep 工具里的子问题/采访策划 | `LLMClient` | 全局 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME` |
| 人设、模拟配置 | 各自又 `new` 了一个 `OpenAI()` | 同一组全局变量 |
| OASIS 里每个 agent 的行动 | camel 的 `ModelFactory`（`backend/scripts/run_parallel_simulation.py` 的 `create_model`） | Twitter 用全局模型；Reddit 如果设了 `LLM_BOOST_*` 就用加速那一套 |

`create_model` 会把密钥写进进程级环境变量 `OPENAI_API_KEY` 和 `OPENAI_API_BASE_URL`。双平台在同一个进程里跑，这样换角色密钥有互相覆盖的风险。M2 应该把密钥交到模型对象上，不要靠改全局环境变量。

现有的「加速模型」只是为了让 Reddit 和 Twitter 打到不同供应商，提高并发，不是「贵模型写报告、便宜模型跑群演」。

费用开关今天很弱：

- `OASIS_DEFAULT_MAX_ROUNDS` 默认 10（`backend/app/config.py`）。时间配置却常常生成 72 小时、每轮 60 分钟，也就是 72 轮，再被 max_rounds 截断。
- 每轮醒着的人数由模型生成的 `agents_per_hour_min/max` 决定，上限大约是实体数的九成。实体数等于图谱里抽出来的人数，没有单独的「最多 30 个 agent」开关。
- 人设提示词要求约 2000 字，而且 Twitter 侧会把 `bio + persona` 整段塞进 `user_char`。OASIS 每轮都把这段放进系统提示词。这是单次模拟变贵的主要原因。
- OASIS 环境的并发信号量是 30（`run_parallel_simulation.py` 里 `oasis.make(..., semaphore=30)`）。
- `config.py` 里的 `REPORT_AGENT_MAX_TOOL_CALLS`、`REPORT_AGENT_MAX_REFLECTION_ROUNDS`、`REPORT_AGENT_TEMPERATURE` 没有被 `ReportAgent` 读到。类里写死了每章最多 5 次工具调用。`MAX_REFLECTION_ROUNDS` 只有常量，没有反思循环。

语言：`backend/app/utils/locale.py` 的 `get_language_instruction()` 会把「请使用中文回答」贴到很多提示词后面。前端用 `Accept-Language` 传当前界面语言（`frontend/src/api/index.js`）。默认语言已经是中文。

### 1.4 Zep 用在哪

Zep 不是可选缓存，而是图的唯一存储。客户端在 `backend/app/utils/zep.py`，基址写死为 `https://api.getzep.com/api/v2`。如果环境里出现 `ZEP_API_URL`，配置校验和客户端都会直接报错，明确不支持自建地址。

主要用途：

- 按本体建图、等待云端抽完实体（`graph_builder.py`）
- 读节点和边，供人设与前端画图（`zep_entity_reader.py`、`zep_paging.py`）
- 模拟进行中把行为写回图（`zep_graph_memory_updater.py`）
- 报告工具做语义搜索、全量边、采访人选（`zep_tools.py`）
- 删除项目时删云端图（`backend/app/api/graph.py` 的 `_delete_cloud_graph_if_present`）

本地已经有的 SQLite 只属于 OASIS 的帖子表，不能代替 Zep 的实体图。

### 1.5 外部依赖和费用

| 依赖 | 是否必须 | 费用 | 说明 |
| --- | --- | --- | --- |
| 任意 OpenAI 兼容对话接口 | 是 | 按 token。界面文案写「常规模拟平均 5 美元/次」（`locales/zh.json` 的 `home.metricLowCostDesc`），这是原项目的估计，不是这次实测 | 示例配置指向阿里百炼 `qwen-plus`。换 Grok 或 GPT 只改 base URL 和模型名即可，前提是对方兼容 Chat Completions |
| Zep Cloud | 今天是必须 | 按他们的套餐和额度。README 写每月免费额度够简单试用。本仓库没有写单价 | 建图的每个文本块、以及模拟中每批行为，都会变成云端 episode。抽取用的是 Zep 自己的模型，额度另算 |
| OASIS / CAMEL | 是（Python 包） | 无单独授权费；贵在上面的大模型调用 | 版本钉死在 `camel-oasis==0.2.5` |
| Docker 镜像 `ghcr.io/666ghj/mirofish:latest` | 只在用上游镜像时 | 拉取免费 | `docker-compose.yml` 用的是上游镜像，不是本 fork 现编的镜像 |
| 其他 | 否 | | 没有数据库、没有 Redis、没有登录 |

没有做这次实测的事：没有注册 Zep，没有打任何付费模型，所以不能给出「跑一则推文要多少钱」的实测数字。第 5 节给了估算口径，等 M2 加上计数后再填真实值。

### 1.6 本地要怎么跑，需要哪些变量

根目录 `.env.example`：

```env
LLM_API_KEY=...
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL_NAME=qwen-plus
ZEP_API_KEY=...
# 可选。不使用就不要留下这些行
LLM_BOOST_API_KEY=...
LLM_BOOST_BASE_URL=...
LLM_BOOST_MODEL_NAME=...
```

`backend/run.py` 在启动时调用 `Config.validate()`。缺 `LLM_API_KEY` 或缺 `ZEP_API_KEY`，或者设置了 `ZEP_API_URL`，进程直接退出。所以没有这两把钥匙时，完整界面起不来。

其他会读到的变量：

- `FLASK_HOST`、`FLASK_PORT`（默认 5001）、`FLASK_DEBUG`、`SECRET_KEY`
- `OASIS_DEFAULT_MAX_ROUNDS`（默认 10）
- `VITE_API_BASE_URL`（前端直连后端时用；开发代理可以不设）
- 上面那三个 `REPORT_AGENT_*` 目前写了但没被报告代码读

Python 要求 `>=3.11,<3.13`。Node 要求 `>=18`。包管理：前端 npm，后端 uv（`npm run setup:all` 会两边都装）。

源码跑法（README-ZH.md）：

```bash
cp .env.example .env   # 填入真实钥匙后再启动
npm run setup:all
npm run dev            # 前端 3000，后端 5001
```

Docker：

```bash
cp .env.example .env
docker compose up -d
```

注意两点：

- `docker-compose.yml` 的镜像是 `ghcr.io/666ghj/mirofish:latest`，跑的是上游，不是这个 fork 的工作区。要跑 fork，得用根目录 `Dockerfile` 自己构建。该 Dockerfile 最后执行的是 `npm run dev`，属于开发服务器，不是生产构建。
- compose 只把 `./backend/uploads` 挂进容器。密钥来自 `.env`，不要把 `.env` 提交进 git。

### 1.7 这次在环境里实际跑过什么

机器上的版本：Python 3.12.3，Node v22.14.0，npm 10.9.7。开始时没有 `uv`，也没有 `docker`。为了装后端依赖，临时安装了 uv 0.12.20。没有写 `.env`，没有调用付费接口。

| 动作 | 结果 |
| --- | --- |
| `backend` 下 `uv sync --frozen` | 成功。会装 torch 等大包，因为 camel-ai 依赖它们 |
| `uv run pytest -q` | 130 通过，6 条来自 `zep_cloud` 类型注释的 SyntaxWarning |
| 根目录和 `frontend` 的 `npm ci` | 成功。npm 报告前端依赖有 2 个漏洞（1 中 1 高），没有改依赖去修 |
| `frontend` 下 `npx vite build` | 成功。有一张动态 import 提示和一个大于 500 kB 的包体积警告 |
| 不设钥匙执行 `uv run python run.py` | 退出码 1，提示缺少 `LLM_API_KEY` 和 `ZEP_API_KEY` |
| 用假钥匙创建 Flask 测试客户端，请求 `GET /health` | 200，`{"status":"ok","service":"MiroFish Backend"}` |
| `docker compose up` | 没做。`docker` 命令不存在，也不能在没有钥匙的情况下完成一次真实模拟 |

因此：安装、测试、前端构建、后端健康检查是实测。完整 Docker、Zep 建图、OASIS 多轮群演、报告质量都还没跑过，后面章节里关于运行时行为的描述来自读代码，以及对照 GitHub 上 `camel-oasis` v0.2.5 的源码（和 `pyproject.toml` 里的版本一致）。

---

## 2. 改造方案

四个改造彼此可拆开。推荐顺序写在第 4 节。推文模式可以先不依赖 Zep 替换，这样 M5 不必等 M3。

### 2.1 全中文界面，输出默认中文

现状：界面已经用 vue-i18n，`frontend/src/i18n/index.js` 默认 `localStorage` 没有语言时用 `zh`，回退也是 `zh`。`locales/zh.json` 覆盖了首页和大部分步骤。大模型提示词末尾也会加「请使用中文回答」。所以这不是从零做国际化，而是收尾，并改成中文优先产品，而不是「预测万物」的通用演示站。

还没收干净的地方（读代码看到的，没有逐个像素核对浏览器）：

- `frontend/src/views/MainView.vue` 的状态文字仍是英文：`Error`、`Ready`、`Building Graph`、`Generating Ontology`、`Initializing`，以及 `Step {{ n }}/5`。若干日志以 `Error:` 开头。
- `SimulationView.vue`、`SimulationRunView.vue`、`ReportView.vue`、`InteractionView.vue` 在错误状态返回英文 `Error`。
- `frontend/src/components/Step3Simulation.vue` 平台名写死为 `Info Plaza` / `Topic Community`，统计标签是 `ROUND`、`TIME`、`ACTS`，提示是 `Available Actions`。
- 首页 GitHub 链接指向 `https://github.com/666ghj/MiroFish`（`frontend/src/views/Home.vue`）。
- 语言切换器仍提供英、西、法、葡、俄、德（`locales/languages.json`）。中文优先不等于立刻删掉它们，但默认路径和文案应该先为中文服务。
- 人设失败时的规则兜底是英文（`OasisProfileGenerator._generate_profile_rule_based`）。
- OASIS 自带的行动提示词是英文，见第 3 节。只改界面语言不会让群演用中文思考。

做法：

1. 把上述硬编码收进 `locales/zh.json`，默认语言保持中文。
2. 首页、步骤说明、按钮改成推文预演也能看懂的说法，同时留一个「通用模拟」入口，避免把后面的游戏/播客/故事堵死。
3. 后端在推文模式忽略浏览器语言，报告和人设固定中文；通用模式仍尊重 `Accept-Language`。
4. 规则兜底人设改成中文短句。
5. 品牌和 GitHub 链接是否换成这个 fork，等你决定（开放问题）。

要动的文件：`locales/zh.json`、`frontend/src/views/MainView.vue`、四个 `*View.vue`、`Step3Simulation.vue`、`Home.vue`，以及人设兜底函数。推文专用页面放到 2.4，不塞进这次扫尾。

风险：有些字符串是日志和状态机用的，改文案时不要改状态枚举值（`created`、`running` 这些要保持英文机器值）。

备选：继续维持七种语言并只补漏网英文。工作量更小，但和「中文优先产品」不一致。建议先补漏、把默认路径写成中文产品，语言包先留着不删。

规模：S。若连首页叙事和步骤文案一起改成双入口（推文 / 通用），算 M，可以和 M5 的页面一起做。

### 2.2 按角色选模型，并加上费用闸门

现状已经能打任何 OpenAI 兼容接口，缺的是角色和闸门。

建议的角色：

| 角色 | 用途 | 模型倾向 |
| --- | --- | --- |
| `persona` | 一次生成整个人群 | 较强 |
| `report` | 结案 JSON | 较强 |
| `agent` | 每个模拟用户的每一轮 | 便宜、快 |
| `ontology` / `extractor` | 通用模式的本体和本地抽图 | 中等 |
| `moderator` | 可选，只负责叫醒谁 | 便宜；第一版可以不用模型 |

配置形状（名字可以再定，语义如下）：

```env
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL_NAME=...          # 默认兜底

LLM_AGENT_API_KEY=...       # 可省略，省略则用上面的兜底
LLM_AGENT_BASE_URL=...
LLM_AGENT_MODEL_NAME=...

LLM_REPORT_MODEL_NAME=...
# persona、ontology 等同理

SIM_MAX_AGENTS=24
SIM_MAX_ROUNDS=4
SIM_MAX_TOKENS_PER_CALL=800
SIM_MAX_TOTAL_TOKENS=200000
```

做法：

1. 在 `backend/app/utils/llm_client.py` 上加一个按角色取客户端的工厂，而不是到处 `OpenAI(api_key=Config.LLM_API_KEY)`。
2. `oasis_profile_generator.py`、`simulation_config_generator.py`、`ontology_generator.py`、`report_agent.py`、`zep_tools.py` 改成问工厂要客户端。
3. OASIS 那条路不要再写全局 `OPENAI_API_KEY`。要确认 camel 的模型对象在创建时复制了密钥。如果做不到，推文模式就不要用 camel 的工厂，改为我们自己的行动循环调用 `LLMClient`（见 2.4 的备选）。
4. 每次调用记录角色、模型名、prompt token、completion token。模拟目录里落一个 `usage.json`。超过 `SIM_MAX_TOTAL_TOKENS` 就停，并在报告里说明是被闸门截断的。
5. 推文模式的默认值用小数字（24 人、4 轮、只开 Twitter）。通用模式保留现在的双平台，但默认轮数不要再悄悄变成 72。

风险：

- 有的兼容接口不支持 `response_format`。`LLMClient.chat_json` 已经会在明确拒绝时降级。Grok 或别的兼容层仍可能在工具调用格式上和 camel 不一致。M2 要用假服务器测「角色 A 和角色 B 打到不同 URL」，M5 再用真实模型看工具调用。
- 思考模型会把 token 花在隐藏推理上。人设和报告可以让供应商自己决定上限；agent 行动必须设小的 `max_tokens`，否则 24 人 × 4 轮也会很贵。
- 不要把密钥写进模拟配置 JSON。现在配置文件会落到 `uploads/simulations/`，里面如果出现密钥，就容易被以后的日志或下载接口带出去。

备选：只继续用现有的 `LLM_BOOST_*`，把 Twitter 指到便宜模型、报告指到贵模型。改动更小，但表达不了五个角色，也解决不了全局环境变量。不建议停在这一步。

规模：M。

### 2.3 去掉 Zep Cloud，换成可本地跑的记忆

要保住的行为，不是保住 Zep 的 API 形状：

- 输入一段文本和一份本体，得到实体和关系
- 按类型把实体读出来，生成人设
- 模拟过程中追加「后来发生了什么」
- 报告能按关键词找到相关事实，最好还能带一点语义搜索
- 删项目时把对应的图删掉

推荐做法：加一个记忆接口（例如 `backend/app/services/memory/`），两种实现：

- `zep`：把现在的调用包进去，测试继续能 mock 云端
- `local`：SQLite 文件，例如 `uploads/graphs/<id>.sqlite`

本地库的表够用即可：`nodes`、`edges`、`episodes`、可选的 `embeddings`。抽取不再交给 Zep，而交给 `extractor` 角色：把文本块和本体交给模型，要它返回 JSON 实体和关系，校验失败就丢弃该块并记日志。搜索先用 SQLite FTS5；语义搜索再用已经随 camel 装上的 sentence-transformers，在本机算向量，不另买嵌入接口。模拟行为写入 `episodes`，需要时再抽成新的边。

环境变量建议：`MEMORY_BACKEND=local|zep`，默认在这个 fork 里改为 `local`。`ZEP_API_KEY` 只在 `zep` 时必填。`run.py` 的校验要跟着改，否则本地模式仍然起不来。

要动的文件：`config.py`、`run.py`、`utils/zep.py`、`graph_builder.py`、`zep_entity_reader.py`、`zep_graph_memory_updater.py`、`zep_tools.py`、`oasis_profile_generator.py`（它会用 Zep 检索来补人设上下文）、`api/graph.py` 的删除逻辑，以及 `backend/tests/test_zep_*.py` 那一组。前端画图读的是 `/api/graph/data/<graph_id>`，只要这个 JSON 形状不变，`GraphPanel.vue` 可以不动。

风险：

- 这是四个改造里最大的一块。Zep 的抽取质量、时间有效性（`valid_at` / `invalid_at`）、混合搜索，本地第一版都会更糙。报告如果仍假设「图里什么都有」，会写出更空的章节。
- 现有测试大量锁的是 Zep Cloud 的契约。应该保留契约测试给 `zep` 实现，给 `local` 另写一组用临时 SQLite 的测试，不要把云端测试改成永远跳过。
- sentence-transformers 能离线算向量，但模型文件要下载。没网或没模型时，应自动退回 FTS5，而不是启动失败。
- 不要在这一步引入 Neo4j 或另一套必须常驻的服务，除非本地抽取明显不够用。

备选：

1. 自建开源 Zep / Graphiti。行为最接近，但当前代码拒绝非云地址，而且通常要 Neo4j。对个人单机偏重。留作抽取质量不够时的退路。
2. 推文模式完全不建图，只把 OASIS 的 SQLite 动作日志当记忆。通用模式暂时仍用 Zep。这能让推文先做出来。建议采纳这条作为 M5 的范围，M3 再替换通用模式的图。

规模：L。

### 2.4 一键「推文预演」

不要把一则一两百字的草稿送进「上传研报 → 抽 10 类实体 → 双平台跑 72 小时」这条链路。那会又慢又贵，而且抽出来的实体往往不是「刷到这条推文的人」。

建议新开一个模式，通用五步流程留着：

- 新路由，例如 `/tweet`。表单字段：草稿正文、可选的发帖人自述、可选受众、语言（自动检测，允许手改）、人数、轮数。人数和轮数有上限，默认 24 和 4。
- 后端新入口，例如 `POST /api/modes/tweet/run`，内部仍复用模拟目录、动作日志和报告存储，避免再造一套文件格式。
- 人设来自配额表，不来自图谱。提示词见 `docs/prompts/tweet_persona.zh.md`。一次强模型调用生成全部人设。
- 草稿作为唯一初始帖，用 `ManualAction` 写入，作者可以是一个不参与闲聊的「发帖账号」，避免发帖人自己和自己辩论。
- 只开 Twitter 形态的广场。第一版动作：划走、点赞、转发、引用。回复若实验证明 `CREATE_COMMENT` 在 Twitter 库上可用，再打开；否则报告里的「回复」先收录引用帖，并在界面上叫「引用/回复」，不要假装有楼中楼。
- 行动提示词用 `docs/prompts/tweet_agent_action.zh.md`，不要用 OASIS 英文默认提示词。`generate_twitter_agent_graph` 没有把自定义系统提示词暴露出来，所以推文模式应自己建 agent（OASIS 的 `SocialAgent` 支持 `user_info_template`），或在建图后替换系统消息。不要去改 site-packages。
- 调度用规则，不用主持人模型。规则见 `docs/prompts/tweet_round_moderator.zh.md`。这样多数路人不会产生调用。
- 报告不走现在的「未来预测、2 到 5 章、必须搜 Zep」。改为一次强模型调用，输入是动作日志，输出固定 JSON，再由前端渲染。草稿见 `docs/prompts/tweet_report.zh.md`。程序要检查引用是不是日志原文。
- 模式注册做成小表：`id`、输入字段、人设来源、平台、报告格式。游戏购买、播客选题、故事续写以后各加一行，不改推文表单。

要动的文件（实现时）：

- 新增 `frontend/src/views/TweetPreview.vue`（名字待定）、路由、`frontend/src/api/` 里的一个模块
- 新增 `backend/app/services/modes/tweet.py`（编排）、人设配额、报告校验
- `backend/app/api/` 增加一个蓝图或挂在 `simulation` 下
- `backend/scripts/run_parallel_simulation.py` 需要能「只跑 Twitter、使用外部人设、使用自定义系统提示词、使用更小的轮数」。能加参数就加参数，避免复制整个 1500 行脚本
- 首页加一个主按钮进入推文模式

风险：

- OASIS 的 Twitter 环境提示词和工具调用是英文函数名。模型要同时看懂中文人设和英文工具名。需要在 M5 用真实模型跑一轮 4 人 × 1 轮的冒烟，看它会不会乱调用。
- 如果 camel 的工具调用在目标模型上不稳定，备选是自写一个很短的行动循环：把时间线给 `LLMClient`，要求 JSON 动作，由我们写入一个自己的 SQLite。这会脱离 OASIS，但推文场景动作很少，循环大概一两百行。通用模式继续用 OASIS。这是推文模式的退路，不要一开始就重写社交平台。
- 人设若仍按「每个实体一次 2000 字」去生成，24 人也是 24 次贵调用。推文模式必须改成一次批量生成。
- 报告若仍鼓励「上帝视角预言未来」，模型会把 24 个玩具账号说成社会共识。提示词和校验都要压住这件事。

规模：L。其中「表单 + 假数据报告」是 M，「接上真实群演」才是 L。

---

## 3. 提示词

### 3.1 现有提示词清单

下面都在本仓库里，除非特别注明在 `camel-oasis` 0.2.5。

| 位置 | 作用 | 主要问题 |
| --- | --- | --- |
| `ontology_generator.py` 的 `ONTOLOGY_SYSTEM_PROMPT` | 设计 10 个实体类型和一批关系 | 为「舆情事件当事人」设计，不适合「一条草稿的读者」。类型数量卡死。中英指令混在一起 |
| `oasis_profile_generator.py` 的 `_get_system_prompt`、`_build_individual_persona_prompt`、`_build_group_persona_prompt` | 每个实体一份人设，要求 persona 约 2000 字 | 一人一次调用；过长；性别只许 male/female/other；失败兜底是英文；个人类型列表是学生/校友/教授，覆盖不了粉丝和路人 |
| `simulation_config_generator.py` 的时间、事件、agent 活动三段提示词 | 生成作息、初始帖、立场和活跃度 | 立场只有 supportive/opposing/neutral/observer，没有「划走」。初始帖让模型编内容，推文模式应该用用户原稿。作息按 72 小时舆论事件设计 |
| `report_agent.py` 的 `PLAN_*`、`SECTION_*`、`CHAT_*`、若干 ReACT 模板 | 未来预测报告，每章 3 到 5 次工具调用 | 鼓励把模拟写成「未来已经发生」。章节自由发挥，没有热度档和改写。引用必须翻译成报告语言，增加一轮改写误差。反思轮次是空常量 |
| `zep_tools.py` 的子问题分解、选采访对象、出采访题、采访摘要 | 报告工具内部 | 仍假设有一张大图和一群事件当事人 |
| `simulation.py` 的 `INTERVIEW_PROMPT_PREFIX` | 采访时禁止 agent 再调用工具 | 可用，但是通用采访，不是推文结案 |
| camel-oasis `UserInfo.to_twitter_system_message` | 每个 Twitter agent 的系统提示词 | 英文；只有名字和整段 profile；原文还有语法问题（"Your have profile"）。每轮用户消息是 "Please perform social media actions..."，并鼓励不要只点赞。这会制造过多发言，也就是虚假的热闹 |
| camel-oasis `SocialAgent.perform_action_by_llm` | 每轮把时间线塞给模型 | 时间线由环境生成，仓库控制不了文案。推文模式如果继续用它，只能换系统提示词，换不了每轮那句英文，除非自己包一层 |

没有单独的「主持人」提示词。谁醒着，是 `get_active_agents_for_round` 用随机数和作息决定的。

### 3.2 推文预演要改成什么样

原则：

- 人设一次生成，短，并且按配额分配角色。完整草稿在 `docs/prompts/tweet_persona.zh.md`。
- 每个账号每轮的提示词只包含自己的短人设和公开时间线。完整草稿在 `docs/prompts/tweet_agent_action.zh.md`。
- 调度第一版用规则，不增加一次模型调用。规则和可选的便宜模型版本在 `docs/prompts/tweet_round_moderator.zh.md`。
- 结案只调用一次，输出固定 JSON。完整草稿在 `docs/prompts/tweet_report.zh.md`。前端再用同一份 JSON 渲染成可读备忘，不要让模型同时写两份容易互相矛盾的正文。

多样性，针对中文 X 而不是微博热搜：

- 中文用户里同时有路人、粉、黑、行业内部、媒体号、爱阴阳的 KOL。不要全部写成愤怒网友。
- 语言风格分四档：口语、书面、中英夹杂、极短。配额里每种角色再错开风格，避免 24 个人同一套「家人们」。
- 立场在生成前就定死，模型不许改配额。默认 24 人里路人 6 个，且 stance 为 `ignore`。
- 每个人设写明「不知道别人的集体态度」。
- 真实名人不要出现。影响力账号用虚构 KOL，bio 里标明虚构。
- 喷子可以尖锐，提示词明确禁止威胁、色情和编造草稿里没有的数据。

压低「全体同意」：

- 行动提示词把划走写成默认，而不是把发言写成默认。这和 OASIS 原句 "don't limit your actions to just like" 相反，所以必须换掉原提示词。
- 调度规则：如果上一轮没有反对意见，下一轮必须叫醒一个还没说话的反对或混合立场。
- 报告禁止「舆论一边倒」，除非同时给出反对条数。样本小时写进 `caveats`。
- 结案后的程序检查：引用必须能在日志里原样找到。编出来的金句丢掉。
- 同一立场的人如果要表示同意，优先点赞，不要复读。

费用：

- 人设 1 次（强模型，输出大约 24 × 200 字）。
- 行动：不是 24 × 4。`ignore` 和高门槛账号默认不调用。粗算第一轮约 10 次，后面每轮 6 到 8 次，4 轮大约 30 到 40 次便宜模型。每次输入是短人设加几条时间线，输出限制在大约 180 token。
- 报告 1 次（强模型）。
- 不跑 Reddit，不建 Zep 图，不给每个实体写 2000 字。
- 硬上限见 2.2。超过就停，报告注明截断。

下面是结案 JSON 的字段约定（与 `docs/prompts/tweet_report.zh.md` 一致）：

```json
{
  "engagement_tier": "low | medium | high",
  "tier_reason": "用动作计数说明",
  "trigger_lines": [
    {"text": "草稿原文片段", "polarity": "positive|negative|mixed", "why": "", "evidence_ids": []}
  ],
  "top_replies": [
    {"evidence_id": "", "role": "", "text": "日志原文", "why_it_might_spread": ""}
  ],
  "backlash_risk": {"level": "low|medium|high", "triggers": [], "likely_frames": []},
  "disagreement": {
    "supportive_count": 0,
    "opposing_count": 0,
    "ignore_or_like_only_count": 0,
    "note": ""
  },
  "rewrites": [
    {"text": "完整改写", "intended_change": "避开哪一句"}
  ],
  "caveats": []
}
```

热度档只描述这次模拟：发言少于 4 或划走超过 70% 为 low；发言不少于 10 且至少 3 种角色发了言为 high；其余 medium。不要输出「预测真实浏览量」。

前端展示时把 JSON 渲染成五块：热度、刺点、可能被顶上来的回复、反噬、改写。`caveats` 始终展开，避免看起来像精确预测。

---

## 4. 分阶段里程碑

每一步都应该能单独合并、单独证明。建议顺序：M1 → M2 → M4 可以和 M2 并行 → M5（推文先不建图）→ M3（通用模式换成本地图）→ M6。M6 的夹具可以在 M5 之前就放进仓库，但没有真实跑数之前不要假装评估已完成。

### M1 按上游方式在本地跑起来

- 范围：不改产品行为。补一份「从零到健康检查」的记录即可，如果发现 README 和代码不一致，只改文档。
- 验收：在有 Python 3.11 或 3.12、Node 18+、uv 的机器上，`uv sync --frozen` 后 `uv run pytest -q` 全部通过；`npx vite build` 成功；不设钥匙时 `run.py` 以缺少 `LLM_API_KEY` 和 `ZEP_API_KEY` 退出。若要证明完整链路，还需要你自己的两把钥匙，以及 Docker 或本机双进程：上传一份短 txt，把轮数压到 2，看到至少一个动作日志和一份报告。这一步本次没有钥匙、也没有 Docker，所以没有完成。
- 难度：simple。

### M2 角色模型层和费用闸门

- 范围：工厂方法、环境变量、调用计数、超过总 token 就停止。用假的 HTTP 服务器测试，不要求真实钥匙。OASIS 脚本改为从工厂拿「agent」角色，并停止依赖进程级环境变量（若 camel 做不到，就在这一步记下来，把推文行动循环标成必须自写）。
- 验收：测试里 persona 请求发到 URL A、agent 请求发到 URL B；模拟计数器在达到 `SIM_MAX_TOTAL_TOKENS` 后不再发请求；`uploads` 里的配置 JSON 不含密钥。现有 130 个测试仍然通过。
- 难度：normal。

### M3 本地图替换 Zep

- 范围：`MEMORY_BACKEND=local` 的 SQLite 实现，覆盖建图、读实体、关键词搜索、写入模拟行为、删除。Zep 实现留着但不是默认。推文模式不依赖这一步。
- 验收：用一份 1 到 2 KB 的夹具文本，在不设置 `ZEP_API_KEY` 的情况下跑完建图；SQLite 里能查到夹具中的实体名；搜索能返回该事实；追加一条模拟行为后能再搜到；`MEMORY_BACKEND=zep` 时原有 Zep 契约测试仍可在 mock 下通过。语义向量是加分项，FTS5 必须有。
- 难度：hard。

### M4 中文界面收尾

- 范围：第 2.1 节列出的硬编码英文改为中文文案。默认语言保持中文。不在这一步做推文表单。
- 验收：`vite build` 成功。在浏览器打开首页和模拟步，状态、按钮、平台名不再出现第 2.1 节那些英文残片（品牌名 MiroFish 可以保留）。把界面语言留在默认值时，后端收到的 `Accept-Language` 是 `zh`。本机可以用 Vite 预览加浏览器点选；如果没有浏览器，至少用组件测试或渲染后的 HTML 字符串检查这些词。
- 难度：simple。

### M5 推文模式和专用提示词

- 范围：表单、配额人设、只跑短程广场、自定义行动提示词、固定 JSON 报告、引用校验。记忆用动作日志，不调用 Zep。提示词以 `docs/prompts/` 为起点，按冒烟结果再改。
- 验收分两层：
  1. 无真实模型：用假的大模型返回固定人设和固定动作，跑 4 个账号、1 轮，报告 JSON 通过校验，伪造的引用会被丢掉。
  2. 有真实模型时再做一次冒烟：一则 80 字以内的中文草稿，不超过 8 个账号、1 轮，日志里同时出现「发言」和「划走」，报告里的 `top_replies[].text` 能在日志中原样找到。这一层需要你的 API 钥匙，没有钥匙就不能勾掉。
- 难度：hard。

### M6 评估夹具

- 范围：`eval/tweets/` 下放小样本（见第 5 节），加一个脚本统计立场熵、划走比例、引用是否真实、三次重跑是否吵成一团、token 花费。不自动抓取别人的推文。
- 验收：对一个假日志夹具，脚本输出上述指标且退出码 0；真实样本的结论由人看，脚本不负责宣布「预测准了」。
- 难度：normal。

---

## 5. 怎么判断模拟像不像样

不要用「报告读起来通顺」当标准。通顺的报告最容易在编共识。

准备一个小集，大约 8 到 12 则，由你提供，放在仓库里时去掉不该公开的账号和隐私。每则包含：

- 当时的草稿原文
- 你记得的真实结果，用定性标签就行：反响平淡、被支持、被抓住一句话攻击、出现了意料外的解读
- 你事后认为最刺人的那半句
- 不要要求系统预测点赞数

每次跑完看四件事：

1. **引用是真的。** 报告里的回复和刺点句子都能在日志或草稿里原样找到。这是机器可检查的，M5 就要做。
2. **不是全体同意。** 划走加只点赞应该占多数。发言里至少能看到两种 stance。可以用立场分布的熵做一个下限：熵接近 0 就说明提示词或调度坏了。
3. **重跑不会换一个世界。** 同一草稿用同一模型和同一配额跑 3 次。热度档允许差一档，反噬等级不应从 low 跳到 high。被点名的刺点句子应有重叠。完全不一致就不要拿去指导发布。
4. **费用。** 每次记下调用次数、分角色 token、墙钟时间。推文模式的目标是：在默认 24 人、4 轮、只开广场的设置下，总 token 远小于现在「双平台 × 长人设 × 多轮 × 报告工具」的通用跑法。具体美元数等 M2 的 `usage.json` 和你选定的单价表再算，不要沿用界面上的 5 美元估计。

已知结果只用来给人做对照，不要在提示词里写「这则后来翻车了」。否则模型是在背答案，不是在预演。

样本太小，所以评估结论只能是「这套提示词会不会撒谎、会不会一边倒、贵不贵」，不能说「能预测 X 的真实传播」。

---

## 6. 需要你拍板的问题

1. 三个角色分别用哪家、哪个模型？至少需要：人群和报告用的强模型，群演用的便宜模型。base URL 是否都是 OpenAI 兼容（xAI、OpenAI，或别的中转）？
2. 默认真的用 24 人、4 轮吗？你能接受的单次推文预演花费上限是多少（按人民币或美元）？闸门会按这个反推 token 上限。
3. 第一版报告里的「回复」如果只能做成「引用转发」，能不能接受？还是必须先证明 OASIS 的 Twitter 库能写评论？
4. 中文 X 的人群按哪一种来配：海外中文用户（中英夹杂、时政和科技圈更多），还是更接近微博口吻？这会直接改配额和语言风格。
5. 通用五步流程还要不要留在首页？建议留，但推文按钮更靠前。若你希望这个 fork 只做推文，M3 可以再往后放。
6. 本地记忆是否同意用 SQLite，而不是自建 Zep / Neo4j？默认 `MEMORY_BACKEND=local`。
7. 界面品牌、Logo、GitHub 链接是否改成这个 fork？仓库名和文档标题要不要换掉 MiroFish？
8. 评估用的历史草稿由谁提供？请不要在后续任务里要求代理去爬真实用户的 X 时间线。
9. AGPL 之下，你是否只在自己机器上跑？如果要给别人开网页账号，需要另做授权和隔离设计，这次方案没有包含多用户。
