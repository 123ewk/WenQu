# OPT-45 设计实现文档：外部事实记忆 + 历史滚动压缩

> 状态：**待评审**（本文档评审通过后再开工）。负责人：后端。前端仅按后续 `docs/api` 契约对接，本文档不含前端实现。
> 迁移落点：本分支 alembic head = `0016`；若合入的 OPT-41（0017/0018）已并 main，本功能的 memory 表迁移号顺延到真实 head 之后，下文统一记为 `001X`。

## 1. 背景与目标

现状只有一种"记忆"：**会话内短期记忆**——每次提问把当前会话**最近 10 条**消息原样拼进上下文（`loop.py:44`、`qa_pipeline.py:32` 的 `_MAX_HISTORY_MESSAGES`），超出部分直接丢，无摘要、无跨会话沉淀。

痛点：这是 RAG 系统，用户会依据内部资料和 AI 回复做出判断、得出结论。换会话窗口或上下文被压缩后，这些"会话级/成员级的事实性结论与决策"就丢了，交流无法延续。

目标（两件事一起做）：
1. **外部事实记忆**：把值得长期保留的决策/事实结论，经**用户确认后**沉淀到库里，跨会话按相关性召回注入。
2. **历史滚动压缩**：把窗口内被"挤出去"的旧消息滚动摘要，用"摘要 + 最近若干条"替代现在的"最近 10 条硬截断"，让长对话不失忆、可控 token。

## 2. 需求边界（已拍板决策）

| # | 决策点 | 结论 |
|---|--------|------|
| B1 | 记忆归属/可见范围 | **用户在某空间内跨会话**可见（`space_id` + `user_id` 双维度隔离）；不同成员、不同空间互不可见 |
| B2 | 记忆产生方式 | **AI 自动提议 + 用户确认**（不做用户显式指令那一套，也不做压缩时顺带提炼） |
| B3 | 记忆召回方式 | **向量相关性检索**（复用现有 pgvector `Vector(1024)`，非全量注入） |
| B4 | 本次是否含压缩 | **含历史滚动压缩** |
| B5 | 压缩触发 | **手动按钮 + 接近上限时自动兜底** |
| B6 | 被概括掉的原文消息 | **只存摘要，原文全部保留**（可逆、可回看、可审计；压缩是"叠加摘要"而非删除） |
| B7 | 压缩与事实记忆的关系 | **完全独立**（压缩不自动弹记忆卡片） |
| B8 | 已存记忆管理 | **含基础 CRUD 接口**（列表/改/删），前端列表页由前端实现 |

### 边界假设（默认按此实现，用户可推翻）
- 三按键 = **确认（直接落库）/ 编辑后确认（改内容再落库）/ 忽略（前端丢弃，后端不留痕、不落库）**。
- **自动提议仅在 Agent（ReAct）模式生效**；直检流水线模式不产生提议（它没有工具通道）。但**召回注入两条路径都做**。
- 忽略不占后端存储；落库时按 `(space_id, user_id, content)` 去重，防用户连点/模型重复提议。
- 压缩后拼上下文从"最近 10 条全部历史"改为"**摘要 + 压缩点之后的最近 N 条原文**"。

## 3. 与现状的区别、效果、代价（大白话）

**记忆**
- 区别：现在完全不留跨会话的东西；加了一层"用户确认过的记忆库"，提问时按相关性捞几条塞进提示词。
- 效果：换会话也能接上之前的结论/决策，减少"复读机式重述背景"。
- 代价/风险：① 每次提问多一次 embedding 调用 + 一次向量检索（延迟 + 少量成本）；② 记忆是"快照事实"，资料更新后可能过期，靠用户 CRUD 纠正；③ 多一张表、多一套隔离谓词，漏过滤 = 跨用户泄密（下面 §7 专门防）。**会不会影响现有运行**：不注入记忆时行为与现状一致，召回为空即回退，不阻断问答。

**压缩**
- 区别：现在是"砍到最近 10 条"，第 11 条以前彻底看不见；改成"旧消息滚动摘要 + 保留最近 N 条原文"。
- 效果：长对话不丢主线，token 可控，进度条能真实反映占用。
- 代价/风险：① 摘要是 AI 生成的，可能丢细节或有幻觉，**原文全保留**就是为此兜底（可随时回看、重算）；② 自动兜底会在生成前多一次摘要调用（仅在超阈值那一次触发）；③ 改变了默认上下文组装逻辑，需要回归测试钉住短对话（未触发压缩时）行为不变。

## 4. 数据模型

### 4.1 新表 `memories`（迁移 `001X`）
```
id                    UUID pk
space_id              UUID fk→spaces  ondelete=CASCADE  index
user_id              UUID fk→users    ondelete=CASCADE  index
kind                  VARCHAR(16)   'fact' | 'decision'   default 'fact'
content               TEXT          记忆正文(可编辑)
topic                 VARCHAR(255)  来源会话主题快照(默认取 conversation.title)
source_conversation_id UUID fk→conversations ondelete=SET NULL nullable  溯源
source_message_id      UUID nullable                            溯源
embedding             VECTOR(1024)  召回向量(用空间层 embedding key 生成)
created_at / updated_at
```
- 隔离复合索引：`ix_memories_space_user (space_id, user_id)`。
- 向量索引：与 `chunks` 表同款（HNSW/IVFFlat + cosine，参照 `models.py:242`）。
- **不加**全文 tsv 列（本期纯向量召回，够用；需要再加）。

### 4.2 会话摘要：复用 `conversations.meta` JSONB，**不新增列、不加表**
```jsonc
// Conversation.meta
{
  "summary": "……滚动摘要正文……",
  "summary_through_seq": 42,        // seq<=42 的消息已被摘要覆盖
  "summary_updated_at": "ISO8601",
  "summary_message_count": 42
}
```
- 原文消息一律不动（B6）。要"回看被压缩的历史"，前端直接读 `messages` 表即可。
- 若后续需要压缩审计，可另加 `conversation_summaries` 历史快照表（本期不做）。

### 4.3 新表 `memory_proposals`（迁移 `001X`，仅存 dismissed，用于去重再提议）
```
id            UUID pk
space_id      UUID fk→spaces ondelete=CASCADE  index
user_id       UUID fk→users  ondelete=CASCADE  index
conversation_id UUID fk→conversations ondelete=CASCADE nullable
content_hash  VARCHAR(64)   归一化内容的 sha256(去重键)
kind          VARCHAR(16)
created_at    timestamptz   server_default now()
expires_at    timestamptz   created_at + APP_MEMORY_PROPOSAL_DISMISS_TTL
```
- 索引：`ix_mprops_lookup (space_id, user_id, content_hash, expires_at)`。
- 只在用户点"忽略"时写一行 `status=dismissed`（不设独立 status 列——表内只放 dismissed）。
- 提议前查：命中 `(space_id,user_id,content_hash)` 且 `expires_at>now()` → 跳过，不再弹卡片。
- 过期行靠查询条件自然失效；可择期加一个清理任务，本期不做（量小）。

## 5. 后端架构与改动点

### 5.1 记忆模块
- **领域**：`domain/models.py` 加 `Memory` 模型；`domain/interfaces.py` 加 `MemoryRepository`、`MemoryQueryRepository` 协议。
- **仓储**：`application/repository/memories.py`：`add / list_for_actor(space_id,user_id,分页) / get / update / delete / search_by_embedding(space_id,user_id,vector,top_k,min_sim)`。所有查询强制带 `space_id AND user_id`。
- **服务**：`application/service/memory.py` `MemoryService`：
  - `save_confirmed(...) -> Memory`（算 embedding + 去重落库）
  - `recall(space_id, user_id, question) -> list[Memory]`（embed 问题 → 向量 top-K）
  - `list/update/delete`（B8 CRUD）
- **ReAct 提议工具**：`application/agent/tools.py` 新增 `propose_memory` 工具（`READ_ONLY=False` 但**无副作用**）：参数 `content/kind/topic`。循环里识别到该调用即 `emit("memory_proposal", {...})`（带 `proposal_id`、草稿、元数据），**不落库**、不计入检索预算。上限 `APP_MEMORY_PROPOSAL_MAX_PER_TURN` 护栏，超出丢弃并记日志。`loop.py` 系统提示补一条"何时提议"的规则。
- **召回注入**：
  - 直检：`qa_pipeline.py` 在 `compose` 前加阶段 `recall_memory`，把命中记忆拼进 system 段的"已确认相关记忆"块。
  - Agent：`AgentRunner.run()` 组装初始 messages 时，同样注入召回块。
  - 二者都受 `APP_MEMORY_ENABLED` 控制，召回为空即与现状完全等价。

### 5.2 压缩模块
- **服务**：`application/service/compaction.py` `CompactionService`：
  - `compress(conversation_id) -> CompactResult`：取 `seq > summary_through_seq` 的消息；不足 `APP_COMPACT_MIN_MESSAGES` 则返回"无需压缩"；调 `ChatGateway` 把"旧摘要 + 新消息"合成新摘要，写回 `meta`。返回 `{summary_through_seq, summary, est_used, est_window}`。
  - `build_context(history, meta) -> list[Message]`：给 qa/agent 用——返回 `[摘要代表消息?] + 压缩点之后最近 APP_COMPACT_KEEP_RECENT 条`（替代现在直接 `history[-10:]`）。摘要作为一条 system 附加段拼入，不作为 Message 落库。
- **自动兜底**：`qa.ask_stream` 生成前，若估算上下文 > `window * APP_COMPACT_TRIGGER_RATIO`，先**同步** `compress()` 再走原流程（仅这一次额外调用，用户这一问略慢但绝不超限）。**阈值 = 上下文窗口总量的 80%**。
- **上下文悬浮窗数据（字段口径待 §9-Q2 定稿）**：扩展消息/会话出参或新增 `GET /conversations/{id}/context`：返回 `used_tokens`、`window_tokens`（已有 OPT-29 快照列）、`in_scope_memory_count`、`compressible_messages`、`can_compress`。**注**：`缓存命中` 现无数据源——`model_usage.py` 仅记 `prompt_tokens/completion_tokens`，未记 `cached_tokens`；工具调用亦无结果缓存。是否纳入本期见 §9-Q2（推荐 A：本期不展示缓存命中）。

### 5.3 API 端点（`api/routes/`）
```
POST   /memories                         确认落库(proposal 草稿/编辑后内容)
POST   /memories/proposals/dismiss       忽略留痕(写 dismissed 行,供去重再提议)
GET    /memories?space_id=               列表(分页,仅本人本空间)
PATCH  /memories/{id}                    编辑(重算 embedding)
DELETE /memories/{id}                    删除
POST   /conversations/{id}/compress      手动压缩
GET    /conversations/{id}/context       上下文/压缩状态(进度条悬浮窗)
```
均走 `get_current_actor` + `_require_member`；非本人/他空间一律 404（防枚举，与 `messages` 路由同规则）。

### 5.4 SSE 事件（新增）
`memory_proposal`：`{proposal_id, content, kind, topic, content_hash, source:{conversation_id,message_id}}`，随 `done` 前发出。前端渲染卡片 + 三按钮；确认/编辑走 `POST /memories`（带 `proposal_id`），忽略走 `POST /memories/proposals/dismiss`（写 dismissed 行）。

## 6. 端到端数据流

**写入（提议→确认）**：用户在 Agent 会话提问 → 循环判定某结论值得记 → `propose_memory` → `emit memory_proposal` → 前端卡片 → 用户点确认/编辑 → `POST /memories` → embed → 去重 → 落 `memories`。

**召回注入**：新会话/后续提问 → `MemoryService.recall(space,user,question)` 向量 top-K → 拼进 system"相关记忆"块 → 生成。

**压缩**：进度条接近上限或用户点压缩 → `compress()` 滚动摘要写 `meta` → 后续 `build_context` 用"摘要 + 最近 N 条"。

## 7. 隔离与安全
- 一切记忆读写以 `(space_id, user_id)` 为谓词，仓储层不提供"无归属"查询入口。
- API Key 调用（OPT-25/41）：能解析到具体 user 才启用记忆；不能解析到 user 的调用 → 不召回、不提议（不落任何长期记忆）。
- 记忆内容进上下文时按工具消息同款"不可信包裹/长度截断"处理，避免历史记忆里的注入指令被执行。
- embedding 走空间层 key（与 `chunks` 同路径，`deps.py:268`），dev 才回落全局。

## 8. 配置项（`core/config.py`）
```
APP_MEMORY_ENABLED=true
APP_MEMORY_RECALL_TOP_K=5
APP_MEMORY_RECALL_MIN_SIM=0.30
APP_MEMORY_PROPOSAL_MAX_PER_TURN=3
APP_MEMORY_PROPOSAL_DISMISS_TTL_DAYS=7 # dismissed 去重窗口
APP_COMPACT_ENABLED=true
APP_COMPACT_TRIGGER_RATIO=0.8      # 自动兜底阈值(占窗口比例)
APP_COMPACT_KEEP_RECENT=6          # 压缩后仍原样保留的最近消息条数
APP_COMPACT_MIN_MESSAGES=8         # 低于此条数不压缩
```

## 9. 决策定稿（2026-09-23 用户拍板）
- **Q1 忽略=留痕 + TTL**：dismiss 落 `memory_proposals` 表（见 §4.3），带 `expires_at`。用途=**去重再提议**：模型再想提同一结论时，若命中未过期的 dismissed 记录（按 `content_hash`）就跳过，不再骚扰用户。确认过的走 `memories` 内容去重。
- **Q2 缓存命中=放到最后的不确定需求**：模型层一般会返回命中缓存的 token 数，命中率我们自己算（`cached_tokens / prompt_tokens`）。**开工顺序**：先把 §11 P1–P4 的确认小功能做完并测绿，再单开一片做缓存——届时用真实字段（`model_usage.py` 现仅解析 `prompt_tokens/completion_tokens`，需补 `usage.prompt_tokens_details.cached_tokens`，实际字段名以真机响应为准）透传+落库+算率；**若上游确实不返回，悬浮窗显示"当前模型暂不支持缓存命中统计"**，不硬造数字。
- **Q3 自动兜底**：生成前**同步压缩**，阈值 **80% 窗口总量**（见 §5.2、§8）。

## 10. 测试与验收门槛
- 单元：`MemoryService`（去重/召回谓词/CRUD 隔离）、`CompactionService`（阈值/摘要写回/`build_context` 条数）。
- 契约回归：现有 SSE 事件形态测试不破坏；`APP_*` 关时问答行为与现状逐事件等价（钉住"最近 10 条"回退）。
- 隔离测试：跨用户/跨空间的 `search_by_embedding`、`list`、`update`、`delete` 一律不可见/不可操作（404）。
- 迁移：`001X` 升降级可逆；`memories.embedding` 维度与 KB 侧一致（1024）。
- 手工：Agent 会话触发一次提议→确认→新会话召回命中；长对话手动压缩后进度条回退、原文仍可回看。

## 11. 实施分期（每片独立可验证、一片一提交）
> 顺序铁律（用户拍板）：**P1–P4 确认小功能全部做完并测绿之后，才碰"复杂不确定"的缓存片（P-cache）**。P4 的悬浮窗本期只放有数据支撑的字段。

- **P1 记忆底座**：`memories` 表 + 迁移、领域/仓储/`MemoryService`、CRUD 端点、单元与隔离测试。
- **P2 提议与召回**：`propose_memory` 工具 + `memory_proposal` SSE；`memory_proposals` dismissed 表 + `POST /memories/proposals/dismiss`；两路径召回注入 + 配置；契约回归。
- **P3 压缩底座**：`meta` 摘要 + `CompactionService.compress` + `POST /compress` + 生成前同步兜底(80%) + `build_context` 接管上下文组装。
- **P4 上下文悬浮窗数据**：`GET /context` = 使用率/本范围记忆条数/可压缩数/能否压缩（复用 OPT-29 快照列）。**不含缓存命中**。
- **P5 前端契约文档**：`docs/api/` 出"OPT-45 前端契约"（卡片、三按钮含忽略留痕、CRUD、压缩、悬浮窗）。
- **P-cache（最后，不确定）**：`model_usage.py` 解析真实 `cached_tokens` 字段 → 透传+落库 → `GET /context` 补 `cache_hit_rate=cached/prompt`；上游无该字段则返回 `cache_supported=false`，前端显示"当前模型暂不支持缓存命中统计"。工具结果缓存若要做另开片。

---
> 边界与 §9 决策已定稿。下一步：从 P1 开工，逐片提交、每片过门禁（ruff + 类型 + pytest）。
