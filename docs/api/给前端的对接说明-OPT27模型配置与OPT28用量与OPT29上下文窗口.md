# 给前端的对接说明:OPT-27 模型配置页 / OPT-28 Token 用量 / OPT-29 上下文窗口(2026-09-21)

**状态:v3.2 —— 按你们《给后端的回复:OPT-27/28/29 联调报告》处置回写**:F2 是后端真 bug 已修
(**百炼重排走原生协议 `dashscope_rerank`,`/types` 里该类型的 `protocol`/`default_base_url` 值变了**,
见 §1.5/§1.6;其余三个重排类型不变),F1/F3 是口径没写清(已在 §1.4 补 PATCH 三态与探活 body),
F2 后半"重排生效不可观测"登记为台账 OPT-34(设计如此,不是缺陷)。**契约路径/方法数不变(52/74)**,
`/types` 是数据不是 schema。上一版 v3.1:OPT-27 / OPT-28 / OPT-29 三项均已落地并合入契约(§1、§2、§3 已按
**实际实现**逐字段核对过,与计划稿的差异都在各节里改到位;§1.8 / §2.4 是可直接照抄的调用序列)。
契约基线:**52 路径 / 74 方法**(OPT-27 后 50/72,OPT-28 新增 2 路径 / 2 方法,
**OPT-29 只加字段、路径数不变**,全部加法式),
权威源 `docs/api/openapi.json`,字段/枚举以它为准;
流程与错误口径另见 `docs/api/m4-模型管理-接口说明.md` §6、§7。
OPT-29(§3)**已完成**:`done` 帧与 `MessageOut` 的两值都已发出、契约已重导、dev 真跑取证,
按你们 §1 的裁决**没有**加 `context_usage_estimated`。三批代码(含 OPT-27/28)已推送 `origin/main`,
且 **dev 已于 2026-09-21 13:43 完成"重启 server+worker"+"升库到 0015"两道闸 —— 联调已解锁**
(重启会作废已发 token,前端需重新登录,这是预期不是 bug);当前进度与该测什么,见
`docs/api/给前端的回复-OPT29契约已就绪.md` 与可直接执行的
`docs/api/前端接入工单-OPT29上下文窗口进度条.md`。

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

- `kind` 必须与所选供应商的协议匹配:`/types` 里 `kinds` 含 `rerank` 的类型(重排协议,现两种
  `cohere_rerank`/`dashscope_rerank`)只能挂 rerank,其余类型只能挂 chat;搞错给 422(消息会点名
  "该供应商是重排协议/对话协议"),所以**区块②③的供应商下拉与区块④的下拉要按 `/types` 的
  `kinds` 字段分开渲染**(判据用 `kinds`,别判 `protocol` 等值 —— 重排协议有两个值);
- `model_id` 不传时后端合成 `{供应商 name}/{model_name}`(如 `deepseek/deepseek-chat`),
  前端默认不填、把合成结果回显即可;要自定义再传,撞库给 409 `MODEL_ID_TAKEN`;
- 编辑(`PATCH /models/{model_pk}`)只开放 `model_name`/`context_tokens`/`enabled`/`is_default`
  —— `provider_id`、`kind`、`model_id` 建好即锁死(改这些等于换模型,前端做"删除后重建");
- **PATCH 的三态口径(F1,请按这条实现)**:字段**缺省**与**显式传 null 都是"不改"**(后端逐字段
  `if x is not None` 判定)。所以 `context_tokens` **一经建立无法用 API 清除** —— 想让某模型回到
  "窗口未配置"态只能删了重建(且 chat 新建必填,重建也填不回空值)。"窗口未配置"因此只会出现在
  **全局层**(运维建全局 chat 模型时留空,用户 2026-09-20 裁决允许);空间层的这条分支按你们
  已有的实现保留即可,dev 上用 API 造不出来属预期,不是前端缺陷;
- 【测试模型】`POST /models/{model_pk}/test`:chat 发一条 `ping` 取首个增量,rerank 试排两条短文档,
  成功 `{ok:true}`,失败 424(见 §1.7)。探活三个端点里**只有 `embedding-key/test` 有请求体**
  (openapi 标 `required`,空体 422 `Field required: body`,发 `{}` 或 `{"api_key":null}` 都可);
  `providers/{pk}/test` 与 `models/{pk}/test` **没有 body**,发不发都行 —— 前端统一发 `{}` 是最省事的写法;

### 1.5 区块④:Rerank 重排(开关 + 模型,按需启用)

| 元素 | 数据 | 交互 |
| --- | --- | --- |
| 启用开关 | `rerank.enabled`(bool,默认 false) | Admin+ 可拨;`PUT .../model-config/rerank-switch` 入参 `{"enabled": true}` → `{ok:true}` |
| 当前默认 | `rerank.default_model`(string\|null) | 有值才说明"开了真会用";null 且开关为 true → 黄色提示 |
| 开关旁文案 | — | "开启后,检索结果会经由重排模型重新排序;关闭时不参与链路。上游故障时自动降级为不重排,不影响问答" |
| 供应商/模型 | 与区块②③同款形状,`kind="rerank"` | 复用同一套 providers/models 端点;**只能选 `/types` 里 `kinds` 含 rerank 的 4 个类型**(阿里云百炼重排 / SiliconFlow 重排 / Jina 重排 / Cohere 重排)。**v3.2 变化**:阿里云百炼那条的 `protocol` 已是 **`dashscope_rerank`(原生形态)**,其余三条是 `cohere_rerank`(平铺形态)—— 类型键没变,前端仍按 `kinds` 渲染即可,别把 protocol 写死;**无需 context_tokens**;模型名按厂商填(百炼填 `gte-rerank-v2`,旧的 `gte-rerank` 已于 2026-05-30 下线,填它会 424 `reason=model_name`) |
| 未配模型就开开关 | — | 后端允许开启但检索时无默认 rerank 模型 = 等同关闭;建议前端在开关打开且无默认模型时给黄色提示 |

注意:开关与模型都属空间级配置;`GET /models` 公开清单里的 `rerank` 数组会包含本空间配置的
rerank 模型(形状不变),前端模型选择器现逻辑无需改动。重排只在**该空间**开着时生效,
且是检索链路的固定挂点(融合后取 top-30 候选重排),**没有暴露给 `/ask` 请求参数**,前端不用传任何东西。

两点补充:

1. **修好的是"新建",不是"已存在"**:`protocol` 在建供应商时由类型定死且之后不可改,所以你们 dev
   空间 A 那把已存在的"百炼重排"供应商仍是 `cohere_rerank` + `/compatible-mode/v1`,继续 424。
   请**删掉重建**(或新建一个)并取新的 `default_base_url`(站点根),模型名填 `gte-rerank-v2`;
2. **"重排有没有生效"从检索响应上看不出来**(你们 F2 后半段):重排只改头部**顺序**、不改融合分
   (`score` 仍是 RRF 值),上游失败时 fail-open 回落 —— 这是设计,不是缺陷。要做可观测得给命中行
   加法式补字段,已登记台账 **OPT-34**(触发条件:确实需要向用户/调参证明生效),动手前会先发契约变更说明。
   眼下的验收办法:开关 ON + 配好模型后【测试模型】200,即说明该协议链路通;检索侧看 worker 日志的
   降级 WARNING(有 rerank 失败才打)。

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

重排四类的预填地址**不是一样的**:百炼那条(v3.2 起)是站点根 `https://dashscope.aliyuncs.com`
(原生端点的服务路径由后端适配器钉死,地址里带 `/compatible-mode/v1` 会拼出两段路径),
SiliconFlow / Jina / Cohere 三条带 `/v1` 或 `/v2` 版本段(平铺形态,后端只往后面接 `/rerank`)。
所以**输入框初值一律取 `/types` 的 `default_base_url`,别复用 chat 那边的百炼地址**。

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

## 2. OPT-28 Token 用量页(已落地:空间设置 › 用量)

一句话:**后端只交明细汇总,不做任何换算与图表**;两个只读 GET,口径全在响应字段里。

### 2.1 按成员汇总表(Admin+)

`GET /api/v1/spaces/{space_id}/token-usage?period=7d|30d|all`(`period` 省略 = `30d`)

```json
{
  "period": "30d",
  "since": "2026-08-22T01:00:00+00:00",        // period=all → null(不是 0,别当日期用)
  "generated_at": "2026-09-21T01:00:00+00:00",  // 服务端 UTC 时刻,直接显示"截至 …"
  "space_total": {"prompt_tokens": 172, "completion_tokens": 40, "total_tokens": 212,
                  "estimated_tokens": 15, "chat_calls": 2, "embedding_calls": 2},
  "members": [
    {"user_id": "…", "username": "…", "nickname": "…", "role": 40,
     "prompt_tokens": 130, "completion_tokens": 40, "total_tokens": 170,
     "estimated_tokens": 0, "chat_calls": 1, "embedding_calls": 1}
  ]
}
```

字段口径(逐条都是后端已实现的语义,不是建议):

| 字段 | 含义与前端用法 |
| --- | --- |
| `total_tokens` | `prompt + completion` 的合计(对话与向量化**混在同一个数**里,按类别拆看图见 `*_calls`) |
| `estimated_tokens` | 其中"本地估算"那部分行(`source=estimated`)的 total 之和。**必须在 UI 上区分**:两种口径不可混算 |
| `chat_calls` / `embedding_calls` | 上游调用**次数**(一次问答可能因 Agent 工具轮打多次上游,故按调用计数,不按提问数) |
| `members[]` | **当前花名册**,零用量成员也在列表里(全 0);按 `total_tokens` 降序、同分按 `username` |
| `space_total` | 该空间**全部**事件之和,含 `user_id` 为空的行与**已退空间/已注销成员**的历史行 |

- 因此 `sum(members[].total_tokens) <= space_total.total_tokens`,**不等是设计如此**:
  差值就是"归属人已在花名册之外"的既成用量(成员被删时 `user_id` 置空,用量不回收)。
  若要给管理员一个交代,表格底部加一行"其他/历史归属 ≈ N"用 `space_total - Σmembers` 显示即可。
- `period` 传未知值(如 `1y`)**直接 422**(不静默回落),下拉只有三档就发这三档。
- 门槛:Viewer/Editor → **403 `FORBIDDEN`**(不是 404);非成员与跨空间 → **404 `SPACE_NOT_FOUND`**。
  入口按钮按角色隐藏,但**必须处理 403**(角色被降级后旧页面点进来就是)。

### 2.2 我的用量(所有成员)

`GET /api/v1/spaces/{space_id}/token-usage/me?period=…`

```json
{"period": "all", "since": null, "generated_at": "…",
 "usage": {"prompt_tokens": 600, "completion_tokens": 0, "total_tokens": 600,
           "estimated_tokens": 0, "chat_calls": 1, "embedding_calls": 0}}
```

- 任意成员可读(含 Viewer),非成员 404;**没有 `members` 字段**,别复用汇总表组件的数据层。
- 普通成员侧栏**只给这个入口**;后端已挡住别人,前端只需隐藏入口。

### 2.3 口径提示文案(必须放,原文照抄)

> 统计含对话与向量化两类用量。个别供应商不回传精确 token 数,此时后端按"1 汉字≈0.75 token、
> 1 字母≈0.25 token"估算并单列(标 ≈),与厂商回传的精确值不混算。
> 用量自 2026-09-21(计量上线)起统计,历史不回填。

### 2.4 最小调用序列(照抄可用)

1. 进页面:`GET /token-usage/me`(所有人都能拿,先渲染"我的用量"卡片);
2. 角色 ≥30 时再 `GET /token-usage?period=<档位>` 渲染汇总表;拿 403 就只留"我的用量"卡片;
3. 切档位只重发对应请求;`since`/`generated_at` 直接显示在表头("统计区间 … ~ …")。

### 2.5 已知的"不是 bug"

- **零用量但存在的成员**:成员列表是全花名册,不是"有用量的人";
- **入库用量记在上传者头上**:`uploaded_by` 为空的历史文档,其向量化用量只进 `space_total`;
- **API Key 用不了这两个接口**:端点只挂 JWT 依赖且刻意未登记进 Key 能力表,带 `X-API-Key`
  不带 Bearer → **401 `AUTH_REQUIRED`**(与 `/model-config/*`、`/admin/*` 同一做法:
  用量与配置都是"人的账");
- **重排与配置页【测试连接】不计费**:前者是开关的附带开销、不进单次问答账,后者是管理员点按钮;
- **调没发生就不计费**:模型连不上/鉴权被拒(一字节都没回来)时**不落行**,所以"空间配错了"
  不会凭空长出用量;中途断流则按已吐出的部分记 `estimated`。
  2026-09-21 dev 实测:同一问,真上游 = `chat`+`embedding` 两行 `upstream`;
  把 chat 上游换成死端口 → 只剩 `embedding` 一行,`chat_calls` 为 0;
- 行是按**上游调用**落的,不是按提问/文档落:Agent 模式一次提问 3 轮 = 3 行 `chat_calls`。

---

## 3. OPT-29 上下文窗口使用率(聊天页,进度条)

> **已落地(2026-09-21,契约已重导)**。下面按**实际实现**写,与计划稿的三处出入用「勘误」标出。

### 3.1 数据来源(两处,前端都只读不算)

1. **实时**:SSE `done` 帧新增 `context_used_tokens`、`context_window_tokens`(int|null);
2. **刷新复原**:`GET /conversations/{id}/messages` 的 assistant 消息带同名两字段
   → 进度条按"该会话最后一条 assistant 消息"渲染。

两值同时进 `done` 帧与库里的 assistant 行,**同一轮读到的必须一致**;续流
`GET .../conversations/{cid}/stream?after=<seq>` 重放出来的 `done` 帧同样带(值就在事件 payload 里)。

### 3.2 这两个数到底是什么(后端口径,前端只需知道"不要自己算")

- **分子** `context_used_tokens` = 本轮**最后一次**发给上游的 chat 调用 `prompt+completion`;
  Agent 模式多轮**不累加** —— 每轮都在重发同一份上下文,累加会报到超出窗口;
  「勘误 ①」计划稿写的是"组装点估算",实际取上游真实用量(厂商没回传才本地估算);
- **分母** `context_window_tokens` = **那一次调用实际解析到的** chat 模型的 `context_tokens`;
  空间层 chat 模型必填(F1),**全局层历史模型允许留空**;
- 「勘误 ②」**契约不区分实测/估算**:`done` 帧与 `messages` 就只有这两个 `int|null`,
  **没有** `context_usage_estimated`、也不打 ≈(你们 2026-09-21 §1 的裁决,后端按此实现);
- 「勘误 ③」计划稿写"模型没配窗口 → 两值均 null",实际**只让分母为 null**(分子照实给),
  双 null 专门留给"本轮没打模型"的情况 —— 三种组合见 §3.3,后端不会产出第四种。

### 3.3 渲染规则

```
上下文  12,400 / 64,000 tokens  ▓▓▓▓▓▓░░░░ 19%
```

| used | window | 含义 | 建议渲染 |
|---|---|---|---|
| 有值 | 有值 | 正常 | 进度条 = used / window |
| 有值 | null | 窗口未配置(全局层历史模型) | 只显示"已用 N tokens"+「窗口未配置」,不画条 |
| null | null | 本轮没打模型(兜底回答)/老消息 | **整行隐藏**(不是"窗口未配置") |
| null | 有值 | **后端不会产出**,出现即后端 bug,请报 |

- 百分比 = used / window;阈值配色建议:<70% 蓝、70–90% 黄、>90% 红 + 提示"接近上限,建议新开会话";
- **`partial=true` 的 done 帧同样带两值**,与完整回答同分支(断线宽限截止那一轮也要能显示);
- 更新时机:每轮回答 done 后刷新;流式过程中不做实时爬升(V1 口径,防误导)。

### 3.4 一次真跑的数字(dev 取证,deepseek-chat)

`done` 帧原文:`{"type":"done","message_id":"…","cited_indexes":[1,2,3],"context_used_tokens":845,"context_window_tokens":64000,"seq":276}`
库里同一行:`assistant | 845 | 64000`;同一轮的 user 行两值恒 null;
`token_usage_events` 那一条是 `prompt 572 + completion 273 = 845`(与分子逐项相等,分母不另开字段)。

---

## 4. 联调与落地节奏

- **OPT-27 已交付**(契约 50 路径 / 72 方法)、**OPT-28 已交付**(契约 **52 路径 / 74 方法**,
  新增 `GET /spaces/{sid}/token-usage` 与 `/token-usage/me`),两次都已重导 `docs/api/openapi.json`,
  前端可以开工配置页与用量页;
- **OPT-29 已交付(2026-09-21),契约基线不变:仍 52 路径 / 74 方法** —— 三项全是字段级加法
  (`done` 帧 +2、`MessageOut` +2、`messages` 表 +2 列/迁移 0015),**路径数不变不等于"没契约变化"**:
  要画进度条仍需按 §3 接;`GET /models`、`/admin/*`、API Key、既有 SSE 事件一律逐字段不变。
  **这三批代码的联调解锁已于 2026-09-21 全部完成**(推送 → 重启 server+worker → dev 库升到 0015),
  联调单可以开了(状态与自检取证见 `docs/api/给前端的回复-OPT29契约已就绪.md` 与接入工单 §0);
- 三批全部为**加法式变更**:语义变化只有一处对前端透明 —— 模型解析多了"空间优先、全局兜底"(见本文开头的提示框:
  `GET /models` 内容随活动空间变化,切空间要重拉)。OPT-28 另外只往**写入侧**加了旁路记账:
  问答/入库的响应体、状态码、错误码一律不变(记账失败只打服务端日志,不上契约)。
- 需要前端配合的只有三件事:① 新页面(配置页);② 新页面/区块(用量页,§2);
  ③ 模型选择器**要能渲染空清单**并引导去配置页
  (prod 下未配向量化 Key 的空间,`GET /models` 的 `embedding` 就是空的)。
