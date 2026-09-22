# 前端实现工单 · OPT-38 对话模型选择器只用本空间模型 + OPT-39 配置弹窗参数可自定义

- 归属:前端会话自用工单(实现方 = 我;后端要做的部分另见 `docs/api/给后端的需求单-OPT38对话模型清单空间化与OPT39上下文窗口上限.md`,本文只写前端改动)
- 分支:`web-l0`(G:/my/my_file/my_rag),基线 `ca41ee3`
- 日期:2026-09-21
- 消费方式:自包含,冷启动可照着做;每条改动带文件:行与验收断言。

---

## 1. 需求复述(用户原话拆成可验收条目)

1. "把默认的模型下了,使用在这个空间下的模型配置里面配置了的 llm"
   → 问答页**不再有"默认模型"这个可选项**;下拉里的每一条都必须是本空间「模型配置 › 对话模型」里配好并启用的模型;选不到就是没配,不能靠服务端兜底出答案。
2. "这些窗口里面的现在参数除了给了预写好的,也要支持自定义"
   → 配置弹窗里带预设的字段(上下文窗口)必须**预设 + 自由输入双通道**,且非法输入要有提示,不能静默禁用提交按钮。
3. "现在的上下文窗口是不是太小了,至少 256K"
   → 预设档位最大要覆盖 262144(256K),并给到 1M 档;编辑已有模型(如 64000 这种非预设值)要正常回显。

## 2. 现象与根因(前端侧)

### 2.1 现象:下拉里有本空间从没配过的模型

截图实测(2026-09-21,空间「产品」):下拉 = 分组 `deepseek` → 选项 `deepseek-chat`;而同空间模型配置页对话模型表格为空。

- 前端侧根因:数据源 `GET /api/v1/models` 是**全局 ∪ 空间合并清单**(后端 N1 要收窄的那条),`ChatView.vue:177 chatModels` 原样渲染,所以全局夹具必然出现。前端**无法**靠现有字段区分某条来自全局还是本空间 —— 契约 `ModelItem` 没有来源标记。
- 另一半根因在前端自己:`ChatView.vue:174-204` 把"不选"当合法态(`selectedModelId = ''` → 不传 `model_id` → 服务端自选),于是"用哪个模型"的决定权交回服务端,正好落进全局兜底。模板 `:962-981` 的 `placeholder="默认模型"` + `clearable` + `value-on-clear=""` 就是这条的实现。

结论:**这一条必须两端各改一半**。前端这一半(必传 + 无"默认模型"态)不依赖后端就能落地,先做;清单里不再混全局行依赖后端 N1/N2。

### 2.2 现象:上下文窗口"看似能自定义,实际会静默卡死"

浏览器实测(dev `localhost:5173`,新建模型弹窗):

- 控件是 `el-select filterable allow-create`(`ProvidersModelsBlock.vue:604-613`),输入 `262144` + 回车 → 该项**确实**成为选中值(DOM:`.el-select` 文本 = `262144`,内部 input 已清空 = 已提交为选中项)。所以"不支持自定义"不是不能输,而是:
  1. **必须按回车**才生效,失焦不提交 —— 用户不知道;
  2. 预设 label 走 `toLocaleString()`(`:612`),显示成 `32,768`;而表格里上下文窗口列也显示 `64,000`(`:62` 行内文本)。用户照抄带逗号的值 → `Number('64,000')` = NaN → `parsedContext`(`:286-289`)返回 null → `canSubmitModel`(`:290-296`)false →【创建】永久禁用,**页面没有任何提示**(`:614` 的 hint 只讲"常用值",`:613` 无校验文案);
  3. 非法值(小数、负数、字母)同样静默禁用;
  4. 预设最高 `128000`(`:58 CONTEXT_PRESETS`),256K/1M 档完全没有,hint 文案 `:614` 也在复读这四个数。
- 后端侧无阻塞:`context_tokens` 只有 `gt=0`(`space_models.py:127`),262144 存得进去。**这条前端可独立完成。**

### 2.3 现象:模型名字段(对照项,不改)

`ProvidersModelsBlock.vue:583-598` 的模型名同样是 `filterable allow-create`,上轮联调已实测通过(自定义 `my-custom-rerank-x1` → 201 创建成功,见 `docs/api/给后端的回复-模型配置页二轮.md` §1)。字符串字段用 allow-create 是合适的,**保持不动**,只补一句"回车确认"的提示。

---

## 3. 改动清单

### A1 问答页模型选择器:去掉"默认模型"态,`model_id` 必传

文件:`web/src/views/chat/ChatView.vue`

| 位置 | 现状 | 改成 |
|---|---|---|
| `:171-206` 状态段 | `const selectedModelId = ref('')`,注释"`''` = 默认模型(不传 model_id)" | 保留 `ref('')` 作为"尚未初始化",但语义改为"未初始化";新增 `syncModelSelection()`:清单刷新后,若当前值不在 `chatModels` 里 → 取 `catalog.defaults.chat`(命中清单时)否则 `chatModels[0].id`;清单为空 → 置 `''` |
| `:196-204 loadModelCatalog` | 拉完不改选 | 拉完调 `syncModelSelection()` |
| `:250-263` 切空间 watch | 重拉清单 | 重拉后同样 `syncModelSelection()`(切空间必须换默认,不能留上一个空间的 id) |
| `:456` 请求体 | `model_id: selectedModelId.value \|\| null` | `model_id: selectedModelId.value`(见 A4:为空时根本不允许发) |
| `:962-981` 模板 | `placeholder="默认模型"`、`clearable`、`value-on-clear=""`、`:title` 双分支 | 删 `clearable`/`value-on-clear`;`placeholder="选择模型"`;`:title` 恒为 `回答模型:<label>(本空间模型配置里维护)`;选项 `:label="m.model"` 之外加 `<div class="model-opt-sub">{{ m.id }}</div>` 副行(同名模型靠 id 区分,现在 `deepseek-chat` 就可能两条同名) |
| `:192-194 selectedModelLabel` | 找不到时回退 `'默认模型'` | 回退改为空串,由 placeholder 兜 |
| 默认项标记 | 无 | `defaults.chat` 命中的那一项在选项右侧标 `默认` 小徽标(数据已有:`ModelCatalogOut.defaults`,types.ts:252-258) |
| `:1533-1536 .model-select` | `width:168px` | 加副行后放宽到 200px(实测不截断为准) |

补充要求:
- 选中项要**跨会话保持**(组件内 `ref` 即可,刷新页面回到空间默认);不做 localStorage 持久化(避免跨空间脏值,收益小)。
- 清单为空(`chatModels.length === 0`)时:现有 `chatCatalogEmpty` 分支(`:979-981` 的"本空间未配置对话模型 · 去配置"链接)保留,**并追加**:输入区 placeholder 改「本空间未配置对话模型,先去配置」+ `canSend`(`:212`)追加 `&& chatModels.length > 0`,让发送按钮禁用。
  - ⚠️ 现在这个分支永远不会命中(dev 里全局夹具至少给一条),真正生效要等后端 N1 上线。代码可以先写,联调时按需求单 §4.2 步骤 1 验。
- 注释口径同步:`:171` 段头注释、`:962` 模板注释里"二轮§4:无「默认模型」可选项,不选 = 不传 model_id"这句要改成新语义,别留旧解释。

### A2 上下文窗口控件:预设 + 自由输入 + 显式校验

文件:`web/src/views/settings/ProvidersModelsBlock.vue`

1. **控件换形**:`el-select filterable allow-create` → `el-input-number` + 一排预设 chip。
   ```
   <el-input-number v-model="contextNumber" :min="1" :max="CONTEXT_MAX" :step="1024"
                    :controls-position="'right'" placeholder="直接输入 tokens,如 262144" class="pm-full" />
   <div class="pm-preset-row">
     <button v-for="n in CONTEXT_PRESETS" :key="n" class="pm-preset" :class="{on: contextNumber===n}" @click="contextNumber=n">
       {{ n.toLocaleString() }}<span class="pm-preset-alias">{{ ctxAlias(n) }}</span>
     </button>
   </div>
   ```
   理由:数字字段用下拉 + `allow-create` 要求"必须回车",且允许把非数字串塞进值里;`el-input-number` 天然杜绝非法输入,预设 chip 一点即填,自定义直接键入 —— 两个通道都显性可见。
2. `CONTEXT_PRESETS`(`:58`)= `[4096, 8192, 32768, 64000, 65536, 128000, 131072, 200000, 262144, 512000, 1048576]`;`ctxAlias` 给 256K/1M 这类量级注记(`262144 → 256K`、`1048576 → 1M`、`131072 → 128K`),其余返回空。
3. `CONTEXT_MAX = 20_000_000`(前端自设护栏;后端上限见需求单 N5,若后端最终给了 `le`,改成与后端同值并在注释里指向契约)。
4. 状态改造:`modelDialog.contextTokens: string` → `contextNumber: number | null`(`:233`);删 `contextSelectValue` computed(`:256-260`);`parsedContext`(`:286-289`)退化为 `contextNumber` 的合法性校验(`Number.isInteger && > 0 && <= CONTEXT_MAX`);提交处 `:303/:312/:322` 相应改。
   - 编辑态回显(`:279`):`m.context_tokens ?? null`,非预设值(如 64000)正常显示 —— 64000 已进预设,双保险。
5. 校验提示(`:614` hint 重写 + 新增错误行):
   - hint:`OPT-29 上下文进度条的分母(仅用于显示占比,服务端不据此裁剪上下文);填上游模型的真实窗口,不确定时填小不填大`
   - 非法/为空时:`<div class="pm-err">上下文窗口需为 1 ~ 20,000,000 的整数</div>`,并让【创建】禁用 —— **不再静默**。
6. 模型名(`:583-598`)保留 `allow-create`,hint 末尾补一句:`自定义名输入后按回车确认`。

### A3 提交前对表(避免把非法值发给后端)

- 前端校验必须与契约同向:`context_tokens` 正整数;`AskRequest.model_id` 为清单内 id。
- 不新增本地"兜底转换"(例如把空 context 偷偷填 4096)—— 后端 chat 必填是 422,前端就禁提交,别造第二套真值。

### A4 文案口径(等后端 N1/N2 落地同批改,先挂 blocked)

| 文件:行 | 现文案 | 目标 |
|---|---|---|
| `web/src/views/settings/ModelConfigView.vue:138` | `本空间自配模型与 Key;未配置的模型调用回落全局默认` | `本空间自配模型与 Key;问答只用本空间配置的模型` |
| `web/src/views/settings/ProvidersModelsBlock.vue:441` | `问答未指定模型时使用带「默认」标记的模型;上下文窗口是进度条分母` | `问答页默认选中带「默认」标记的模型(可切换);上下文窗口是进度条分母` |
| `web/src/api/types.ts:359` 注释 | `问答未指定模型时用它` | `问答页默认选中它` |
| `web/src/api/types.ts:240` 注释(`ModelItem.id`) | 已说明"提交 model_id 用它" | 补一句"清单只含本空间自配模型(OPT-38)" |

**为什么挂 blocked**:文案先改会和后端实际行为不符(后端还没收窄),用户会看到"只用本空间模型"但下拉里仍是全局夹具。前端 A1/A2 先落,文案随回信收尾。

### A5 退路方案(若后端只做"加 `scope` 字段"而不做 N1)

`ModelItem` 加 `scope: 'space' | 'global'` → 前端 `ChatView.vue:177` 改为 `chatModels = catalog.chat.filter(m => m.scope === 'space')`,并在模型配置页对全局行不显示。这条**不解决 N2**(不选仍会用全局默认),所以 A1 的"必传 model_id"必须一起做才能达标。仅作为后端排期让步时的兜底,不作为首选。

---

## 4. 提交拆分与门禁(多 AI 并行纪律)

一片一提交、逐文件 `git add`、commit 前 `git status` + `git diff --cached --stat` 两道核对;`git status` 里不是本会话改的文件一律跳过,不顺手提交也不回滚。

| 片 | 内容 | 依赖 | 门禁 |
|---|---|---|---|
| W1 | A2 + A3(上下文窗口控件、预设、校验)+ A3 的模型名 hint | 无 | `pnpm --dir web exec vue-tsc -b && pnpm --dir web run build` |
| W2 | A1(选择器去"默认模型"、必传 `model_id`、空清单禁发、选项副行) | 无(空清单体验等后端) | 同上 + 浏览器实测两条路径 |
| W3 | A4 文案与 types 注释 | 后端 N1/N2 回信 | 同上 |
| W4 | 文档(本工单 + 需求单) | — | 无 |

- **push 状态(2026-09-22 更新)**:本工单四片已随 `web-l0` 推到 `origin/web-l0`(`3b33e36..072fbfb`);OPT-31 后端分支 `feat/api-keys-active` 已并入 `main`(`e4e8148`,门禁 415 passed / ruff+mypy / openapi 逐字一致),`main` 侧尚未推送,等用户点头。
- 若发现 `web/src/api/types.ts`、`ModelConfigView.vue`、`ProvidersModelsBlock.vue` 有别的会话 in-flight 改动:照旧用隔离 worktree 验证构建(先例:OPT-31 的 `web-akverify`),提交用逐文件 `git add` + 必要时 git plumbing 只提自己的 hunk。

## 5. 验收标准(逐条可判)

1. 空间「产品」(无 chat 配置,后端 N1 上线后):下拉不出现任何模型,显示"本空间未配置对话模型 · 去配置",输入框 placeholder 变更,发送按钮禁用。
2. 有 chat 配置的空间:下拉选项数 == 模型配置页「对话模型」里**启用**的行数;每条 label = 上游模型名,副行 = 模型 id;`defaults.chat` 那条带「默认」徽标;进入页面即预选该条(不是空白、不是"默认模型")。
3. 发一条问:请求体 `model_id` 非空;`GET /messages` 里 assistant 行 `model_id` == 所选 id(OPT-33 那条 null 现象在本链路消失);进度条分母 == 该模型 `context_tokens`。
4. 切空间后重进对话页:选中项属于新空间(不残留上一个空间的 id);若新空间无配置 → 落到验收 1 的形态。
5. 新建对话模型:上下文窗口可点预设(含 `262,144 256K`)也可键入 `262144`,无需回车;清空 →【创建】禁用且出现红字提示;填 `0`/负数/超 `20,000,000` → 同样有提示;保存后表格显示 `262,144`,重开弹窗回显 `262144`。
6. 编辑现有 64000 的模型:回显 64000,不改这一字段能正常保存其它字段(PATCH 语义:`context_tokens` 仍按当前值提交,不能误传 null)。
7. `vue-tsc -b && vite build` 全绿;`grep -rn "默认模型" web/src` 只剩"去配置/默认标记"这类正确用法,不再有"不选 = 默认模型"的语义。

## 6. 风险与禁测项

- **不重启共享 dev 后端 `:8000`**(另一会话在用),需要新码时先向用户报备。
- 浏览器实测**不往 dev 库留垃圾数据**:新建模型用一次性空间,验完删掉自己建的供应商/模型并在报告里列名;上轮遗留夹具(`prod-deepseek`、`ui-bailian-rerank` 等)属另一会话产物,不动。
- A1 的"空清单禁发"在后端 N1 之前不可达,别为此改判成"前端自己按 provider 猜来源"—— 猜不出来,也不该猜。
- 不做的事:不在前端加"模型可用性探活"(后端已有【测试模型】);不把 `top_k` 暴露到 UI(当前写死 6,契约允许 1–20 —— 属新需求,要用户裁决,已在本工单 §7 记一笔)。

## 7. 顺带记录(本轮不做)

- `top_k` 前端硬编码 `6`(`ChatView.vue:455`):同类"参数只有预写值、没给自定义",但要不要开放到 UI 是产品决定,先问用户。
- Agent 模式、温度等生成参数目前完全不可见;若要做"参数自定义",这是下一个讨论点。
- OPT-33(`MessageOut.model_id` 语义)在 A1 之后于本链路自然消失,但**不等于已修**,台账那条保留。

## 8. 实现进度(2026-09-21 前端会话自填)

| 片 | 状态 | 提交 | 实测证据 |
| --- | --- | --- | --- |
| W1 = A2/A3(上下文窗口预设 chips + 数字输入 + 显式校验,扩到 256K/1M) | 已完成 | `e25ac46` | 浏览器:新建弹窗未触碰时不高亮红 → 点 `262,144` 档位回填 → 清空失焦出"必填"红字且提交按钮禁用 → 编辑 `prod-deepseek/deepseek-chat` 回显 64000 且对应档位高亮、保存可用 |
| W2 = A1(问答页去"默认模型"态、`model_id` 必传、空/失败态提示) | 已完成 | `da655ef` | `vue-tsc -b` 退出 0;`POST /ask` 请求体实测为 `{"question":"…","model_id":"prod-deepseek/deepseek-chat",…}`(拦截 fetch 取证,非 null);下拉副行显示 id、`默认` 徽标打在 `defaults.chat` 那条;选择器无清空图标 |
| W3 = A4(文案口径:`ModelConfigView.vue:138`、`ProvidersModelsBlock.vue:441`、`types.ts` 注释里"回落全局默认"那几句) | **blocked** | — | 等后端对 N1/N2 的回复(见需求单 §4 四段式回信),落地后同批改,否则会写出与实现不符的承诺 |
| W4(整体回归 + 台账勾销) | 未开始 | — | 依赖 W3 |

实测取证与验收标准 3 的一条偏差,记清楚:

- 请求侧 `model_id` 已必传(验收标准 3 前半 **通过**);但 `GET /messages` 里 assistant 行 `model_id` 仍为 **null**。已定位到确切代码原因而非猜测:无检索命中时回答走 `_NO_RESULT_ANSWER` 短路分支,该分支在 `wenqu-backend/server/src/app/application/service/qa_pipeline.py:149` 把 `model_id` **硬编码成 None** 落库(正常生成分支 `:217/:231` 落的是 `state.ctx.model_id`,即请求里那条 id)。而本账号在 dev 只有「二轮联调空间A」「二轮联调空间B-空配置」两个空间,`GET /knowledge-bases` 均为空 → **无命中的短路是这两个空间的常态**,不带文档提问永远看不到 id 落库。所以验收标准 3 后半在 dev 当前形态下**不可达**(要验需先给某空间上传一份文档,会往共享 dev 库留数据,不在本轮做),不是 A1 引入的回归;A1 也不依赖这条(前端不再靠 `model_id` 反查任何语义)。
- 测试期在 dev 空间「二轮联调空间A」(`104fa4d5-…`)自建的一次性会话 `cdf151e7-2c18-4958-a4ed-9f2cd7655083`(标题「只回一个字」)**已删除**(DELETE 204),该空间会话列表回到 1 条非本次产物;未新建模型/供应商,故无其它夹具需清理。
- 页面数据现状:`GET /models` 仍返回全局 ∪ 空间合并清单 —— 空间 A 下拉里同时看到 `deepseek/deepseek-chat`(全局)与 `prod-deepseek/deepseek-chat`(本空间);切到 0 行的空间 B 仍返回 1 条(纯全局),`defaults.chat` 也指它。因此"空清单 → 去配置 + 禁发"这条分支在 N1 之前**不可达**(已知,见 §6),W2 这一半只保证"必传、不兜底",**清单收窄与默认解析空间化仍依赖后端 N1/N2**。
- 切空间回归:空间切换后清单确实重拉(`ChatView.vue:288-301` 的既有 watcher → `loadModelCatalog`),空间 A→B→A 往返后选中项与「默认」徽标随清单校正,未把上一个空间的模型带过去;实测已把活动空间切回 A,浏览器会话状态与实测前一致。
