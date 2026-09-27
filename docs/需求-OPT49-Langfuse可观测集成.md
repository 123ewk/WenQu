# OPT-49 需求单（计划文档）：Langfuse 全链路可观测 —— 按"模型配置"标准接入

> **本文档是计划，不含实现代码。** 2026-09-26 起草；拍板后按 §6 切片实施，一片一提交。

## 0. 一句话说清要什么

把"每条问答 / 每个入库任务的可观测 trace"做成真功能：**Langfuse 按"模型配置"的标准接入**——
管理页提供地址与密钥（直接去 Langfuse 申请）、密钥加密落库、变更写审计日志；未配置时零开销零依赖，
观测自身的任何异常绝不影响问答主链路。

## 1. 现状与事实基线（2026-09-26 实测）

| 事实 | 位置/证据 | 影响 |
|---|---|---|
| Langfuse **零集成**：代码无引用、pyproject/uv.lock 无依赖 | 全库 grep | 旧简历 v2"对接 Langfuse 落 trace"一条无实现支撑，v3 已删 |
| 仅有一个**预留空配置位** `APP_OTLP_ENDPOINT`（OTLP 协议、默认关闭） | `server/src/app/core/config.py:127`；架构设计 §6.4："trace 从 HTTP 头 → 任务 payload → worker 同棵树；默认关闭，上线前接入(P1)" | 设计意图早已预留：跨进程 trace 贯穿是既定方向；本需求是把它落地 |
| **埋点候选（真实代码位置）**：问答七阶段插件 retrieve/cite/fallback/recall_memory/compose/generate/persist | `application/service/qa_pipeline.py`（RetrieveStage 持多路召回分数、GenerateStage 持 LLM I/O 与 token 用量） | 每阶段一个 span 是最自然的切法 |
| Agent 工具轮：轮数预算 20、CitationLedger 引用账本、Pydantic 参数强校验、ToolContext 显式范围 | `application/agent/loop.py`、`tools.py` | 工具调用 span + 引用编号入 trace，Agentic RAG 可观测 |
| 入库 worker：`python -m app.worker`（DB 队列消费） | `app/worker.py:119` | 任务 trace 贯穿需 payload 携带 trace 上下文 |
| **配置形态的先例 = 模型配置（OPT-25/27）**：admin 页提供地址+密钥 → AES-GCM 加密落库（CredentialCipher）→ 响应只回 key_hint → 变更写 audit_logs | `application/service/model_admin.py:76-133,259` | **用户已拍板：Langfuse 按同一标准做** |

## 2. 配置形态（用户已定）：像"模型配置"那样

- 新表 `langfuse_config`（全局单行）：`host`（Langfuse 地址，云版或自托管 URL）、
  `public_key`、`secret_key_encrypted`（AES-GCM 加密，CredentialCipher 同款）、
  `key_hint`（掩码回显，如 `pk-lf-1234****5678`）、`enabled`、`updated_at/updated_by`；
- 管理页（superuser）：填写地址 + 密钥，页面上放"去 Langfuse 申请"外链（注册后把 pk/sk 粘回来）；
- `GET/PUT /api/v1/admin/langfuse-config`：PUT 后本进程即时生效（配置读取带短 TTL 缓存 + 写时失效，复用 OPT-48 模式）；
- **审计**：新增动作 `langfuse_config.updated`（detail 带旧值摘要与操作者，密钥本体不落审计，只有 key_hint）；
- 环境变量 `APP_OTLP_ENDPOINT`：**建议删除**（空配置位无人消费，留着误导）——待 §7 Q4 确认。

## 3. trace 设计（埋什么）

- **一条用户提问 = 一个 trace**（traceId 从请求入口生成，SSE/Agent 多轮共享）；
- 七阶段各一个 span：retrieve（多路召回分数列表、命中数）、cite、compose、generate（model、prompt、
  completion、token 用量——与 `token_usage_events` 同口径）、persist 等；
- **Agent 模式**：每个工具轮一个 span（工具名、参数摘要、结果引用编号），CitationLedger 的编号映射入 trace；
- **流式**：generation 随 SSE 结束收尾（首 token 延迟、完整输出、用量）；
- **入库任务（第二期候选）**：traceId 随任务 payload 落库，worker 消费时延续同棵树（架构设计 §6.4 的原始意图）；
- **nil-safe**：未配置/`enabled=false` 时 Langfuse 客户端为 None，埋点路径直接短路——零依赖导入成本、零请求开销；
- **失败隔离**：trace 采集统一 try/except 吞掉 + 后台 flush，任何观测异常不允许进入用户响应。

## 4. 明确不做

- 不做 metrics 聚合与日志采集（日志已有结构化 logging）；
- 不在本需求内自建 Langfuse 服务（用云版或用户给的自托管地址）；
- 不做采样率配置（默认全量；量上来再议）；
- 不改问答/入库的功能行为——可观测是纯旁路。

## 5. 风险与坑（提前写明）

| 风险 | 对策 |
|---|---|
| **数据出域**：云版 Langfuse 会拿到用户问题原文/生成答案/检索片段 | 三档可选：①全文上送（默认）；②脱敏（只送分数/元数据/截断摘要）；③自托管（数据不出内网）。**必须由用户选**（§7 Q2） |
| SDK 依赖与体积 | langfuse-python 为纯 Python 轻依赖；未启用时代码路径不 import（懒加载） |
| 流式场景 trace 收尾丢尾 | SSE finally 钩子里 flush；进程退出钩子兜底 |
| 每请求读配置打库 | 复用 OPT-48 的 TTL 缓存模式 + 写时失效 |
| 弱网/自托管不可达 | 客户端超时 + 静默丢弃，绝不重试阻塞主链路 |

## 6. 实施切片（一片一提交，每片 `make server-lint && make server-test`）

| 片 | 内容 |
|---|---|
| N1 | 迁移 `langfuse_config` 表 + 模型/仓储/服务（加密、key_hint、TTL 缓存、审计）+ admin GET/PUT + 错误码/契约登记 + 前端配置卡 |
| N2 | 问答链路 trace：入口生成 traceId → 七阶段 span（召回分数/生成 I/O/用量）→ SSE 收尾 flush |
| N3 | Agent 工具轮 span + CitationLedger 编映射 + 记忆提议入 trace |
| N4 | 入库任务 trace 贯穿（HTTP 头 → 任务 payload → worker 同棵树） |
| N5 | 文档收尾（上线手册 + .env.example 清理）+ 简历回写：项目二恢复"全链路可观测"条目（此时为真） |

## 7. 决策（2026-09-26 用户已拍板）

| # | 决策 |
|---|---|
| Q1 | **自托管**（生产机或内网跑 Langfuse，数据不出域） |
| Q2 | **全文上送**（自托管数据不出内网，观测价值拉满；配三重资源闸见 §7.1） |
| Q3 | 入库任务 trace **不做**（二期再说）；本期只覆盖"问答"链路 |
| Q4 | `APP_OTLP_ENDPOINT` 空配置位**删除**（无代码消费；自托管 SDK 直连不需要 OTLP） |
| Q5 | 配置页 = **superuser 专属**（与模型配置页一致）；Langfuse 面板走其自带账号体系，与 WenQu 权限无关 |

### 7.1 自托管部署与资源闸（回应"全文上送可能内存/资源过多"）

真正的资源大头是 **Langfuse 进程常驻内存**，不是 trace 数据（一条全文 trace ≈ 2-10KB，
个人/演示频率下每月仅 MB 级）。四重闸：

1. **选轻量架构**：用 Langfuse **v2 单容器**（v3 需 ClickHouse+Redis+MinIO 全家桶；v2 只需
   一个容器 + 一个独立数据库），**复用生产机现有 Postgres 实例、新建独立 database `langfuse`**——
   不与 WenQu 主库混用、不新增数据库服务；
2. **容器内存上限**：compose 里 `mem_limit: 512m`（超限只影响观测自身，OOM 不波及 WenQu 主服务）；
3. **单条 trace 体积截断**：prompt/completion/检索片段各设上限（如 8KB/段），超长截断；
4. **保留期**：定期清理 90 天前 trace（个人用量下数据量本就是 MB 级，此项为保险）。

部署位置：生产机 `106.53.41.116`（与 WenQu 栈同机），docker compose 独立项目名，
不与 wenqu 主栈混编排；部署前先向用户出示将跑的容器与资源限制清单。

## 8. 下一步

按 §6 切片开工：N1 配置层（迁移/模型/仓储/服务/admin 端点/前端配置卡）先行，
Langfuse 容器部署在 N2 联调前完成。
