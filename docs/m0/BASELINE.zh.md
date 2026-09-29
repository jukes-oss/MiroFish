# M0 源码基线

记录时间：2026-09-29。命令在加入本里程碑的契约文件之前执行，源码为 `main` 上的 `39d849138ef254f6c737ab4c4705e5545dbe31d4`（`chore: update star history [skip ci] (#792)`）。锁文件没有升级，也没有为了让命令通过而修改应用代码。

## 环境

| 项目 | 本次实际值 |
| --- | --- |
| Python | 3.12.3，`/usr/bin/python3.12`。满足 `backend/pyproject.toml` 的 `>=3.11,<3.13` |
| Node | v22.22.2，`/home/ubuntu/.nvm/versions/node/v22.22.2/bin/node` |
| npm | 10.9.7，与上面的 Node 22.22.2 是同一安装 |
| uv | 0.12.20，`/home/ubuntu/.local/bin/uv`。只用来执行冻结同步，没有写入仓库依赖 |

根目录 `package.json` 的 `engines.node` 仍是 `>=18.0.0`。这是上游声明的最低版本，不是这次基线选择的长期版本，也没有在本次把它改成 Node 18。本次使用的是能安装当前 lockfileVersion 3 锁文件的 Node 22。

## 锁文件

| 文件 | 版本标记 | SHA-256 |
| --- | --- | --- |
| `package-lock.json` | lockfileVersion 3；`concurrently` 9.2.4 | `8a4bcce87e9e5ab7f27f498ad8d3d4d53ad3862fa894dfc0918092f7039edcbd` |
| `frontend/package-lock.json` | lockfileVersion 3；`vue` 3.5.25，`vue-i18n` 11.3.0，`vue-router` 4.6.3，`vite` 7.3.6，`@vitejs/plugin-vue` 6.0.2，`axios` 1.18.1 | `a4a188daf04a442b8e23c3291d9062b4e00faa5ff49503db759c85e67c4037be` |
| `backend/uv.lock` | `version = 1`，`revision = 3`，`requires-python = ">=3.11, <3.13"` | `877281f85fe26eb120149503b0b99021fd7e3e577d026f584857d03839595b09` |

`backend/uv.lock` 中与本次相关的包版本：`camel-ai` 0.2.78，`camel-oasis` 0.2.5，`zep-cloud` 3.25.0，`flask` 3.1.2，`openai` 1.109.1，`pytest` 8.2.0，`torch` 2.9.1，`sentence-transformers` 3.0.0，`jsonschema` 4.25.1。`jsonschema` 是锁文件里已有的传递依赖，契约检查直接使用它，没有新增依赖。

## 实际命令

日志开头的 `command`、`cwd`、`date` 和工具版本是记录包装加上的。`uv sync` 的 `command` 行已改成实际参数；其余行是工具原文。

| 顺序 | 实际命令 | 工作目录 | 退出码 | 日志 |
| --- | --- | --- | --- | --- |
| 1 | `npm ci` | `/workspace` | 0 | [logs/npm-ci-root.txt](logs/npm-ci-root.txt) |
| 2 | `npm ci --prefix frontend` | `/workspace` | 0 | [logs/npm-ci-frontend.txt](logs/npm-ci-frontend.txt) |
| 3 | `npm run build --prefix frontend` | `/workspace` | 0 | [logs/npm-build-frontend.txt](logs/npm-build-frontend.txt) |
| 4 | `uv sync --frozen --python /usr/bin/python3.12` | `/workspace/backend` | 0 | [logs/uv-sync.txt](logs/uv-sync.txt) |
| 5 | `uv run pytest -q` | `/workspace/backend` | 0 | [logs/pytest.txt](logs/pytest.txt) |

计划中的后端命令是 `uv sync --frozen`。这里增加 `--python /usr/bin/python3.12`，只是指定落在依赖范围内的解释器，没有放宽 `--frozen`，也没有刷新锁文件。

## 只记录、没有当作缺陷修掉的输出

上述命令都退出 0，所以没有基线失败需要修。下面这些文字出现在原始日志里，本次没有改锁文件、没有改模拟引擎，也没有把它们写成必须复现的验收条数：

- 前端 `npm ci` 打印 `2 vulnerabilities (1 moderate, 1 high)`，并建议 `npm audit fix`。退出码仍是 0。
- 前端构建打印动态 import 与 chunk 体积警告。退出码仍是 0。`frontend/dist` 被 `.gitignore` 忽略，没有提交。
- `uv run pytest -q` 打印 6 条来自已安装 `zep_cloud` 的 `SyntaxWarning`。pytest 原文的汇总行只描述这一次运行；日志最后的 `exit=0` 是记录包装加上的。该汇总不作为后续必须通过的测试条数。
