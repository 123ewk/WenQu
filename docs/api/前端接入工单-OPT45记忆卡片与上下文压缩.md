# 前端接入工单:OPT-45 外部事实记忆 + 历史滚动压缩(2026-09-23)

**这份文档自包含**:照它做完即可,不需要先读别的文档。字段权威源是 `docs/api/openapi.json`
(本文已把要用的端点/事件/出参原文抄在下面)。涉及后端实现细节的,一律以"前端会看到什么"为准。

先解释两个本工单里的自造词,免得对着猜:
- **记忆卡片**:AI 在回答里认为"某条结论值得跨会话长期记住"时,前端弹出的一张确认卡,带三个按钮。
- **悬浮窗**:聊天页上下文进度条鼠标悬停时展开的那块读数(复用 OPT-29 已做的进度条)。

---

## 0. 现在能不能动手?**能——但联调要等三道闸**

- **可以立刻开工**:后端 P1–P4 代码、契约、单测/集成测试全部完成并测绿,`docs/api/openapi.json`
  已导出这 7 个端点 + 1 个新 SSE 事件。前端照 §1–§5 的类型和分支写、mock 自测,都不受阻。
- **暂不能真联调**,因为本功能还在功能分支 `feat/opt45-memory-compaction`,**尚未并 main、未部署 dev/prod**:
  - dev 库当前 `alembic current` = **0018**(OPT-41 占的);本功能的新表/新列迁移 `0019 memories` +
    `0020 memory_proposals` + `0021 messages 缓存快照两列` 会在**并 main 时**重挂到 0018 之后。**部署并升库前,直接打这些端点会 404(路由没起)或 500(表/列没建)**。
  - 所以:**mock 阶段照本文契约做;真机验收(§9)要等后端把"已部署 + 已 `alembic upgrade head` + 自检通过"的
    联调解锁单发出来再跑**。别把 dev 上打不通当成契约 bug。

---

## 1. 契约总览(新增 5 个路径 / 7 个方法 + 1 个 SSE 事件)

openapi 规模从 53 路径 / 75 方法 → **58 路径 / 82 方法**。全部走 JWT(`get_current_user`),
路径里带 `{space_id}`,非本人/他空间一律 **404 防枚举**(见 §7)。

| # | 方法 + 路径 | 用途 | 成功码 | 出参 |
|---|-------------|------|--------|------|
| E1 | `POST /spaces/{sid}/memories` | 确认 / 编辑后确认 → 落库 | 201 | `MemoryOut` |
| E2 | `POST /spaces/{sid}/memories/proposals/dismiss` | 点"忽略" → 写去重留痕 | **204**(无体) | — |
| E3 | `GET /spaces/{sid}/memories?limit&offset` | 记忆列表页 | 200 | `MemoryOut[]` |
| E4 | `PATCH /spaces/{sid}/memories/{mid}` | 编辑某条(后端重算向量) | 200 | `MemoryOut` |
| E5 | `DELETE /spaces/{sid}/memories/{mid}` | 删除某条 | **204**(无体) | — |
| E6 | `POST /spaces/{sid}/conversations/{cid}/compress` | 手动压缩按钮 | 200 | `CompressOut` |
| E7 | `GET /spaces/{sid}/conversations/{cid}/context` | 悬浮窗读数 | 200 | `ContextOut` |

新增 SSE 事件:`memory_proposal`(只在 **Agent 模式**产生,见 §2)。

`MemoryOut` 字段:

```jsonc
{
  "id": "uuid",
  "space_id": "uuid",
  "kind": "fact" | "decision",   // fact=客观事实结论;decision=用户拍板的决策
  "content": "记忆正文",          // ≤2000 字
  "topic": "来源主题",            // ≤255,默认取会话标题,可为空串
  "source_conversation_id": "uuid" | null,  // 溯源
  "created_at": "ISO8601" | null
}
```

---

## 2. 记忆卡片 + 三按钮(核心交互)

### 2.1 卡片从哪来:Agent 回答里多一种 SSE 帧

**只在 Agent 模式**,`memory_proposal` 夹在工具轮之间、正文 `delta` 之前。一轮提问的完整事件顺序:

```
meta → [ tool_call → tool_result → (memory_proposal?) ]* → citations → delta* → done
        └── 每次 propose_memory 工具调用后紧跟一条 memory_proposal,可出现多条 ──┘
```

(直检模式顺序不变:`meta → citations → delta* → done`,且**不会**有 `memory_proposal`。)

`memory_proposal` 帧原文:

```jsonc
{
  "type": "memory_proposal",
  "proposal_id": "uuid",              // 每次提议唯一,仅前端本地标 key 用
  "content": "上线窗口定在周五晚",      // 草稿正文(已去多余空白)
  "kind": "decision",                 // fact | decision
  "topic": "发布计划",                 // 可空串
  "content_hash": "64位sha256hex",    // 后端算的去重键,前端只读不回传
  "source": { "conversation_id": "uuid|null", "message_id": "uuid|null" }
}
```

要点:
- **一轮可弹多条**(后端护栏默认每轮最多 3 条,超出的后端直接不发,你收不到即可);
- **只发事件、不落库**:AI 提议 ≠ 已存记忆。用户不点确认,这条什么都不是;
- 事件里的 `content`/`kind`/`topic` 就是要渲染进卡片 + 回填到确认请求的默认值。

### 2.2 三个按钮 → 三个动作(全由前端决定,后端无状态)

| 按钮 | 前端动作 | 打到哪 |
|------|----------|--------|
| **确认** | 把卡片 `content/kind/topic` 原样带上 | `POST /memories`(E1) |
| **编辑后确认** | 用户改了正文再提交 | `POST /memories`(E1),带改后的 `content` |
| **忽略** | 丢弃这张卡 | `POST /memories/proposals/dismiss`(E2) |

E1 请求体:

```jsonc
{
  "content": "上线窗口定在周五晚",        // 必填,1~2000
  "kind": "decision",                     // 可选,默认 "fact"
  "topic": "发布计划",                     // 可选,默认 ""
  "source_conversation_id": "uuid",       // 可选,直接填事件里的 source.conversation_id
  "source_message_id": "uuid"             // 可选,填 source.message_id
}
```

E2(忽略)请求体——**只回传正文和 kind,不要传 `content_hash`**,后端自己按正文重算:

```jsonc
{
  "content": "上线窗口定在周五晚",  // 必填,后端会归一化(压空白)后算哈希
  "kind": "decision",              // 可选
  "conversation_id": "uuid"        // 可选,溯源用
}
```

### 2.3 去重与幂等(为什么用户乱点不会脏)

- **确认落库**:后端按 `(空间, 用户, 正文)` 去重。同一结论连点两次 / 模型重复提议 → 库里只有一条,
  第二次 E1 仍返回 201,但 `id` 和第一次相同(你可以直接用它覆盖本地卡片状态)。
- **忽略留痕**:E2 写一条带 **TTL(默认 7 天)** 的去重记录。之后同一结论的 `content_hash` 命中未过期记录时,
  **Agent 后端就不再把这条 `memory_proposal` 发给你**(弹卡前已跳过)。所以"忽略"是真的能安静一阵,不是骗用户。
  过了 TTL 还想要它会重新弹——正常。E2 幂等:重复忽略同一正文只留一行。
- **确认过 ≠ 还会再提议**:确认走 `memories` 内容去重,与忽略的 TTL 是两套,互不干扰。

### 2.4 直检模式(非 Agent)不会有卡片

只有开了 **Agent(ReAct)模式**的会话才会出 `memory_proposal`。普通直检问答没有工具通道,不弹卡,
前端不需要为直检预留卡片逻辑。**但召回注入两条路径都做**(见 §6),这点前端无感。

---

## 3. 记忆列表页(CRUD)

- **列表 E3**:`GET /spaces/{sid}/memories?limit=50&offset=0`(`limit` 1~200,默认 50;`offset` ≥0)
  → `MemoryOut[]`。**只返回当前登录用户、当前空间的记忆**,不同成员/不同空间互相看不到。
  后端这一版**不回 `total`**,分页就按"拿到不足 `limit` 条即到底"来判最后一页。
- **编辑 E4**:`PATCH /spaces/{sid}/memories/{mid}`,请求体三字段全可选(`content`/`kind`/`topic`),
  只传你要改的。改了 `content` 后端会**重算向量**(影响后续召回)。返回 `MemoryOut`。
- **删除 E5**:`DELETE /spaces/{sid}/memories/{mid}` → 204,无体。
- 列表页 UI(分组、搜索、批量删)由前端自定,后端只给上面这层扁平 CRUD。

---

## 4. 手动压缩按钮

- 调 **E6** `POST /spaces/{sid}/conversations/{cid}/compress`,请求体 `{ "model_id": "可选" }`
  (不指定就用该会话/空间默认 chat 模型)。
- 出参 `CompressOut`:

```jsonc
{
  "compressed": true | false,       // 是否真的写了新摘要
  "summary": "……合并后的前情摘要……",  // 当前生效摘要(compressed=false 时可能是旧值或空串)
  "summary_through_seq": 14,         // seq≤14 的消息已被摘要覆盖
  "reason": ""                       // 未压缩时的原因,见下表;压缩成功时为 ""
}
```

- **`compressed=false` 不是错误,是 HTTP 200**。用 `reason` 分诊给用户看:

| `reason` | 含义 | 前端怎么提示 |
|----------|------|--------------|
| `""` | 成功(`compressed=true`) | 刷新悬浮窗读数 |
| `too_short` | 未压缩的消息条数不够(默认 <8 条) | "对话还太短,暂不需要压缩" |
| `nothing_to_fold` | 够长但全落在"最近保留条数"内,没有可折叠的旧消息 | "没有可压缩的历史了" |
| `disabled` | 后端把压缩开关关着 | 隐藏压缩按钮(见 §8) |
| `empty_summary` | 模型返回了空摘要(异常兜底) | "压缩暂时没成功,稍后再试" |

- **压缩是"叠加摘要",原文一律不删**(B6)。用户要回看被压缩掉的旧消息,前端照常读 `messages` 端点即可,
  不需要为压缩加任何"历史归档"特殊处理。再点一次压缩:从 `summary_through_seq` 之后继续滚动,幂等向前。

---

## 5. 上下文悬浮窗(进度条 hover 读数)

调 **E7** `GET /spaces/{sid}/conversations/{cid}/context` → `ContextOut`:

```jsonc
{
  "used_tokens": 5200 | null,        // 复用 OPT-29 快照:上一条带齐用量 assistant 的 prompt+completion
  "window_tokens": 64000 | null,     // 同一次调用解析到的模型窗口
  "in_scope_memory_count": 1,        // 本用户本空间已存记忆条数
  "compressible_messages": 6,        // 压缩点之后、还能折叠进摘要的旧消息条数(不含最近保留的 N 条)
  "can_compress": true,              // 现在点压缩会不会真有效果
  "has_summary": false,              // 该会话此前是否已有摘要
  "cache_supported": true,           // 上游模型是否回报了缓存命中(见下)
  "cache_hit_rate": 0.769            // cached/prompt,3 位小数;不支持时为 null
}
```

口径与渲染:
- **进度条**:直接 `used_tokens / window_tokens`,配色/三档/隐藏规则**完全沿用 OPT-29 那套**(整行隐藏 / 只显文本 / 画条三态不变);
  这里只是把同一个分子分母从 `done` 帧换成刷新时读 E7,值同源。
- `used_tokens`/`window_tokens` **可能同时为 null**(首问、本轮没打模型、或 0015 前的老会话)→ 走 OPT-29 的"整行隐藏"分支。
- `can_compress=false` 时把压缩按钮**置灰或隐藏**;`compressible_messages` 就是告诉用户"点了大概能折几条",
  可作按钮角标。`in_scope_memory_count` 可在悬浮窗显示"本空间已存 N 条记忆",点击跳 §3 列表页。
- **缓存命中率(OPT-45 P-cache)**:命中率 = 末次 chat 调用 `cached_tokens / prompt_tokens`,由后端在
  每次回答落库时存成当轮快照,前端**只在刷新后读 E7**(不进 SSE `done` 帧,流式过程中不实时爬升)。
  渲染两分支:
  - `cache_supported=true` → 显示"缓存命中 XX%"(`cache_hit_rate` 已是 0~1 的小数,乘 100);支持但一次没命中会是 `0.0`,**这是正常值不是 bug**;
  - `cache_supported=false`(`cache_hit_rate=null`)→ 显示"当前模型暂不支持缓存命中统计"。**别把 null 当 0%**,也别自己估。

---

## 6. 召回注入对前端透明(了解即可,不用写代码)

用户确认过的记忆,会在**后续每次提问**时由后端按相关性(embedding 向量 top-K)捞几条塞进系统提示词——
直检和 Agent 两条路径都注入。**前端不需要为此改任何东西**:召回为空/关掉记忆开关时,问答行为与现在逐帧一致。
唯一可感知的:长对话/换会话时,AI 会"记得"之前确认过的结论。记忆是**快照事实**,资料更新后可能过期,靠 §3 的 CRUD 纠正。

---

## 7. 错误码与隔离(全部按现有 `{ success:false, error:{ code, message } }` 信封)

| HTTP | `error.code` | 触发 |
|------|--------------|------|
| 401 | `AUTH_REQUIRED` | 没带/带错 token |
| 404 | `SPACE_NOT_FOUND` | 路径 `space_id` 不是你的空间 / 你不是成员(记忆相关读写)|
| 404 | `CONVERSATION_NOT_FOUND` | `/compress`、`/context` 的会话不属于你或不在该空间 |
| 404 | `MEMORY_NOT_FOUND` | `PATCH`/`DELETE` 的 `memory_id` 不是你的(与"不存在"同码,防枚举)|
| 400 | `VALIDATION_ERROR` | 正文为空、超 2000 字等 |
| 403 | `FORBIDDEN` | 角色权限不足(仅空间成员关系层面) |

**跨用户/跨空间一律 404,绝不返回 403 泄漏"存在但无权限"**——和 `messages` 路由同规则。前端把 404 统一当"这条不存在/无权访问"处理即可,不要据码区分。

---

## 8. 后端配置开关会改变前端看到的行为(前端要能优雅降级)

这些是后端 `APP_*` 配置,前端拿不到值,但要能应对它们被关:

- **记忆总开关关** → Agent 回答里**不会再有 `memory_proposal` 帧**;§3 CRUD 仍可用(已有记忆还能列/删)。
- **压缩总开关关** → E6 恒返回 `reason:"disabled"`;E7 的 `can_compress` 恒 `false`。
  → 前端**别把压缩按钮写死常亮**,依 §4/§5 的 `reason`/`can_compress` 动态决定显隐。

---

## 9. 最小改动清单(前端)

1. 重导 openapi 类型:新增 `MemoryOut`/`CompressOut`/`ContextOut` 三模型 + 7 端点;SSE 事件联合类型加 `memory_proposal`。
2. **Agent 回答渲染层**:解析 `memory_proposal` → 弹卡片,三按钮接 E1/E1/E2(§2)。
3. **记忆列表页**:E3/E4/E5 三件套(§3)。
4. **压缩按钮**:接 E6,按 `compressed`/`reason` 给反馈(§4);点了之后顺手刷一次 E7。
5. **悬浮窗**:接 E7,进度条逻辑复用 OPT-29,补 `in_scope_memory_count`/`compressible_messages`/`can_compress`
   三个读数与按钮态,以及缓存命中率两态显示(`cache_supported`? 显 `cache_hit_rate`% : 显"不支持")。

---

## 10. 验收步骤(顺序即依赖;每条标在验什么)

> 前提:后端已发出"已部署 + `alembic upgrade head`(到 0021)+ 自检通过"的解锁单。**没解锁就只跑 mock,不跑真机。**

1. `GET /openapi.json` 里含 §1 的 7 条路径与 `ContextOut`/`CompressOut`/`MemoryOut` schema,且 `ContextOut` 有
   `cache_supported`/`cache_hit_rate` 两字段 → 验连的是新进程;
2. dev 库 `alembic current` 到本功能 head(含 `memories`/`memory_proposals` 两表 + messages 缓存两列)→ 没升的话 §3/§4 会 500;
3. 开 Agent 模式问一轮,让 AI 出一条结论 → 收到 `memory_proposal` → 点确认 → E1 返回 201,列表页 E3 能看到这条;
4. 换一个新会话问相关问题 → AI 回复体现"记得"该结论(召回注入,§6);
5. 同一结论再让 AI 提议 → 直接点忽略 → E2 返回 204;同一会话再问 → **不再弹这张卡**(TTL 去重生效);
6. 连点两次"确认"同一正文 → E3 列表仍只有一条(id 相同)→ 验内容去重;
7. 陌生人拿你空间里某 `memory_id` 调 PATCH/DELETE → 404 `MEMORY_NOT_FOUND`,且你那条没被动;
8. 长对话点压缩 → `compressed=true`、`summary_through_seq` 前进 → 再查 E7 `has_summary=true`、`can_compress` 变 `false`;
9. 短对话(<8 条)点压缩 → `compressed=false, reason="too_short"`,HTTP 仍 200;
10. 悬浮窗:配了窗口值的模型问一轮 → E7 的 `used/window` 与 OPT-29 `done` 帧同源一致;首问老会话 → 双 null 走隐藏分支。
11. 缓存命中率(**真机以厂商是否回 `cached_tokens` 为准,当前无法本地验证**):用一个会回缓存的模型问一轮 →
    刷新后 E7 `cache_supported=true` 且 `cache_hit_rate` 在 0~1;用不回该字段的模型 → `cache_supported=false`、
    `cache_hit_rate=null` → 悬浮窗显"当前模型暂不支持缓存命中统计"。**mock 阶段两态都构造一遍即可,真机数值等解锁后核。**

---

## 11. 报告格式(数字/事件类不符必须附这些)

- SSE:原始帧全文(尤其 `memory_proposal`,含 `content_hash`);
- REST:`POST/PATCH` 的原始请求体 + 原始响应体(不要转述);
- 隔离类:两条不同登录态各自请求 + 各自响应,证明"看不到对方的";
- 不贴 `.env`、不贴明文 Key/token 明文。

---

## 12. 已含的限制 / 本期明确不做(不用等、别自己加)

- **缓存命中率(P-cache)已含,但有两条边界**:① 只在**刷新后**读 E7,不进 SSE `done` 帧,流式中不实时爬升;
  ② 命中率依赖上游回 `cached_tokens`,回不了的模型一律 `cache_supported=false`(前端显"不支持",别当 0%)。
  字段名后端已做多写法容错(OpenAI/百炼/Anthropic),但**真机是否真回、回的确不确定,需解锁后实测**。
- **工具结果缓存**:另开片,本期无。
- **跨空间/团队共享记忆**:记忆只 `(space, user)` 双维度隔离,不做空间级共享。
- **API Key 调用产生记忆**:解析不到具体 user 的 Key 调用 → 不召回、不提议、不落库,本工单端点也不面向 Key(仅 JWT)。
- **压缩审计快照表**:本期只把摘要写 `Conversation.meta`,原文不删;要历史快照表另议。
