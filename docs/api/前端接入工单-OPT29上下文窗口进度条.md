# 前端接入工单:OPT-29 聊天页上下文窗口进度条(2026-09-21)

**这份文档自包含**:照它做完即可,不需要先读别的文档。字段权威源是 `docs/api/openapi.json`
(§1 已把要用的部分原文抄在下面)。

## 0. 现在能不能动手?**能写代码,不能联调**

- 后端代码 + 契约 + 实测都已完成,全在本地 `main`(`1e9a7cb`/`c7c601e`/`5f681da`/`afd11b7`),**未推送**;
- 三道闸**一条都没解除**:① 推送 ② 重启 server + worker ③ dev 库 `alembic upgrade` 到 0015;
- 所以:**静态接入照做、`vue-tsc`/`vite build` 照跑,但不要开任何联调单/beta bug**;
  解锁前你测进度条只会看到"整行隐藏"(旧进程没有这两个字段),那是正确 fallback,**不算通过**。
  怎么判断自己连的是旧进程:`curl :8000/openapi.json` 数 `paths`,**52 才对**(旧进程 39)。

## 1. 契约(新增的只有两个字段,SSE 与 REST 各一处)

**规模不变:52 路径 / 74 方法。** 没有任何新端点、新事件类型、新错误码。

### 1.1 SSE `done` 帧(`/api/v1/spaces/{sid}/ask` 与续流 `.../stream?after=`)

```jsonc
{
  "type": "done",
  "message_id": "00a3f7eb-58a7-4130-b4ec-244bea69868d",
  "cited_indexes": [1, 2, 3],
  "context_used_tokens": 845,      // ← 新增 int | null
  "context_window_tokens": 64000,  // ← 新增 int | null
  "seq": 276
}
```

- 一轮回答的事件顺序仍是 `meta → citations → delta* → done`(Agent 模式中间插 `tool_call`/`tool_result`);
- **`partial: true` 的 done 帧同样带这两值**(断线宽限截止那一轮);
- 续流重放出来的 done 帧 payload 与原始那帧逐字节相同,不需要特殊处理。

### 1.2 `GET /api/v1/spaces/{sid}/conversations/{cid}/messages` 的每条消息

`MessageOut` 属性清单(★=本轮新增):

```
id, role, content, seq, citations, model_id, agent_steps,
★ context_used_tokens: int|null, ★ context_window_tokens: int|null, created_at
```

- **只有 assistant 行可能有值**;user 行恒为 null(后端只写 assistant 行);
- 0015 之前的历史消息两值恒 null(不回填)。

## 2. 这两个数的口径(为什么和你直觉不一样)

| | 口径 |
|---|---|
| 分子 `context_used_tokens` | 本轮**最后一次**发给上游的 chat 调用 `prompt+completion`。**多轮不累加** |
| 分母 `context_window_tokens` | **那一次调用实际解析到的** chat 模型的 `context_tokens`(空间层配模型时必填;全局层历史值可空) |

三个"不是 bug"的点,先说清免得你排查半天:

1. **Agent 模式数字明显小于"感觉该花的量"**:一次提问可能打 3 次上游,每轮都在重发同一份上下文,
   累加会报到超出窗口,所以只取最后一次。想知道"这轮花了多少 token"去看用量页(OPT-28),那是账,不是仪表。
2. **看不出是实测还是估算**:厂商回传 usage 就用精确值,不回传才本地估(汉字 0.75 / 其他非空白 0.25,向上取整)。
   **契约里没有 `context_usage_estimated`,也不打 ≈** —— 你们 2026-09-21 的裁决,后端按此实现。
   dev 用 deepseek 会回传,所以你们 mock 里全是精确值属正常,**别拿 dev 的绿当"估算分支测过了"**。
3. **分母是快照不是现算**:同一条消息刷新后永远是当轮那个数,即使模型配置后来被改了/那个模型被删了。
   设计如此(存快照才能让"刷新页面进度条复原"读到当轮真实数字)。

## 3. 渲染分支(判定顺序就照这个写)

```ts
type Window = { used: number | null; window: number | null };

function renderBar({ used, window }: Window) {
  if (used === null && window === null) return hide();          // 整行隐藏:兜底回答 / 老消息 / 本轮没打模型
  if (used === null && window !== null) return reportBug();     // 后端不会产出,出现即后端 bug
  if (window === null) return showTextOnly(used);               // "窗口未配置",只显示已用,不画条
  return showBar(used, window, used / window);
}
```

- 百分比 = `used / window`,配色沿用你们已实现的三档:<70% 蓝、70–90% 黄、>90% 红 + "接近上限,建议新开会话";
- 数据源优先级:本轮 `done` 帧 → 刷新后用"该会话最后一条 assistant 消息"的同名两字段;
- **流式过程中不要实时爬升**(V1 口径,防误导),每轮 done 后刷新一次;
- **上游失败时只有 `error` 帧、没有 `done` 帧**(生成阶段异常不发 done,也不落库),
  此时**保持上一轮的进度值别清零**,不要显示 0。

## 4. 最小改动清单(前端)

1. `MessageOut` 类型加两个 `number | null` 字段(openapi 重导即可带上);
2. SSE `done` 事件解析加这两值;
3. 聊天页按 §3 分支渲染(整行隐藏 / 只显文本 / 画条三态);
4. 会话切换、刷新页面、断线续流三条路径都要能复原同一对数字 —— 三条路读的是同一份 payload,
   不要各自加工。

## 5. 解锁后的验收(顺序即依赖,每条标了在验什么)

1. `curl :8000/openapi.json | jq '.components.schemas.MessageOut.properties | keys'` 含两字段 → 验**连的是新进程**;
2. dev 库 `alembic current` = **0015**(否则 `/ask` 写消息会 500,现象是回答能流出来但刷新丢消息);
3. 空间配一个**填了 `context_tokens`** 的 chat 模型 → 问一轮 → done 帧两值都有 + 刷新后 messages 同值
   (同时验"取最后一次"与"落库一致");
4. 换走全局默认模型(`context_tokens` 为空)再问 → `used` 有值、`window` null → "窗口未配置";
5. 问一个知识库查不到的问题 → 兜底回答 → done 帧双 null → **整行消失**;
6. 开 Agent 模式问一轮 → 数字 ≈ 最后一次调用(远小于累加值);
7. 断线不重连等宽限截止 → `partial=true` 的 done 帧两值仍在,刷新后 messages 读到同一对数;
8. 找一个 0015 之前的老会话 → 双 null → 整行消失(不报错、不显示"窗口未配置")。

## 6. 报告格式(数字类不符必须附这两样)

- `done` 帧原始 payload 全文 + `GET .../messages` 原始响应体(不要转述);
- 后端侧同轮 `token_usage_events` 查询结果由我们取,你只要给 `message_id` 和大概时刻;
- 不贴 `.env`、不贴明文 Key。

## 7. 顺带提醒(现状,非本批引入)

走"空间没配模型 → 全局默认兜底"这条链时,`MessageOut.model_id` 是 **null**
(它记的是"请求里显式指定的模型",不是"实际解析到的模型")。
**窗口大小只读 `context_window_tokens`**,别拿 `model_id` 去 `GET /models` 清单里反查。

## 8. 还没做的相邻事项(不用等)

- **OPT-32**:生成阶段失败的 SSE `error` 帧**只有 `message`、没有 `code`**(检索阶段的 `error` 倒是带),
  所以上游挂了你只能按文案兜底展示。修它属 SSE 形态变更,要先发契约变更说明,与进度条无关,不阻塞本工单。
