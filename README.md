# WenQu — 文档知识库 RAG 平台

多空间文档知识库 RAG 平台:文档接入(解析 → 分块 → 向量化/索引)→ 混合检索 → 带引用流式问答 → ReAct Agent(工具调用 + 跨会话事实记忆 + 历史滚动压缩)。

- 架构设计:[docs/架构设计.md](docs/架构设计.md)(档位:**精简档 + 多空间隔离**,依据《文档知识库 RAG 平台工程基准》)
- 上线手册:[docs/上线手册.md](docs/上线手册.md)(Go-Live 14 项翻转清单 + 冒烟七条 + 备份恢复实测)
- 版本单一真源:[VERSION](VERSION),发布只用固定 tag

## 服务组成

| 目录 | 说明 | 技术栈 |
|------|------|--------|
| `server/` | 主 API 服务(API + 流水线 worker + Agent 运行时) | Python 3.12 / FastAPI / Postgres(pgvector) |
| `parser/` | 文档解析服务(独立进程,gRPC + Token) | Python / gRPC / PyMuPDF 等(M2 接入) |
| `web/` | Web 前端 | Vue 3 + Vite + TS + Element Plus |
| `deploy/` | 部署配置与运维脚本 | Docker Compose |

## 快速开始(开发)

先决条件:[uv](https://docs.astral.sh/uv/)、Node 20+、Docker、GNU Make。

```bash
cp deploy/.env.example .env          # 开发默认值即可跑;生产必须逐项翻转(见设计文档 9.2)
make compose-dev-up                  # 起 postgres(pgvector)+ minio(仅基础设施)
make server-install && make server-run   # API: http://localhost:8000/health
make worker-run                      # 入库流水线 worker(不起则上传一直停在 pending)
make parser-install && make parser-gen && make parser-run  # 解析服务 :50051
make web-install && make web-dev     # 前端: http://localhost:5173
```

## 常用命令

```bash
make help        # 全部命令
make server-test / server-lint
make parser-test / parser-lint / parser-gen   # proto 生成物入库,改动 proto 后先 gen
make db-migrate  # alembic upgrade head
make server-openapi   # 改路由/模型后重导 docs/api/openapi.json(不重导会被契约门禁拦下)
```

### 一键演示(OPT-46)

给面试官/观众看时,不用手工点一遍 —— 数据提前种好,页面用真浏览器自动走。

```bash
make demo-seed      # 播种:demo 账号 + 演示空间 + 22 篇已入库文档 + 3 条记忆(幂等,可重跑)
make demo-browser   # 装一次浏览器内核(playwright chromium,约 150MB)
make demo-run ARGS="--headed -n 6"   # 自动演示:登录→建空间→建档位→签 Key→看库→提问,截图进 demo-out/
make demo-seed-purge  # 收回演示空间(演示账号保留)
```

前提是服务端 `APP_DEMO_MODE=1` 且 `APP_DEMO_PASSWORD` 已设、server/worker/parser 在跑;
对着生产播种与演示时加 `WENQU_BASE_URL=http://<站点>`。完整步骤与故障对照见
[docs/上线手册.md](docs/上线手册.md) §9,设计口径见 [docs/设计-OPT46-演示模式.md](docs/设计-OPT46-演示模式.md)。

## 工程纪律(摘要,完整见设计文档)

- 依赖:宽声明 + 锁安装(`uv sync --locked` / `npm ci`),lockfile 必须提交
- 配置:优先级 环境变量 > YAML > 代码默认;密钥永不进 YAML;生产关键配置缺失拒绝启动
- 队列拓扑单一事实源;文档处理函数幂等(先清后写);任务失败进死信并翻用户可见状态
- 新增模型/向量库/存储 Provider 改动 ≤3 处,注册风格全项目只有一种(工厂 switch + 常量表)
- 契约镜像:新增端点必须同步进 `tests/test_openapi_contract.py` 的 `REQUIRED_PATHS`,新增错误码必须进
  `REQUIRED_ERROR_CODES` 且在 `core/errors.py` 注册 —— 表与实现是**双向镜像门禁**,多做少做都红
- 迁移:`test_migration_gate.py` 硬断言脚本 head 号,加迁移必须同步改它;删列型迁移不得先于代码上到共用库

## 里程碑状态

- [x] M0 骨架(仓库/工具链/CI/compose/迁移框架/hello world)
- [x] M1 账号与空间(JWT + 空间 RBAC + 审计基础;前端页面对接完成)
- [x] M2 核心 RAG 闭环(解析/入库流水线/分块+预览/混合检索/引用流式问答;页面对接完成)
- [x] M3 体验与治理(父子分块/插件链化/API Key/契约测试扩面;另含凭据加密与断线续流,均提前交付)
- [x] M4 Agent 与上线(ReAct 工具链与 `tool_call`/`tool_result` 事件;Go-Live 14 项翻转 + 备份恢复演练
      + 冒烟七条脚本化实测 23/23 零重试;生产发布走手动 dispatch + 固定 tag,已实跑至 v0.0.5)
- [x] M4 之后的需求直插(OPT-25…OPT-46):模型配置空间化与参数自定义、API Key 与权限档位改造、
      外部事实记忆与历史滚动压缩、演示模式(一键体验 + 自动演示脚本)

> 判定依据:`docs/架构设计.md` §10 路线图与 git 提交历史(非本清单自身)。
> 契约:`docs/api/openapi.json`;对接规则见 `web/对接文档.md`。

### 当前已知缺口(后端侧)

| 项 | 状态 |
|---|---|
| 模型错误码 | ✅ 已修:`MODEL_NOT_CONFIGURED` 503 / `MODEL_CALL_FAILED` 502(走统一错误壳) |
| SSE 流内失败 | ✅ 已修:检索失败发 `error` 事件,不再静默截断 |
| 断线续流(resume) | ✅ 已实现(OPT-3:`GET .../conversations/{cid}/stream?after=<seq>`;断线 5 秒宽限后落库部分答案并发 `done(partial=true)`)。可选增强,不接则回退拉历史照旧 |
| Agent 工具调用事件 | ✅ 已实现(OPT-10:`tool_call`/`tool_result` 成对 `id`,步骤落 `messages.agent_steps` 供刷新回看;OPT-45 再加 `memory_proposal`) |
| 权限档位灰度生效 | ⏸ 代码已交付但开关默认关(`APP_REQUIRE_PERMISSION_GROUP_FOR_CORE=0`);打开前必须先给每个真人建档 + 分配 + 设生效档,否则那一刻无档位的真人核心接口全 403(OPT-42) |
| OPT-45 记忆与压缩真机验收 | ⏳ 前后端已并 main;库要 `make db-migrate` 升到 0021 才有对应表/列,浏览器端按 `docs/api/前端接入工单-OPT45记忆卡片与上下文压缩.md` §10 的验收尚未跑 |
| OPT-46 演示模式 | ⏳ 后端/前端/自动演示脚本**已并 main**(PR #14),本地真机彩排全绿(提问 7 条命中 7 条、种子连跑三次幂等);**生产还没发布**,所以生产站现在打开登录页看不到演示入口。发布后按 `docs/上线手册.md` §9 加两行开关 + 播种,再补 A8/A9/A10 三条生产验收 |
| 审计日志导出 / 邮箱体系 | ❌ 明确不做(见 `web/对接文档.md` §2.4 L3) |

> **交付了什么、验收数字多少 → [`docs/V1交付台账.md`](docs/V1交付台账.md)。**
> **哪些是有意延后的优化(OPT-xx,含触发条件)→ [`docs/优化台账.md`](docs/优化台账.md)。**
> 基准裁剪的完整登记在 [`docs/架构设计.md`](docs/架构设计.md) §11。
