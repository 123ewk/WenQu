# 给后端的需求单 · OPT-38 对话模型清单空间化 + OPT-39 上下文窗口参数口径

- 提出方:前端会话(`web-l0` 分支,G:/my/my_file/my_rag)
- 日期:2026-09-21
- 占号:**OPT-38**(对话模型清单与默认解析空间化)、**OPT-39**(上下文窗口参数口径与上限)。台账已随本文档登记,后端请勿另取这两个号。
- 配套前端工单(前端自己要做的事,不需要后端动手):`docs/api/前端实现工单-OPT38对话模型选择器与OPT39参数自定义.md`
- 关联既有项:OPT-33(`MessageOut.model_id` 在"走全局默认"链上是 null)、OPT-32(生成段错误码丢失)、上轮遗留的 dev 全局夹具清理。
- 消费方式:本文自包含,冷启动可读。所有取证命令可抄。

---

## 1. 问题(一句话)

**问答页能选到的模型、以及不选模型时实际调用的模型,都不是"本空间模型配置里配的模型",而是全局层的那条兜底行。** 产品口径要求:对话用哪个 LLM,由本空间的模型配置决定;空间没配就是不能用,不能悄悄换成全局(或内置)模型。

第二条是参数口径:配置弹窗里的数值参数(上下文窗口)只有 4 档预设、最大 128000,当前主流模型已是 256K/1M 档;需要"预设 + 自定义输入"都成立,并且后端要明确 `context_tokens` 的边界与语义。

---

## 2. 现象与取证

### 2.1 现象:空间没配对话模型,下拉里却有一条,而且能出答案

用户实测截图(2026-09-21):当前空间「产品」(Owner),问答页模型下拉展开后是 `分组 deepseek → 选项 deepseek-chat`,输入框未选时显示占位「默认模型」。而同一时刻该空间的模型配置页「对话模型」表格是空的。

取证 A —— dev 库只读 SELECT(chat 行全量):

```bash
docker exec my_rag-postgres-1 psql -U wenqu -d wenqu -c \
"select m.model_id, m.context_tokens, m.is_default, coalesce(s.name,'(全局)') as space, p.name as provider
 from custom_models m join model_providers p on p.id=m.provider_id
 left join spaces s on s.id=m.space_id where m.kind='chat' order by m.space_id nulls first;"
```

| model_id | context_tokens | is_default | space | provider |
|---|---|---|---|---|
| `deepseek/deepseek-chat` | 64000 | t | **(全局)** | deepseek |
| `prod-deepseek/deepseek-chat` | 64000 | t | 二轮联调空间A | prod-deepseek |
| `deepseek/deepseek-chat` | 64000 | t | 联调空间A-406fa0 | deepseek |
| `百炼/qwen-plus` | 32768 | f | 联调空间A-406fa0 | 百炼Chat |

「产品」空间名下 **0 条 chat 行** → 下拉里那条只能来自全局层。

取证 B —— 代码链路(文件:行,`wenqu-backend` @ `main`):

1. `server/src/app/core/model_catalog.py::ModelCatalog.load_from_db` 先 `_layer(None)` 收全局 provider/model,再 `_layer(space_id)` 让空间行遮蔽同名行(见该函数内 `_layer` 定义与末尾 `_layer(None) → _layer(space_id)` 调用)。合并本身是 OPT-27 的设计,注释里写明"空间行遮蔽全局行"。
2. `server/src/app/api/routes/models.py:41-63`(`GET /api/v1/models`)把合并快照 `catalog.enabled_models_by_kind()` **原样全量**吐给前端 → 全局行进了空间的下拉。
3. 同文件 `defaults=catalog.defaults()`:默认解析里 `if m.is_default: defaults[m.kind] = m.model_id` 对**全局行也会写**,空间行只是"后写顶掉"。空间没有 chat 默认行时,`defaults["chat"]` 就是全局那条。
4. 前端不选模型时 `POST /ask` 不带 `model_id` → `server/src/app/core/model_client.py:156-163 ChatClient._resolve_chat` 走 `self._catalog.default_model("chat")` → 命中全局 `deepseek/deepseek-chat`。`model_catalog.py:210 default_model()` 只有在**完全解析不到**时才抛 `ModelNotConfigured`。

副作用(可量化):这条链路上 `messages.model_id` 落 null(即台账 OPT-33 记的那条),而 `token_usage_events` 记的是全局夹具的 model_id —— 空间维度的用量归因里混进了"该空间从没配过的模型"。

取证 C —— 页面文案自证(说明这是**已知语义**,不是漏网):

- `web/src/views/settings/ModelConfigView.vue:138` 副标题:`本空间自配模型与 Key;未配置的模型调用回落全局默认`
- `web/src/views/settings/ProvidersModelsBlock.vue:441` 对话区块副标题:`问答未指定模型时使用带「默认」标记的模型`
- `server/src/app/core/model_catalog.py` 模块 docstring:`默认解析"空间默认 → 全局默认"`、`空目录是合法状态:解析不到即 ModelNotConfigured 503,fail-closed,不做任何内置兜底`

最后那句"不做任何内置兜底"和实际行为并不一致:**内置 YAML 确实下线了,但全局层行成了新的事实兜底**。本需求就是把这句 docstring 变成真话(对 chat/rerank 而言)。

### 2.2 现象:上下文窗口的边界与语义在契约里是空的

- `server/src/app/api/routes/space_models.py:127` / `:134`:`context_tokens: int | None = Field(default=None, gt=0)` —— **无上限**,所以 262144(256K)、1048576(1M)现在就能存进去。这条不阻塞前端。
- `server/src/app/application/service/space_models.py:345-352 _assert_extra_model_rules`:chat 必填,缺了 422。
- 全仓读点核对(证明它只是分母、不参与截断):

```bash
cd wenqu-backend && grep -rn "context_tokens" server/src/app --include=*.py
```

结论:除开路由/服务层的写入与回显,运行时只有 `server/src/app/core/model_usage.py:142 window_tokens=last.context_tokens`(OPT-29 进度条分母)与 `server/src/app/domain/models.py:312`(消息行留痕)。**没有任何地方按 `context_tokens` 做 prompt 裁剪或预算控制**(已确认 `qa_pipeline.py`/`retrieval.py` 不读它)。

需要后端确认并写进契约的正是这一点:填 256K 不会改变服务端的上下文组装行为,它只是给前端的分母;上游真给不了这么大是上游的事。

---

## 3. 需求(逐条,标注是否破坏契约)

### N1 chat 清单只含本空间层 —— 核心项

`GET /api/v1/models` 的 `chat` 数组(建议 `rerank` 一并)只返回 `space_id == 当前请求空间` 的行。

- **embedding 维持现状**,不要动:内置 embedding + "dev 回落全局、prod 503" 是 2026-09-20 用户点名的例外(`model_catalog.py` docstring 已记),改了会打断入库链路。
- 实现取向(任选,别改快照的合并语义就行,因为 `default_model("embedding")` 的 dev 回落还要靠全局行):
  - 推荐:给 `ModelEntry` 加 `space_layer: bool`(由 `_layer(scope)` 传入),`routes/models.py` 按 `kind in ("chat","rerank") and not space_layer → 跳过` 过滤。快照内部语义不变,只有对外清单收窄。
  - 或:`enabled_models_by_kind()` 增加 `only_space: bool` 参数,清单端点传 True;`/admin` 侧视图传 False。
- `space_id is None` 的请求(无空间上下文)怎么办:清单端点本来就要求登录且带活动空间(`get_request_space_id`),若解析不到空间,建议返回空数组而不是全局清单 —— 宁缺勿兜。
- 破坏性评估:**非破坏**(响应形状不变,只是条目变少)。但会改变前端可见结果,必须和 N2 同批上线,否则会出现"下拉里没有、但不选照样能用全局模型"的更迷惑状态。

### N2 默认解析空间化 —— 与 N1 同批,必须一起做

`default_model("chat")` / `default_model("rerank")` 在 `space_id is not None` 时**只解析空间层默认**;解析不到 → `ModelNotConfigured` 503,message 要指名动作,风格对齐已有的 embedding 那条:

```
该空间未配置可用的对话模型,请到 空间设置 › 模型配置 添加并启用一个对话模型
```

- 全局层默认只在 `space_id is None` 的路径上生效(worker 无空间上下文的系统任务、`/admin/*` 探活)。
- 顺带把 `model_catalog.py` docstring 的 `默认解析"空间默认 → 全局默认"` 改成符合新事实的一句话,并解释"为什么 chat 不回落全局"(空间自助配模型后,回落全局 = 用量与归因记到用户没配的模型上)。

### N3 `AskRequest.model_id` 是否改必填 —— **需要裁决**

前端本轮起**总会显式传** `model_id`(见配套前端工单 A1),所以两条路都能达成用户目标:

| 方案 | 说明 | 破坏性 |
|---|---|---|
| (a) 保持可空 + 按 N2 只解析空间默认 | 推荐。第三方 `/ask` 接入(含 API Key 调用)不受影响,"不传"变成"用本空间默认",语义仍然自洽 | 非破坏 |
| (b) 改必填,缺失 422 `VALIDATION` | 契约上最干净,但 `m2-接口说明.md` 已把 `model_id` 写成可选,外部脚本会红 | **破坏**,要走契约变更说明 |

请后端按 (a) 实现,并在 `AskRequest.model_id` 的字段注释里写清"缺省 = 本空间默认对话模型;本空间无可用对话模型 → 503 MODEL_NOT_CONFIGURED"。若倾向 (b),先出前端契约变更说明再动。

### N4 显式传非本空间模型的错误码 —— 建议项

现状:`model_id` 不在快照里 → `get_model` 抛 `ModelNotConfigured` → **503** "模型 X 不在清单中"。N1/N2 之后,"传了全局那条 id" 也会落到这一支,但那是**调用方传错**,不是服务端没配,503 会把人引向"去找管理员"。

- 建议:显式传入且解析不到 → **400 `VALIDATION`**(或 404 `NOT_FOUND`,与既有错误码表对齐后择一),message 点明"模型 X 不属于本空间或未启用,可选清单见 GET /api/v1/models";**只有"本空间一个可用模型都没配"才保留 503**。
- 若判定改 status 影响面大(有测试钉着),最低要求:**message 必须区分**"不在本空间清单"与"空间未配置任何模型",前端按 code 分支时至少要能透显。
- 破坏性:改 status 属契约行为变更,需在回信里点名改了哪条。

### N5 `context_tokens` 边界写进契约 —— 小改动,但要说清

1. 显式声明上限:`Field(gt=0, le=10_000_000)` 这类宽松天花板(避免有人填 1e18 把进度条算成 NaN/溢出),或明确"无上限"并在注释里说明理由。前端预设最大给到 1048576(1M),自定义输入打算卡到 20_000_000 以内,只要 ≤ 后端上限就一致。
2. 在 `SpaceModelIn.context_tokens` 注释里写明语义:**只作 OPT-29 上下文进度条的分母,服务端不据此裁剪上下文**;真实窗口由上游模型决定。
3. 若将来要按它做 prompt 预算(那才是它字面意思),属行为变更,先发前端契约变更说明。
4. `ModelItem.context_tokens`(公开清单)建议一并返回,前端选择器可显示"窗口 256K"注记 —— 契约里已有该字段(`models.py:28`),确认非空即可,前端已按可空处理。

### N6 dev 全局夹具清理 —— 上轮遗留,请一并处理

全局层 `deepseek` provider + `deepseek/deepseek-chat`(64000, is_default=t)是 OPT-27 联调遗留(上轮报告已挂"待点头")。N1/N2 之后它不再污染空间清单,但 `/admin` 视图和 dev embedding 回落路径仍会看到它。

- 请求:确认它属于 dev 夹具并删除(或换成明显不可调用的假端点,让"没配模型"在 dev 也暴露出来)。
- **共享 dev 库,执行前要用户点头**;需要的话我可以出 SQL 并只跑 SELECT 预演。
- 同时说明:全局层是否还打算作为"运维预置给所有空间的模型池"的正式能力?
  - 若是 → 需要产品口径明确"全局 chat 行是否允许被空间选用",那 N1 就要改成"允许但标注来源",本需求单要重开;
  - 若否(我理解的事实) → 建议 N1/N2 落地后,`/admin/models` 的 chat 行只服务运维自测,不再进任何用户链路。

### N7 文案口径同步

后端 message / docstring 里所有"回落全局默认""全局兜底"的表述随 N1/N2 一起改。前端页面的两句副标题由我改(见配套工单 A4),不占用后端。

---

## 4. 对接标准(交付物与验收)

### 4.1 必须交付

1. **代码 + 测试**,`make server-test` 全绿;涉及文件建议:`core/model_catalog.py`、`api/routes/models.py`、`core/model_client.py`、`api/routes/space_models.py`(N5 注释/上限)、`application/service/space_models.py`。
2. **重导契约**:`make server-openapi` → `docs/api/openapi.json` 提交。预期变化:`ModelItem`/`SpaceModelIn` 的字段注释与 `maximum`(若加了 le);路径数/方法数**不变**(N1 是过滤不是新端点)。提交信息里报一下变更行数。
3. **测试断言更新点名**:以下现有用例按新语义会红,请说明改成什么(不要静默改):
   - `server/tests/test_space_models_integration.py:126` `assert merged.defaults()["chat"] == "g/chat"`(空间默认顶掉全局)—— 新语义下若空间无 chat 行,`defaults` 不应有 `chat`。
   - 同文件 `:108-127` 的合并/遮蔽用例:遮蔽语义是否保留(同名同 id 的空间行顶掉全局行)请明确,前端不依赖它,但用例要自洽。
   - `server/tests/test_admin_models_integration.py:244/252/274` 公开清单 `/api/v1/models` 断言。
   - `server/tests/test_api_integration.py:1463` `assert client.get("/api/v1/models", headers=auth).json()["chat"] == []` —— 这条正好是新语义的期望形态,保留。
   - 新增 2 条用例(必做):
     - ① 空间无 chat 行 → `GET /api/v1/models` 的 `chat == []`、`defaults` 不含 `chat`;
     - ② 同一空间 `POST /ask`(不传 `model_id`)→ 按 N3(a) 期望 503 `MODEL_NOT_CONFIGURED`,并断言 message 原文;传全局 model_id → 按 N4 裁决结果断言。
4. **回信**:沿用上轮四段式(`已完成 / 未完成 / 禁测项 / 报告`),写到 `docs/api/给前端的回复-OPT38-39落地与联调门槛.md`(名字可改,请在这条链路里固定下来)。回信里必须给出:live dev 是否已重启到新码、契约路径/方法数、N3 与 N4 的裁决结论。

### 4.2 我这边怎么验(冷启动可抄)

契约对表:

```bash
cd G:/my/my_file/my_rag && python - <<'PY'
import json
d = json.load(open('docs/api/openapi.json', encoding='utf-8'))
print('paths/methods:', len(d['paths']), sum(len([m for m in v if m in ('get','post','put','patch','delete')]) for v in d['paths'].values()))
print(json.dumps(d['components']['schemas']['ModelItem'], ensure_ascii=False, indent=1))
print(json.dumps(d['components']['schemas']['SpaceModelIn']['properties']['context_tokens'], ensure_ascii=False))
PY
```

行为验收(dev,需后端重启到新码 —— `:8000` 是共享进程,重启前我会先向用户报备):

1. 用一个**没有 chat 配置**的空间(如「产品」)登录 → `GET /api/v1/models` 的 `chat` 应为 `[]`,`defaults` 不含 `chat`;问答页显示"去配置"引导且发不出去。
2. 在该空间配一个 chat 模型(上下文窗口填 262144)→ 清单只出现这一条;不选模型发问 → `messages.model_id` 落该 id(不再是 null,OPT-33 顺带闭环),进度条分母 262144。
3. 显式传 `deepseek/deepseek-chat`(全局那条)→ 期望按 N4 的 400/404 与点明"不属于本空间"的 message。

### 4.3 时序与并行约束

- **N1 + N2 必须同批**(缺一会出现"下拉没有、不选照样用"的更坏状态);N3/N4/N5 可同批也可拆;N6 独立。
- 前端不阻塞:配套工单的 A2/A3(上下文窗口控件)与后端无关,我先做;A1 的"必传 model_id"也先做(传了之后后端行为按 N1/N2 才完全对齐);A4 文案与"空清单引导"的最终形态等后端回信再收尾。
- 本仓库正在多会话并行:`feat/api-keys-active`(OPT-31,迁移 0016)未合并。**本需求单预期不需要迁移**;若 N6/N1 需要动数据或加列,取号前先对 `alembic/versions` 现状,别插到 0016 前面(先例见台账 OPT-37 的排期硬约束)。
- 提交纪律照旧:一片一提交、逐文件 `git add`、不回滚他人 in-flight 改动、不擅自 push。
