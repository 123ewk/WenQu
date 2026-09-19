# 给前端的回复:OPT-10 Agent 工具循环已落地,可以接开关了

> 对应公告:`docs/api/给前端的契约变更说明-OPT10-Agent工具循环.md`(下称"契约说明")。
> **一句话:后端 8 步全部落地并推送 `main`(`cc4b084` 接入 + `534f60c` 集成测试),
> `docs/api/openapi.json` 已重导(含 `agent` 与 `agent_steps` 字段),前端现在可以开工。**
> 日期:2026-09-19。

## 1. 落地与验证结论(实测,可复核)

| 承诺 | 实测结论 |
|---|---|
| `agent` 缺省 = 逐字节不变 | 直检事件序列在集成测试中与 OPT-5 交付时逐帧比对通过;端到端冒烟 21/21(真 PG+MinIO+parser)仍全绿 |
| `agent: true` 事件序列 | 真 `/ask` SSE 集成实测:`meta → tool_call → tool_result → citations → delta… → done`,seq 单调,续流按 seq 补播自动兼容(无需改动) |
| `agent_steps` 落库 | 迁移 **0011**(契约说明原笔误"0012",已修正)真库实测升→降→再升;JSONB 往返经 messages API 回读验证;直检消息恒为 `null` |
| 失败语义 | 工具执行失败只发 `tool_result`(`ok:false`)循环继续;整体失败仍走既有 `error` 事件;护栏触顶(轮数/复读)是**自动收尾**不是错误,正常 `delta/done` |

## 2. 接入清单(与契约说明 §6 一致,按优先级)

1. `AskRequest` 类型增 `agent?: boolean`(types 重导即可,契约只加法式变更);
2. 对话页加 Agent 开关,**默认关**——不拨开关的用户零感知;
3. SSE 分派器增 `tool_call` / `tool_result` 两分支:按 `id` 配对渲染工具卡,
   `args.query` 做"正在检索:…"文案,`output` 只做**截断展示文本**,不要当全文数据;
4. `MessageOut` 增 `agent_steps?`:F5 后按轨迹回放工具卡;为 `null` 时回退现有
   citations 合成逻辑(现有代码不用动);
5. agent 模式下 `citations` 为**空数组是正常路径**(模型如实说明没找到),
   不要进错误分支。

## 3. 联调建议

- 冒烟顺序:先开开关发一条会命中文档的提问(应看到至少一对 tool_call/tool_result),
  再问一个知识库里没有的问题(应看到 citations 为空的正常回答);
- 断线重连:agent 流与直检流同一套续流协议,`GET .../stream?after=<seq>` 不用特殊处理新事件;
- 轮数上限 `APP_AGENT_MAX_ITERATIONS`(后端默认 20),对前端无契约影响,仅长任务预期。

## 4. 口径

机器契约以重导后的 `docs/api/openapi.json` 为准;本文与实测不一致处,按对接标准 §2
以实测为准并回写公告。有问题在本目录下回帖即可。
