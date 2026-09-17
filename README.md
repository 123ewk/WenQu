# WenQu — 文档知识库 RAG 平台

多空间文档知识库 RAG 平台:文档接入(解析 → 分块 → 向量化/索引)→ 混合检索 → 带引用流式问答 → ReAct Agent。

- 架构设计:[docs/架构设计.md](docs/架构设计.md)(档位:**精简档 + 多空间隔离**,依据《文档知识库 RAG 平台工程基准》)
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
make parser-install && make parser-gen && make parser-run  # 解析服务 :50051
make web-install && make web-dev     # 前端: http://localhost:5173
```

## 常用命令

```bash
make help        # 全部命令
make server-test / server-lint
make parser-test / parser-lint / parser-gen   # proto 生成物入库,改动 proto 后先 gen
make db-migrate  # alembic upgrade head
```

## 工程纪律(摘要,完整见设计文档)

- 依赖:宽声明 + 锁安装(`uv sync --locked` / `npm ci`),lockfile 必须提交
- 配置:优先级 环境变量 > YAML > 代码默认;密钥永不进 YAML;生产关键配置缺失拒绝启动
- 队列拓扑单一事实源;文档处理函数幂等(先清后写);任务失败进死信并翻用户可见状态
- 新增模型/向量库/存储 Provider 改动 ≤3 处,注册风格全项目只有一种(工厂 switch + 常量表)

## 里程碑状态

- [x] M0 骨架(仓库/工具链/CI/compose/迁移框架/hello world)
- [x] M1 账号与空间(JWT + 空间 RBAC + 审计基础;前端页面对接完成)
- [x] M2 核心 RAG 闭环(解析/入库流水线/分块+预览/混合检索/引用流式问答;页面对接完成)
- [ ] M3 体验与治理(父子分块/插件链化/API Key/契约测试扩面)
- [ ] M4 Agent 与上线(ReAct 工具/Go-Live 14 项翻转/备份演练/冒烟)

> 判定依据:`docs/架构设计.md` §10 路线图与 git 提交历史(非本清单自身)。
> 契约:`docs/api/openapi.json`;对接规则见 `web/对接标准.md`。

### 当前已知缺口(后端侧)

| 项 | 状态 |
|---|---|
| 模型错误码 | ✅ 已修:`MODEL_NOT_CONFIGURED` 503 / `MODEL_CALL_FAILED` 502(走统一错误壳) |
| SSE 流内失败 | ✅ 已修:检索失败发 `error` 事件,不再静默截断 |
| 断线续流(resume) | ❌ 未实现(ADR-3 已设计,随多副本一起上)→ 前端不得依赖,断线回退拉历史 |
| Agent 工具调用事件 | ❌ 未实现(M4)→ 对话页工具卡不得伪装真实数据 |
| 审计日志导出 / 邮箱体系 | ❌ 明确不做(见对接标准 §3.4) |

