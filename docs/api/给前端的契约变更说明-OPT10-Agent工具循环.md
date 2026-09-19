# 契约变更说明:OPT-10 Agent 工具循环(给前端)

> **一句话:`POST /ask` 新增可选 `agent: boolean`(默认 false = 现行行为不变);
> 开启后 SSE 流新增 `tool_call` / `tool_result` 两种事件,对话页工具卡第一次有了真实数据源。
> 其余事件(meta/citations/delta/done/error)形态不变,续流协议不变。**
> 机器契约以 `docs/api/openapi.json` 为准(随落地提交重导);本文讲契约看不出的部分。
> 触发方式经产品确认为**请求级可选**——默认关,前端加开关,不用的人成本零变化。

---

## 1. 请求侧:`agent` 参数

```json
POST /api/v1/spaces/{sid}/ask
{
  "question": "报销制度是什么?",
  "conversation_id": null,     // 不变
  "kb_ids": [],                // 不变(工具检索范围 = 本次请求范围,模型不能扩)
  "top_k": 6,                  // 不变(作为 search_knowledge 的默认上限参考)
  "model_id": null,            // 不变
  "agent": true                // ★ 新增,可选,默认 false
}
```

- `agent: false`(缺省):与今天逐字节相同 —— 直检管线、事件序列、落库全不变;
- `agent: true`:走 ReAct 工具循环 —— 模型自主决定调几次 `search_knowledge` 再作答。

## 2. SSE 新事件(两种,均在 `agent: true` 时出现)

事件仍走统一壳 `data: {...}` + 单调 `seq`,**续流自动兼容**(按 seq 补播即可,新事件无需特殊处理):

```json
data: {"type":"tool_call","id":"call_0","name":"search_knowledge","args":{"query":"报销上限","top_k":5},"seq":12}
data: {"type":"tool_result","id":"call_0","name":"search_knowledge","ok":true,"duration_ms":180,"output":"[3] 来源:报销制度.pdf\n…(截断)","seq":13}
```

| 事件 | 时机 | 载荷 | 前端动作 |
|---|---|---|---|
| `tool_call` | 模型发出调用意图,**执行前** | `id`(本轮内唯一)、`name`、`args`(对象) | 工具卡渲染"正在检索:query"(args.query) |
| `tool_result` | 执行完成后 | `id`(对应 tool_call)、`name`、`ok`、`duration_ms`、`output`(截断后文本) | 工具卡落定该次结果 |

- `id` 把 tool_call 与 tool_result 配对;一轮可能多个调用(当前仅一个工具,但按多调用设计);
- `output` 是**给用户看的截断摘要**(单结果 ≤2000 字符);不要拿它当全文数据;
- **失败不发 `done` 的规则不变**;工具执行失败是 `tool_result`(`ok:false`)+ 循环继续,
  只有检索/模型整体失败才走既有 `error` 事件。

## 3. `citations` 与 `done`:形态不变,时机微调

- agent 模式下,循环里所有 `search_knowledge` 命中去重(同父块只留一条,沿用 OPT-4 规则)
  后编成**全局编号**;`citations` 事件在**最终回答开始流式之前**发一次(载荷结构与现行完全一致);
- `done` 仍带 `message_id` + `cited_indexes`(语义不变);`partial` 语义不变(断线宽限照旧);
- 无命中兜底变化:agent 模式**没有**固定兜底文案 —— 系统提示词要求模型"检索无相关内容时
  如实说明",所以会收到正常的 `delta` + `done`(citations 可能为空数组),前端按现有
  "citations 空"分支渲染即可。

## 4. 落库:工具卡在 F5 之后也有真实数据

助手消息新增**可选**字段 `agent_steps`(迁移 0011,JSONB,可空):

```json
"agent_steps": [
  {
    "round": 0,
    "thought": "需要查报销上限的具体数字",
    "tool_calls": [
      {"id":"call_0","name":"search_knowledge","args":{"query":"报销上限","top_k":5},
       "ok":true,"duration_ms":180,"output_preview":"[3] 来源:报销制度.pdf\n…"}
    ]
  }
]
```

- `GET .../conversations/{cid}/messages` 的 `MessageOut` 增加可选 `agent_steps`
  (直检模式的消息恒为 null/缺省 —— 前端判空回退现有 citations 合成逻辑);
- `output_preview` 同样是截断展示文本,不是全文。

## 5. 护栏(成本与行为上界,供前端文案参考)

- 循环轮数上限 **20**(可配 `APP_AGENT_MAX_ITERATIONS`),到顶自动收尾作答;
- 单工具结果回填 ≤2000 字符,整个请求的工具输出总量有预算上限 —— 触顶后工具返回
  "预算已用尽,请基于已有资料作答";
- 空响应重试 2 次、连续 2 轮复读自动终止 —— 这些都是**自动收尾**,不是错误,
  前端无需为它们做特殊 UI(正常 delta/done 流)。

## 6. 前端要做的(汇总)

1. `AskRequest` 类型增 `agent?: boolean`;对话页加开关(默认关);
2. SSE 分派器增 `tool_call` / `tool_result` 两个分支(工具卡;没有工具卡 UI 时可先忽略);
3. `MessageOut` 增 `agent_steps?`(判空回退现有合成逻辑);
4. agent 模式下"citations 为空数组"是正常路径(模型如实说明),不要当错误渲染;
5. 续流、心跳、partial、error 处理全部不变。

## 7. 后端落地序列(每步独立提交)

工具接口与注册表 → 模型客户端 function calling → ReAct 循环 → 工具输出防护 →
问答接入(迁移 0011)→ 契约重导 + 护栏测试 → 台账同步。
落地完成后本说明与 openapi.json 一起复核;有出入按对接标准 §2 以实测为准写回 §11。
