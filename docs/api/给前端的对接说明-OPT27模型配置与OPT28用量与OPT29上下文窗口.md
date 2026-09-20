# 给前端的对接说明:OPT-27 模型配置页 / OPT-28 Token 用量 / OPT-29 上下文窗口(2026-09-20)

**状态:v2.1 —— OPT-27 已落地、dev 环境实测通过并合入契约**(本文 §1 已按**实际实现**逐字段核对过,
与 v1 草案的差异都在 §1 各表里改到位;§1.8 是可直接照抄的调用序列)。
v2.1 新增(实测后的两处口径补充):`api_key`/`base_url` 的 **ASCII + 无空白** 写入口径与前端预校验建议
(§1.3"字符口径"、§1.2、§1.7 的 422 行);§1.8 按实际 `response_model` 订正了"哪些写接口只回 `{ok:true}`、
存 Key/拨开关后必须重拉配置页"。
契约基线:**50 路径 / 72 方法**(前值 39/59;本轮全部加法式),权威源 `docs/api/openapi.json`,
字段/枚举以它为准;流程与错误口径另见 `docs/api/m4-模型管理-接口说明.md` §6。
OPT-28(§2)与 OPT-29(§3)**尚未开工**,那两节仍是计划稿,按 OPT-27 → 28 → 29 顺序各自合并后重导契约。

用户 2026-09-20 二轮裁决已落进实现:rerank 入口 + 启用开关;embedding 回落全局 Key 仅限开发环境,
生产必须空间自配。实施计划见 `docs/优化实施计划-OPT27-29.md`。

角色口径:"成员身份 ≥30" = 空间角色 **Admin 及以上**(Role.ADMIN=30,与 OPT-26 建 API Key 同级)。

> **本轮唯一会牵动既有页面的语义变化(OPT-27)**:`GET /api/v1/models` 的**形状逐字段不变**,
> 但内容变成"**当前活动空间的合并清单**"(空间自配模型遮蔽同名全局模型;prod 下没配向量化 Key
> 的空间,`embedding` 数组为空)。切空间后要重新拉一次清单。前端模型选择器无需改代码,
> 但**必须能展示"清单为空"**并给"去模型配置页"的引导。

---

## 1. OPT-27 模型配置页(新页面,建议路由 `/space/:sid/settings/models`)

### 1.1 页面结构(四个区块)

```
┌ 空间设置 › 模型配置 ────────────────────────────────┐
│ ① Embedding(向量化)卡片 —— 模型不可换,只填 Key        │
│ ② 对话模型(chat)供应商列表 —— 卡片式,可增删改 + 测试   │
│ ③ 对话模型列表 —— 表格,挂在供应商下,含默认标记/窗口大小  │
│ ④ Rerank(重排)区块 —— 启用开关 + 供应商/模型(同②③形状)│
└──────────────────────────────────────────────┘
```

- 区块①③④常驻;非 Admin 成员进页面 = 只读(所有写操作按钮隐藏,输入框禁用);
  **读接口对成员一律放行**,所以只读页面不需要任何 403 兜底逻辑;
- 所有 Key 字段**永远只回显掩码**(如 `sk-a****g7h8`),保存后不再回传明文;编辑时留空 = 不修改;
- 响应里的 `providers[]` / `models[]` 就是 `GET /spaces/{sid}/model-config` 的两个数组,
  **只含本空间层的行**(不含全局清单);模型选择器要的全局+空间合并清单走 `GET /api/v1/models`,两者别混用。

### 1.2 区块①:Embedding 卡片(内置模型 + 官网链接 + 只配 Key)

展示(数据来自 `GET /spaces/{sid}/model-config` 的 `embedding` 对象):

| 展示项 | 来源字段 | 说明 |
| --- | --- | --- |
| 模型 | `embedding.model_name` | 固定 `text-embedding-v3`(内置,不可改) |
| 维度 | `embedding.dims` | 固定 1024;文案注明"换模型会导致已入库向量失效,故锁死" |
| 官网 | `embedding.doc_url` | `https://bailian.console.aliyun.com/`,新开页,引导用户去建 API Key |
| 状态 | `embedding.key_configured` | 未配置时红色警示"空间尚未配置向量化 Key,上传入库不可用" |
| Key 掩码 | `embedding.key_hint` | 已配置时展示,形如 `sk-a****g7h8`(**4+****+4**,后端就是这个字符串,直接显示);未配置为空串 |
| 是否在用兜底 | `embedding.fallback_active` | 仅 dev 可能为 true(空间没配 Key 但全局 Key 顶上),显示灰色提示"当前使用平台全局向量化 Key(仅开发环境)";prod 恒 false |

> 后端**不会告诉前端当前是不是 prod**。判据是 `fallback_active`:prod 下它永远是 false,
> 所以"未配 Key"的红色警示在 prod 一定成立、在 dev 可能被灰色提示替代。前端按这两个字段分支即可,
> 不要自己去猜环境。

交互(Admin+ 才可用):
1. 输入框填 Key → 先点【测试】:`POST /spaces/{sid}/model-config/embedding-key/test`,
   入参 `{"api_key": "sk-…"}`(不传则测已存 Key);返回 `{ok:true}` 或 424(见 §1.7);
   Key 的**字符口径与 §1.3 相同**(只收不含空白的 ASCII,首尾空白后端自动去掉,否则 422);
2. 点【保存】:`PUT /spaces/{sid}/model-config/embedding-key`,同上传参;
   **空串 = 清除本空间 Key**(二次确认弹窗;清除后:生产环境向量化直接不可用,
   开发环境会回落运维全局 Key,页面文案按 `GET model-config` 返回的 `fallback_active` 区分);
3. **环境差异(重要)**:生产环境空间**必须自配** Key,未配置时 `key_configured=false`
   → 红色警示"空间未配置向量化 Key,上传入库与检索不可用,请联系空间管理员";
   开发环境未配置会静默回落全局 Key(`fallback_active=true`),页面显示灰色提示即可。

### 1.3 区块②:chat 供应商(用户自配 base_url)

新建供应商弹窗字段:

| 表单字段 | 入参名 | 必填 | 校验 | 说明 |
| --- | --- | --- | --- | --- |
| 供应商类型 | `provider_type` | ✅ | 值 ∈ `GET .../model-config/types` 的 `key` | **下拉只有名字**(DeepSeek/阿里云百炼/智谱/Kimi/豆包/MiniMax/OpenAI 兼容自建/Ollama/vLLM + 4 个重排类型);选中后用其 `default_base_url` 预填下一栏,允许改 |
| 显示名 | `name` | ✅ | **本空间层内**唯一(与全局同名不算冲突,且会遮蔽全局那条) | 列表展示用,也是 `model_id` 的前缀来源 |
| Base URL | `base_url` | ❌ | 留空 = 用该类型的 `default_base_url`;填了必须 http(s),且**只能是不含空白的 ASCII**(见下方"字符口径");prod 另有一道 SSRF 闸门(见 §1.7) | 用户自己填,后端不校验厂商白名单 |
| API Key | `api_key` | ❌ | 空 = 免密钥本地端点(Ollama/vLLM);同样**只收不含空白的 ASCII** | 明文只在这一跳出现,落库即加密 |
| 启用 | `enabled` | ❌ | 默认 `true` | 关掉后其模型从 `GET /models` 清单消失 |

- 创建:`POST .../model-config/providers` → **201**;编辑:`PATCH .../providers/{provider_id}`
  (`api_key` 不传=不改、空串=清除;**协议不可改**,类型定了就锁死);
  删除:`DELETE` → 204(级联删其下模型,需二次确认"该供应商下 N 个模型将不可用");
- 每张卡片有【测试连接】:`POST .../providers/{provider_id}/test` → 用已存配置发最小上游请求
  (取该供应商下**最早**的一个模型);该供应商还没有模型时后端给 **422**,
  文案"该供应商下还没有模型,请先添加模型再测试连接" —— 建议前端把卡片上的测试按钮
  在 `model_count === 0` 时禁用并提示这句话,别等报错。

**字符口径(实测踩过的坑,前端必须按这个做)**:`api_key` 与 `base_url` 只接受
**不含任何空白的 ASCII**;**首尾空白后端会自动去掉**(粘贴带换行是最常见错误),
但中间的空格/制表符/中文会直接 **422**,消息里含"ASCII"。所以:

1. 前端**不要**自己 `trim()` 后再判断为空——纯空白后端等价于空串(= 清除),行为一致,
   但请保持"填空 = 清除"的文案与二次确认;
2. 中文域名(如 `https://中文.example.com/v1`)要先转 punycode,否则 422;
3. 建议在输入框 `blur` 时做一次本地正则预校验(`/^[\x21-\x7e]*$/`,即"可见 ASCII、无空格"),
   命中就内联提示,不要等 422 再 toast;
4. 供应商 `name` 与 `model_name` **允许中文**,别用同一条规则校验。

之所以卡这么严:脏字符入库后,调用时在 HTTP 客户端层编码失败 = 检索链路 500,
用户在页面上只会看到"内部错误",排查成本极高(本次 dev 实测就是这么撞上的)。

### 1.4 区块③:chat 模型列表

| 列 | 来源 | 备注 |
| --- | --- | --- |
| 行主键 | `id` | **PATCH/DELETE/test 的路径参数用它**(文档里写作 `{model_pk}`),不是 `model_id` |
| 模型 ID | `model_id` | 对外 id(进 `GET /models` 与 `/ask` 的那个);**本空间层内唯一**,与全局同名 = 遮蔽全局那条 |
| 模型名 | `model_name` | 实际调上游用的名字,如 `deepseek-chat` |
| 供应商 | `provider_name` | 关联区块② |
| 类型 | `kind` | `chat` / `rerank`(空间层没有 embedding) |
| 上下文窗口 | `context_tokens` | **chat 新建必填**(缺了 422),OPT-29 进度条的分母;rerank 不需要 |
| 默认 | `is_default` | 同空间同 kind 仅一个默认(后端互斥,新的顶掉旧的);问答未指定模型时用它 |
| 启用 | `enabled` | 关掉 = 从 `GET /models` 清单消失,且不能被设成默认 |
| 操作 | — | 编辑/测试/删除(Admin+) |

新建模型字段:`{provider_id, kind, model_name, model_id?, context_tokens?, enabled?, is_default?}`。

- `kind` 必须与所选供应商的协议匹配:`*_rerank` 类型(protocol=`cohere_rerank`)只能挂 rerank,
  其余类型只能挂 chat;搞错给 422(消息会点名"该供应商是重排协议/对话协议"),
  所以**区块②③的供应商下拉与区块④的下拉要按 `/types` 的 `kinds` 字段分开渲染**;
- `model_id` 不传时后端合成 `{供应商 name}/{model_name}`(如 `deepseek/deepseek-chat`),
  前端默认不填、把合成结果回显即可;要自定义再传,撞库给 409 `MODEL_ID_TAKEN`;
- 编辑(`PATCH /models/{model_pk}`)只开放 `model_name`/`context_tokens`/`enabled`/`is_default`
  —— `provider_id`、`kind`、`model_id` 建好即锁死(改这些等于换模型,前端做"删除后重建");
- 【测试模型】`POST /models/{model_pk}/test`:chat 发一条 `ping` 取首个增量,rerank 试排两条短文档,
  成功 `{ok:true}`,失败 424(见 §1.7)。

### 1.5 区块④:Rerank 重排(开关 + 模型,按需启用)

| 元素 | 数据 | 交互 |
| --- | --- | --- |
| 启用开关 | `rerank.enabled`(bool,默认 false) | Admin+ 可拨;`PUT .../model-config/rerank-switch` 入参 `{"enabled": true}` → `{ok:true}` |
| 当前默认 | `rerank.default_model`(string\|null) | 有值才说明"开了真会用";null 且开关为 true → 黄色提示 |
| 开关旁文案 | — | "开启后,检索结果会经由重排模型重新排序;关闭时不参与链路。上游故障时自动降级为不重排,不影响问答" |
| 供应商/模型 | 与区块②③同款形状,`kind="rerank"` | 复用同一套 providers/models 端点;**只能选 `/types` 里 `kinds` 含 rerank 的 4 个类型**(阿里云百炼重排 / SiliconFlow 重排 / Jina 重排 / Cohere 重排,protocol 固定 `cohere_rerank`);**无需 context_tokens**;模型名如 `gte-rerank-v2` |
| 未配模型就开开关 | — | 后端允许开启但检索时无默认 rerank 模型 = 等同关闭;建议前端在开关打开且无默认模型时给黄色提示 |

注意:开关与模型都属空间级配置;`GET /models` 公开清单里的 `rerank` 数组会包含本空间配置的
rerank 模型(形状不变),前端模型选择器现逻辑无需改动。重排只在**该空间**开着时生效,
且是检索链路的固定挂点(融合后取 top-30 候选重排),**没有暴露给 `/ask` 请求参数**,前端不用传任何东西。

### 1.6 表单预置值建议(前端写死即可,后端不拦)

`context_tokens` 快捷选项:4096 / 32768 / 65536 / 128000,或直接输入。
**但预填值请从 `GET .../types` 的 `default_base_url` 取**(后端维护,已含下表这些),
前端不必再抄一份硬编码;下表只是给人看的说明:

DeepSeek `https://api.deepseek.com/v1`;百炼 `https://dashscope.aliyuncs.com/compatible-mode/v1`;
智谱 `https://open.bigmodel.cn/api/paas/v4`;Kimi `https://api.moonshot.cn/v1`;
豆包 `https://ark.cn-beijing.volces.com/api/v3`;MiniMax `https://api.minimax.chat/v1`;
OpenAI 兼容自建 `https://api.openai.com/v1`;Ollama `http://127.0.0.1:11434/v1`;
vLLM `http://127.0.0.1:8000/v1`;四类重排见 §1.5。

本地类型(Ollama/vLLM)的默认地址是 `127.0.0.1` —— **开发环境可用,生产会被 §1.7 的
"地址不安全" 422 拒掉**。建议在 `label` 后加"（本地/开发环境）"小字,免得用户在生产里反复撞。

### 1.7 错误码展示口径

| code | HTTP | 前端处理 |
| --- | --- | --- |
| AUTH_REQUIRED | 401 | 未登录(带 API Key 不带 Bearer 也是这个:本组端点只认 JWT) |
| SPACE_NOT_FOUND | 404 | 非本空间成员与"空间不存在"同壳;统一"资源不存在",不区分无权限 |
| FORBIDDEN | 403 | 不该出现(按钮已按角色隐藏);兜底 toast"需要空间管理员权限" |
| VALIDATION | 422 | **直接展示后端 `error.message`**(已按场景写清:未知类型 / kind 与协议不匹配 / chat 缺 `context_tokens` / 供应商下无模型 / **模型服务地址不安全:…** / **只能是不含空格的 ASCII 地址…**),不要按 code 归并成一句"参数错误" |
| MODEL_PROVIDER_NAME_TAKEN / MODEL_ID_TAKEN | 409 | 表单内联报错"该名称已存在"(注意唯一性只算本空间层) |
| UPSTREAM_MODEL_ERROR | 424 | 【测试连接】失败,`details.reason` ∈ `auth`/`model_name`/`network`,分别提示"Key 无效或权限不足 / 模型名不存在(检查上游的模型/接入点 id) / 网络不通或 base_url 有误"(后端 message 就是这三句,可直接用)。注:`network` 是**兜底桶**,除 DNS/拒绝连接/超时外,上游回任意其它状态码也归它,此时后端 message 会变成"上游返回异常(status=…)"—— 照抄 message 显示即可,不要自己改写原因 |
| MODEL_NOT_CONFIGURED | 503 | 问答页既有链路不变;配置页"测向量化 Key 但一把 Key 都没有"也是它 |

生产环境额外一条要给用户的解释:SSRF 闸门只判**写入时**,已经存下的地址不会被回溯;
真要清历史内网地址由运维处理(前端不需要做检测)。

### 1.8 最小调用序列(照抄可用,Admin 用户视角)

```text
GET    /api/v1/spaces/{sid}/model-config            # 进页面:一次拿全四区块
GET    /api/v1/spaces/{sid}/model-config/types      # 渲染供应商下拉(含 kinds/预填值)

PUT    /api/v1/spaces/{sid}/model-config/embedding-key          {"api_key":"sk-…"}
POST   /api/v1/spaces/{sid}/model-config/embedding-key/test     {"api_key":"sk-…"}   # 不传=测已存 Key

POST   /api/v1/spaces/{sid}/model-config/providers  → 201
       {"provider_type":"deepseek","name":"我的深度求索","base_url":"","api_key":"sk-…"}
POST   /api/v1/spaces/{sid}/model-config/models     → 201
       {"provider_id":"<上一步 id>","kind":"chat","model_name":"deepseek-chat","context_tokens":64000,"is_default":true}
POST   /api/v1/spaces/{sid}/model-config/providers/{provider_id}/test   # 或 models/{model_pk}/test

PUT    /api/v1/spaces/{sid}/model-config/rerank-switch          {"enabled":true}
```

**响应体分两类,别一视同仁**(实做核对过 `routes/space_models.py` 的 `response_model`):

- `POST/PATCH /providers` → 供应商行(含 `key_hint` 掩码)、`POST/PATCH /models` → 模型行:
  **局部更新即可**,不必重拉;但 `is_default` 会顶掉同 kind 的旧默认,设默认后要刷新列表才能对齐两行状态;
- `PUT /embedding-key`、`PUT /rerank-switch`、以及所有 `*/test` → 只回 **`{"ok": true}`**(契约里的 `OkOut`)。
  所以**存完 Key / 拨完开关要重新 `GET /model-config`** 才能拿到新的 `key_configured`/`key_hint`/`rerank.enabled`
  —— 别在本地自己拼掩码,掩码只由后端给。

---

## 2. OPT-28 Token 用量页(区块建议挂在 空间设置 › 用量,或成员列表侧栏)

### 2.1 按成员汇总表(Admin+)

`GET /spaces/{sid}/token-usage?period=30d`,数据形状:

```json
{
  "period": "30d", "generated_at": "…",
  "space_total": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
  "members": [
    {"user_id": "…", "username": "…", "nickname": "…", "role": 30,
     "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
     "chat_calls": 0, "embedding_calls": 0}
  ]
}
```

- 表格列:成员 / 对话次数 / 输入 token / 输出 token / 合计 / 向量化调用;按合计降序;
- 时间筛选:7d / 30d / 全部;
- 口径提示文案(必须放):"统计含对话与向量化;个别供应商不回传精确用量,此时为后端估算值"
  (响应行里有 `source: upstream|estimated`,可打 `≈` 角标)。

### 2.2 我的用量(所有成员)

`GET /spaces/{sid}/token-usage/me` → 单人对象,同款字段;普通成员只有这个入口。

---

## 3. OPT-29 上下文窗口使用率(聊天页,进度条)

### 3.1 数据来源(两处,前端都只读不算)

1. **实时**:SSE `done` 帧新增 `context_used_tokens`、`context_window_tokens`(int|null);
2. **刷新复原**:`GET /conversations/{id}/messages` 的 assistant 消息带同名两字段
   → 进度条按"该会话最后一条 assistant 消息"渲染。

### 3.2 渲染规则

```
上下文  12,400 / 64,000 tokens  ▓▓▓▓▓▓░░░░ 19%
```

- 百分比 = used / window,窗口取所选 chat 模型配置的 `context_tokens`(见 1.4);
- 任一值为 null(模型没配窗口 / 老消息)→ 只显示已用 tokens,不画进度条,显示"窗口未配置";
- 阈值配色建议:<70% 蓝、70–90% 黄、>90% 红 + 提示"接近上限,建议新开会话";
- 更新时机:每轮回答 done 后刷新;流式过程中不做实时爬升(V1 口径,防误导)。

---

## 4. 联调与落地节奏

- **OPT-27 已交付**(契约 50 路径 / 72 方法,已重导 `docs/api/openapi.json`),前端可以开工配置页;
- OPT-28(§2)与 OPT-29(§3)随后各自独立合并,合并时以重导契约为准并在本文勘误,
  路径/方法基线数会出现在这一节;
- 三批全部为**加法式变更**:既有 `GET /models`、`/admin/*`、API Key、SSE/问答链路契约不变;
  语义变化只有一处对前端透明 —— 模型解析多了"空间优先、全局兜底"(见本文开头的提示框:
  `GET /models` 内容随活动空间变化,切空间要重拉)。
- 需要前端配合的只有两件事:① 新页面(配置页);② 模型选择器**要能渲染空清单**并引导去配置页
  (prod 下未配向量化 Key 的空间,`GET /models` 的 `embedding` 就是空的)。
