# 契约变更说明:OPT-35 供应商 `kinds` + OPT-36 类型 `model_names`(给前端)

> **一句话:两个只读响应各加一个字段,零破坏性。**
> ① `GET /spaces/{sid}/model-config`(聚合视图)的 `providers[]` 每项新增 `kinds: list[str]`
> —— 前端分块不再需要猜协议;② `GET /spaces/{sid}/model-config/types` 每项新增
> `model_names: list[str]` —— 模型名下拉的数据源,**只是提示清单,不是白名单**。
> 路径数/方法数 **52/74 不变**,请求体、错误码、SSE、`GET /api/v1/models` 形状**逐字段不动**。
> 配套需求单:`docs/api/前端需求单-模型配置页与聊天页第二轮.md`(§3 模型名、§5 分块回归)。

---

## 1. OPT-35:`providers[]` 新增 `kinds`

### 1.1 为什么加

配置页把供应商分成"对话"和"重排"两个区块,但库里**没存类型 key**,只存 `protocol`
(建好即锁死)。前端此前只能按 `protocol === 'cohere_rerank'` 划重排块。OPT-27/F2 那轮修复给
百炼重排另起了独立协议 `dashscope_rerank`(它的入参是 `input/parameters` 嵌套,与 cohere 平铺形状
**不通用**),于是重排协议变成**两个**,那句硬编码立刻失真:

- 新建的「阿里云百炼重排(gte-rerank-v2)」供应商 → 不出现在重排区块 → 用户看到"配了但页面没有";
- 同一个行反而串进对话区块。

把协议清单复制一份到前端 = 后端每加一个重排协议就要来改一次(后端自己刚把这层复制消灭掉,
`_RERANK_PROTOCOLS` 已改为从协议注册表派生)。所以由后端下发类别。

### 1.2 字段语义

```jsonc
"providers": [
  {
    "id": "...", "name": "公司百炼重排", "protocol": "dashscope_rerank",
    "base_url": "https://dashscope.aliyuncs.com", "key_hint": "sk-…34",
    "enabled": true, "model_count": 1,
    "kinds": ["rerank"]        // ★ 新增
  }
]
```

- 值域是**该供应商允许挂载的模型类别**(来自后端类型表,按 `protocol` 派生),
  **不是**"它当前实际挂了哪些 kind 的模型"。
- 为什么不用后者:新建但还没加模型的供应商并集为空 → 两个区块都不出现,痛点原样复现。
- 当前映射:`openai_compatible → ["chat"]`,`cohere_rerank / dashscope_rerank → ["rerank"]`。
  集合由类型表派生,后端新增类型/协议时前端零改动。
- **空数组 `[]` 是"未知协议"** 的取值(库里被手改或旧代码写的行)。语义 = 两个区块都不列它。
  真在 dev 上看到 `kinds: []` 的行,请回信报出来(那是数据异常,不是要前端兜底显示)。

### 1.3 前端接线

`views/settings/ProvidersModelsBlock.vue:57-61` 的 `protocol` 判断改成:

```ts
const providers = computed(() =>
  props.config.providers.filter((p) => p.kinds.includes(props.kind)),
)
```

顺带把顶部注释(`8-9`、`57`)那句"rerank 块 = cohere_rerank"删掉,别再留协议名。
过渡期(后端还没上线这一字段时)可用 `p.protocol.endsWith('_rerank')`,并标
`// TODO(OPT-35):改用 providers[].kinds` —— **不要**写死两个协议键的常量。

---

## 2. OPT-36:`/types` 每项新增 `model_names`

### 2.1 字段语义

```jsonc
// GET /spaces/{sid}/model-config/types 的一项
{
  "key": "dashscope_rerank",
  "label": "阿里云百炼重排(gte-rerank-v2)",
  "doc_url": "https://bailian.console.aliyun.com/",
  "default_base_url": "https://dashscope.aliyuncs.com",
  "protocol": "dashscope_rerank",
  "kinds": ["rerank"],
  "model_names": ["gte-rerank-v2", "qwen3-rerank"]   // ★ 新增
}
```

- **提示清单,不是白名单**:后端不校验提交的 `model_name` 是否在其中(现在也没有校验,只限长度 1-128),
  自定义值照存照用。填错的表现不变 —— 【测试模型】或真实调用时上游报 `model_name` 类错误,前端已有分诊。
- 数组顺序 = 建议的下拉顺序,把"最该选的"放在第一位(前端用 `allow-create` 时首位就是默认落点)。
- 可能为 `[]`(如 vLLM 这类完全取决于部署方的本地端点)→ 模型名退化为纯输入框,不要渲染空下拉。
- 编辑态:当前值不在清单里也要正常显示(unshift 进选项,或用 `allow-create` 天然支持)。
- 数据源是后端常量表,**刻意不调厂商的模型清单接口**:新建供应商时 Key 常常还没填、并非所有厂商都有
  清单端点,而且那会把上游延迟塞进配置页首屏。因此清单可能随厂商改名而变旧 —— 变旧不影响功能,
  发现过期名字回信报出来即可,后端按批更新。
- 已知下线名不会出现在清单里:`gte-rerank`(无 `-v2`)2026-05-30 已下线,后端表里给的是 `gte-rerank-v2`。

### 2.2 前端接线

`views/settings/ProvidersModelsBlock.vue:560-561` 的「模型名」`el-input` → 可自定义下拉,选项取
**当前所选供应商类型**的 `model_names`(`typeOptions` 已按 `kinds` 过滤,见 `:65`)。
后端字段上线前先用前端常量过渡 + TODO,上线后切数据源、UI 行为不变(需求单 §3)。

---

## 3. 本轮后端**没有**改的东西(别按旧文档改回去)

- `GET /api/v1/models` 形状与语义不变:仍是"全局 ∪ 活动空间、只列已启用";活动空间取自 access token
  里的 `sid`,换空间要走 `/auth/switch-space` 换 token 后重拉(前端 `ChatView.vue:251-262` 已经这么做)。
- 所有写接口的请求体、状态码、错误码不变;`/model-config/*` 与 `/token-usage*` 仍**只认 JWT**(API Key 401)。
- 重排的链路语义不变:开关 ON 但没有默认重排模型 = 等同关闭;重排只改顺序不改融合分,
  **响应上仍看不出它有没有参与**(登记 OPT-34),不要据此判 bug。
- 全局层 `/api/v1/admin/model-providers` 用的是另一套输出模型,**本轮不加 `kinds`**
  (配置页只吃空间层聚合视图)。超管后台若要,再单开一条。

---

## 4. 联调验收(对着这两条就够)

1. 新建「阿里云百炼重排(gte-rerank-v2)」→ 聚合视图该行 `protocol = dashscope_rerank` 且
   `kinds = ["rerank"]` → 页面里它**只出现在重排区块**;对话区块不串。
2. `GET /types` → 每个类型的 `model_names` 非空者渲染下拉、可自由输入;`vllm` 为 `[]` 时是纯输入框;
   提交一个清单外的自定义名 → 201 成功(证明"不校验"成立)。
