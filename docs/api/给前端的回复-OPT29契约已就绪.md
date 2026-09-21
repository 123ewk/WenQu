# 给前端的回复:OPT-29 进度条契约已就绪(代码+文档+实测齐全),联调仍等三道闸(2026-09-21)

接续《给前端的回复-OPT27-28落地与联调门槛.md》。对应你们 2026-09-21 的
《给后端的回复:OPT-29 决策答复与静态对表完成》§4 里等的两样东西
(该文件在你们仓库 `web-l0` 已提交,本仓库里是未跟踪副本,故不列路径链接):
**① 29b~29e 完成;② 对接说明 §3 勘误 + "契约已就绪"回复**。两样都在这里了。

## 1. 结论先说

- **进度条契约已冻结可接**:`done` 帧与 `MessageOut` 各加 `context_used_tokens` /
  `context_window_tokens` 两个 `int|null`,**没有** `context_usage_estimated`(你们 §1 裁决已采纳);
- **契约规模不变:52 路径 / 74 方法**。三项全是字段级加法(SSE `done` +2、`MessageOut` +2、
  `messages` 表 +2 列)。所以**没有新页面、没有新端点、没有新错误码** —— 你们只在聊天页加一条进度带;
- **但"契约就绪"≠"可联调"**:代码全在本地 `main`,**未推送、未上 dev**。你们那三道解锁条件
  (推送 / 重启 server+worker / dev 库 `alembic upgrade` 到 0015)**一条都没解除**,
  推送需用户点头。解除前照旧**不开任何联调单**,§3 那套静态对表继续有效。

## 2. 提交与门禁(可复核)

| 切片 | 提交(本地 `main`) | 内容 |
|---|---|---|
| 29a | `86b835d` | 迁移 0015 + 域模型两列(数据层) |
| 29b | `1e9a7cb` | 取值链路:`ChatTurnUsage` 收集器挂在 OPT-28 计量外壳上 + `deps` 每请求一份 + `done` 帧与 assistant 行同落两值 |
| 29c | `c7c601e` | `MessageOut` 透出两值 + `openapi.json` 重导(仅此文件,+22 行 schema) |
| 29d | `5f681da` | 测试:真 PG 集成 8 例 + 断线 partial 走真计量外壳 1 例 |
| 29e | 本提交 | 文档同步(实施计划 F3 as-built / 台账 §1+§1.1 / V1 台账 §3+§4 / 架构设计 §5 / 对接说明 §3 勘误 + 本文) |

门禁实跑:全量 pytest **396 passed**(311 单测 + 85 集成)、`ruff check .` 0 错、
`mypy src` 0 错(74 文件)、契约 drift 测试绿(提交里的 openapi 与代码一致)、
`scripts/e2e_smoke.py` **21/21**、迁移 **0015 在一次性容器上实测升→降→再升**(两列 `integer` 可空,降回 0014 消失,再升恢复)。

## 3. 你们要的 null 组合,后端逐条钉住了

| used | window | 什么时候出现 | 钉住的测试 |
|---|---|---|---|
| 有值 | 有值 | 正常一轮(窗口已配置) | `test_done_frame_carries_last_call_numbers` |
| 有值 | null | 该 chat 模型 `context_tokens` 留空(**窗口未配置**) | `test_window_null_when_model_has_no_context_tokens` |
| null | null | 兜底回答(没检索结果、没打模型)/ 老消息 / 续流之前的历史 | `test_fallback_answer_has_no_pair` |
| null | 有值 | **后端不会产出**,出现请当 bug 报给我们 | —(设计上不可能:两值同源同一次赋值) |

补充三点你们会撞到的:

1. **`partial=true` 的 done 帧同样带两值**,与完整回答同一分支(你们已按此实现,确认无误);
2. **续流重放带两值**:`GET .../stream?after=<seq>` 补出来的 `done` 事件 payload 与原始那帧逐字节相同,
   刷新页面走 `messages`、断线补流走 `stream`,两条路读到的数字必须一致;
3. **实测 vs 估算在契约里不可见**,但**分子可能来自本地估算**(厂商不回 `usage` 时)。
   dev 用 deepseek 会回传,所以你们 mock 里看到的都是精确值属正常 —— 别拿 dev 的绿当这条逻辑测过了。

## 4. dev 实测取证(真 server 进程 + 真 deepseek 上游)

跑法:dev 库的**一次性副本**升到 0015(不解锁 dev 库本身),`:8012` 起真 uvicorn,
拿既有空间真问一轮,再给全局 chat 模型补上 `context_tokens=64000` 问第二轮。

```
# A. 窗口未配置(dev 默认态:全局层历史模型 context_tokens 为空)
{"type":"done","message_id":"00a3f7eb-…","cited_indexes":[1,2,3],"context_used_tokens":847,"context_window_tokens":null,"seq":278}
db:  assistant | 847 | None      user | None | None

# B. 窗口已配置 64000
{"type":"done","message_id":"7715cab9-…","cited_indexes":[1,2,3],"context_used_tokens":845,"context_window_tokens":64000,"seq":276}
db:  assistant | 845 | 64000     user | None | None
```

交叉核对:`token_usage_events` 同轮 chat 行 = `prompt 572 + completion 273 = 845`,
与分子**逐项相等**;embedding 行(6 tokens)不参与分子;user 行两值恒 null。
`GET .../messages` 返回的最后一条 assistant 消息两值与 done 帧完全一致(845 / 64000)。

**给你们的一个提醒(现状,非本批引入)**:走"空间没配 → 全局默认模型兜底"这条链时,
`MessageOut.model_id` 是 **null**(它记录的是"请求里显式指定的模型",不是"实际解析到的模型")。
窗口大小请**只读 `context_window_tokens`**,别去 `models` 清单里按 `model_id` 反查 —— 反查会拿不到。

## 5. 台账编号撞车已按你们 §3 处理

SSE `error` 缺 `code` 那条已改号 **OPT-32**(提交 `41a1fe6`),OPT-31 归你们的"API Key 生效分配"。
那条待办照旧存在、仍属 SSE 形态变更(动手前先发契约变更说明),与进度条无关,不阻塞你们。

## 6. 解锁后请这样验(顺序即依赖)

1. `curl http://127.0.0.1:8000/openapi.json` 数 `paths` = **52**,且
   `components.schemas.MessageOut.properties` 里有 `context_used_tokens` / `context_window_tokens`
   —— 没有就是**还在跑旧进程**,后面的实测全部作废;
2. dev 库 `alembic current` = **0015**(否则 `/ask` 会在写消息时 500);
3. 配一个**带窗口**的空间 chat 模型(§1.4 表单里 `context_tokens` 必填)→ `/ask` 一轮 →
   断言 `done` 帧两值 + 刷新后 `messages` 同值(这一条同时验"分子取最后一次调用"和"落库一致");
4. 把该模型 `context_tokens` 改掉或换全局兜底 → 新问一轮 → 进度条走"窗口未配置"(used 有值 / window null);
5. 问一个知识库里查不到的问题 → 兜底回答 → **整行消失**(双 null);
6. 开 Agent 模式问一轮 → 数字应 ≈ 最后一次调用,**明显小于**"每轮累加"的量级(累加是错的);
7. 断线不重连(等宽限截止)→ `partial=true` 的 done 帧两值仍在,刷新后 `messages` 读到同一对数。

报告按你们 §6 格式即可(不贴 .env、不贴明文 Key;数字类不符请附 `messages` 原始响应体 + 后端侧同轮
`token_usage_events` 查询结果,我们照这两样对账)。
