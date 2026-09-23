# M3 · API Key 接口说明(前端对接文档)

> **适用页面**:07-api-keys。**机器契约**:`docs/api/openapi.json`(字段/枚举唯一事实源)。
> 本文只写流程、边界与前端要点;与契约冲突时以契约为准,拿不准以实测为准。
>
> **⚠ OPT-41 权限档位已改这块契约(2026-09-22,分支 `feat/api-key-permission-groups`)**:
> 能力与知识库范围**不再挂在 Key 上**,而在**权限档位**上(表 `permission_groups`)。
> 本文已按新契约重写;档位的完整形状/端点/错误码请看
> `docs/api/给前端的契约变更说明-OPT41权限档位.md`(那份才是真源,本文只管 Key 页)。

---

## 1. 概念与取值(前端渲染要用)

- **Key 形态**:`sk-live-` 前缀 + 43 位随机串。列表里只显示 `key_hint`(形如
  `sk-live-****ab12`),**完整明文只在创建响应出现一次**,之后任何接口都拿不回。
- **权限档位 / 档位(本需求自造词,就地解释)**:一个空间内"一种权限"的定义 =
  能力清单 + 可检索知识库范围。一把 Key 必须挂在一个档位上,它的权限**逐字段来自档位**,
  Key 行本身没有任何权限字段。一个档位下可以有多把 Key(共享同一套权限、各自可轮换)。
- **能力 `capabilities`(在档位上,不在 Key 上)**;与原型页 07 两个勾选项一一对应:

| 值 | 原型文案 | 允许的路由 |
|---|---|---|
| `chat` | 对话检索 | `POST /ask`、`POST /retrieval/search`、`GET /conversations/{id}/stream`(断线续流) |
| `documents` | 文档管理 | KB 列表/详情(读)、文档 上传/列表/详情/删除/重解析、分块列表/单块 |

- **知识库范围(在档位上)**:`all_kbs = true` = 本空间**全部**库,每次请求实时展开
  (今后新建的库自动在范围内);`false` 时按 `kb_ids` 逐个勾,**空数组 = 一个库都不给**。
  旧口径"空 `kb_ids` = 不限制"**已作废**,别按老语义写判断。
- **归因成员 `owner_*`(OPT-26 起,OPT-41 明确它只管身份)**:Key 以这个人在空间内的
  身份行事(成员校验、消息归属、审计 actor、用量统计都落到 `owner_user_id`),
  **不决定权限**。签发/吊销要求 **Admin+**。`created_by*` 是签发操作者,仅留痕,
  不参与运行时身份(签发者被移出不影响 Key 可用)。
- **档位改权限 = 即时生效**:`perm` 是认证阶段从档位现算的,不是建 Key 时的快照 ——
  给档位加一个能力,挂在该档上的所有 Key 下一次请求就有;收掉则立刻 403。
- **吊销**:软删除(`revoked_at` 置值)。列表包含已吊销的 Key,前端建议置灰展示;
  吊销后用该 Key 的程序立即收到 401。
- **吊销 ≠ 删行**:Key 台账行**唯一**的硬删入口在档位侧 ——
  `DELETE /api/v1/spaces/{sid}/permission-groups/{gid}/revoked-keys`(OPT-44,Admin+,只认 JWT,
  只清该档位下已吊销的行,返回 `{deleted_count}`)。为什么要硬删:`api_keys.group_id` 的外键是
  `ON DELETE RESTRICT`,已吊销的行照样占着 ⇒ 不删行就删不掉档位。行一删,这把 Key 的归因
  只剩删除前写入的 `api_key.purged` 审计快照,所以**不可逆且不留明文**。
- **OPT-31 的 `is_active` / "正在使用" 已整条删除**:端点 `PUT /api-keys/{key_id}/activate`
  没了,字段 `is_active` 没了。"此刻用哪一档"改由档位侧的 `PUT /permission-groups/{id}/current`
  表达,且语义从台账标记升级为**真权限开关**。

## 2. 端点(均挂在空间下,角色门槛同页面权限)

| 方法/路径 | 说明 |
|---|---|
| `GET /api/v1/spaces/{space_id}/api-keys` | 列表(含已吊销);Viewer+ |
| `POST /api/v1/spaces/{space_id}/api-keys` | 创建(须指定档位 + 归因成员),**201 返回明文仅此一次**;**Admin+** |
| `DELETE /api/v1/spaces/{space_id}/api-keys/{key_id}` | 吊销(幂等,204);**Admin+** |
| `DELETE /api/v1/spaces/{space_id}/permission-groups/{group_id}/revoked-keys` | **硬删**该档位下已吊销凭据的台账行(200 `{deleted_count}`);OPT-44,**Admin+**、只认 JWT |

### 创建请求

```json
{
  "name": "ci-pipeline",
  "description": "CI 构建后同步接口文档",
  "group_id": "<permission_group_uuid>",   // ★ 必填:本空间现存档位
  "owner_user_id": "<user_uuid>"           // ★ 必填:该空间现存成员
}
```

- `group_id` 缺失 → 422;指向**别的空间**的档位 → 404(不泄露存在性)。
- `owner_user_id` 缺失 → 422;指向非本空间成员 → 404 `MEMBER_NOT_FOUND`。
- `name` 缺失/超长 → 422。**请求体里已经没有 `capabilities` / `kb_ids`**,前端表单
  要改成"选一个档位"(下拉数据源 `GET /spaces/{sid}/permission-groups`)。
- **建 Key 不会把 owner 挂进档位成员列表**(成员集合表达"哪些人可以把这档设成当前档"),
  所以"Key 建好了、这个人还是没有生效档位"是正常态,UI 要能看出来并指路到档位页。

### 创建响应(201)

```json
{
  "id": "<key_uuid>",
  "name": "ci-pipeline",
  "description": "CI 构建后同步接口文档",
  "key_hint": "sk-live-****ab12",
  "group": {
    "id": "<group_uuid>",
    "name": "只读问答档",
    "capabilities": ["chat"],
    "kb_ids": ["<kb_uuid>"],
    "all_kbs": false
  },
  "owner_user_id": "<user_uuid>",
  "owner_username": "alice",
  "owner_nickname": "小艾",
  "owner_role": 20,
  "created_by": "<issuer_uuid>",
  "created_by_name": "owner",
  "revoked_at": null,
  "last_used_at": null,
  "created_at": "2026-09-22T03:00:00Z",
  "plaintext": "sk-live-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**前端要点**:
- `group` 是列表/创建响应里**唯一的权限口径**(与 `/permission-groups` 同源同一套映射);
  权限列直接读它,不要再从 Key 行找 `capabilities`/`kb_ids`。
- `plaintext` 就是原型里「Key 已创建,请立即复制保存,这是唯一一次完整展示」的数据源;
  列表接口没有该字段,不要试图刷新后再取。

### 列表项

同上但**没有 `plaintext`**;`revoked_at` 非 null 即已吊销。归因成员已注销或被移出空间时
`owner_username` / `owner_nickname` 为 null(`owner_*` 的 id 仍在)——**用 `owner_username === null`
判"人已失效"并置灰**,这个口径和 OPT-26 时代一样,只是字段改了名(旧 `member_username`)。
存量旧 Key 没有 `owner_user_id` 时后端回退签发者,字段照常返回。

## 3. 用 Key 调接口(写进页面 07 的"使用说明"或文档提示)

程序方在请求头带 `X-API-Key: <明文>`(不是 Bearer):

```
POST /api/v1/spaces/{space_id}/retrieval/search
X-API-Key: sk-live-xxxx...
Content-Type: application/json

{"query": "检索词", "kb_ids": ["<kb_uuid>"]}   // kb_ids 可省略;档位有范围时会自动收窄
```

行为边界(前端文案可直接引用):

| 场景 | 返回 |
|---|---|
| Key 缺失/无效/已吊销/归因成员已离开空间或注销 | `401 API_KEY_INVALID`(按凭据问题处理,重试无用) |
| 用 Key 访问**只认 JWT** 的接口(如用户资料 `/users/me`、**Key 管理 `/api-keys`**、**档位管理 `/permission-groups`**、会话列表、`/api/v1/admin/*`) | `401 AUTH_REQUIRED`(和"没带凭据"同构 —— 这些路由压根不看 `X-API-Key`,改权限/管凭据只能由人来) |
| 用 Key 访问"接受 Key 但没登记进能力表"的路由 | `403 API_KEY_CAPABILITY_DENIED`(`details.reason = "route_not_grantable"`)。**当前无此路径**:12 条接受 Key 的双认证路由与能力表的 12 个条目一一对应,这是给未来新增路由留的 fail-closed 护栏(`api_key_auth.py:243-250`,测试 `test_api_key_integration.py:165-178` 钉住) |
| Key 所在空间与路径 `{space_id}` 不一致 | `403 API_KEY_SPACE_MISMATCH`(`details.reason = "space_mismatch"`,跨空间越权闸,OPT-41 N2) |
| 档位未授对应能力(如只有 chat 却调文档接口) | `403 API_KEY_CAPABILITY_DENIED` |
| 档位 `all_kbs=false` 且 `kb_ids=[]`(一个库都不给) | `403 API_KEY_SCOPE_DENIED`(`details.reason = "empty_scope"`) |
| 请求的 kb_ids 全在档位范围外 | `403 API_KEY_SCOPE_DENIED`(`details.reason = "out_of_scope"`,**不静默降级**) |
| kb_ids 部分在范围内 | 放行,后端**自动收窄到交集**(不报错) |
| `kb_ids` 省略且档位有范围 | 等效于"只查范围内全部" |
| 归因成员被降级后调用超出其新角色的路由 | `403 FORBIDDEN`(身份实时跟随成员角色) |

- 401/403 都走统一错误壳;403 会被记录到空间审计(`access.denied`)。
- **Key 以归因成员身份行事 + 以档位为权限口径**:成员被移出/注销 → Key 立即失效(401);
  成员被降级 → 高权限路由即时 403;档位的能力与范围限制在成员权限**之上**再收窄。
  签发者(创建人)离开空间**不影响** Key 可用性。
- **`/permission-groups` 与 `/api-keys` 只认 JWT**,刻意没登记进 Key 的路由授权表 —— 第三方拿到 401
  是设计而非 bug,别当接口坏了排查(2026-09-23 dev 实测:`X-API-Key` 打档位端点 = `401 AUTH_REQUIRED`)。

## 4. 前端页面(07)检查单

- [ ] 列表列:名称 / Key(`key_hint`,mono 样式)/ **档位(`group.name`,并给"去档位页"入口)**/
      归因成员(`owner_nickname(owner_username)`,null → "已注销"并置灰该行)/
      **权限(读 `group.capabilities` + `group.all_kbs`/`group.kb_ids`,badge:`chat`→对话检索、
      `documents`→文档管理;`all_kbs=true` → "全部知识库(含今后新建)",否则按 KB 列表解析名称,
      取不到就显示数量)**/ 签发人(`created_by_name`)/ 创建时间 /
      最近使用(`last_used_at` 为 null 显示"从未使用")/ 操作(吊销;已吊销置灰)
- [ ] 新建弹窗:**档位下拉(必填,数据源 `GET /spaces/{sid}/permission-groups`)+ 归因成员下拉(必填)**;
      不再有能力勾选与 KB 多选(那是档位页的事)。表单里给一句提示:"想改权限请到权限档位页改这一档"
- [ ] 创建成功弹窗:展示 `plaintext`,文案"仅此一次";关闭后列表只出现 `key_hint`
- [ ] 吊销二次确认:文案说明"立即 401、不可恢复"(见原型)
- [ ] 三态与权限:列表 loading/空/错误;创建/吊销入口按 **Admin+** 显隐(列表 Viewer+ 可看);
      403/404 **直显服务端 `message`**(后端文案已是成品,例如"该档位下还有 3 把 API Key(2 把在用的先吊销、1 把已吊销的先清空历史凭据),处理完才能删档" —— 这句出现在**档位页**的删档动作上,Key 页只会出现吊销/签发的错)
- [ ] 契约同步:`docs/api/openapi.json` 的 `ApiKeyOut` / `ApiKeyCreatedOut` /
      `CreateApiKeyRequest` / `ApiKeyGroupRef`
