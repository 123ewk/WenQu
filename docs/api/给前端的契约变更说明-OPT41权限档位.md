# 给前端的契约变更说明 — OPT-41 权限档位（破坏性变更，必读）

> 后端已落地（分支 `feat/api-key-permission-groups`，A–E 五片提交，迁移到 `0018`）。
> 本文件是**唯一契约真源**，行号取证自当前工作区快照，改代码前请先跳读一遍再对照 `docs/api/openapi.json`。
> 配套工单：`docs/api/前端实现工单-OPT41权限档位.md`。

## 0. 一句话变更

**权限从"挂在 API Key 上"搬到"挂在权限档位上"。** Key 退化成一份可轮换的凭据（一把档位下可以有多把 Key），人也可以通过"挂载"关系挂到档位上。前端所有"这把 Key 能干什么 / 能看哪些库"的展示与表单，都要改成读**档位**。

## 1. 术语（本需求自造词，首次出现就地解释）

| 术语 | 含义 | 表/字段 |
| --- | --- | --- |
| 权限档位 / 档位 | 一个空间内"一种权限"的定义 = 能力清单 + 可检索知识库范围 | 表 `permission_groups`，列 `capabilities` / `kb_ids` / `all_kbs` |
| 挂载 / 分配 | "这个人可以用这一档"的一条关系行 | 表 `permission_group_members` |
| current（当前档） | 每人每空间**至多一个**生效档位，决定他此刻的可检索范围 | 挂载行的 `is_current` 布尔列（部分唯一索引兜底） |
| 归因成员 owner | Key "以谁的身份行事 + 调用记在谁头上"，**不再决定权限** | `api_keys.owner_user_id`（旧名 `member_user_id`，迁移 0018 改名） |
| `all_kbs` | true = 本空间**全部**知识库，每次请求实时展开（新建的库自动在范围内） | 档位列，布尔 |
| fail-closed（默认拒绝） | 空值一律理解为"什么都不给"，不是"不限制" | 能力为空 = 不能做任何动作；`all_kbs=false` 且 `kb_ids=[]` = 一个库都不给 |
| 双凭据强制清单 | 一期要求"人也必须有生效档位"的接口集合（问答/检索/续流 + 文档增删/重解析 + 相关只读） | 后端 `api_key_auth.py:61-126` 两张表 |
| 动作闸 / 范围闸 | 动作闸 = 这个路由要不要某个能力；范围闸 = 这个路由受不受档位知识库范围约束 | 只读路由放范围闸、不放动作闸（刻意） |

## 2. 新增端点：`/api/v1/spaces/{space_id}/permission-groups`

契约源：`server/src/app/api/routes/permission_groups.py`。**这组端点只认 JWT**(依赖是 `get_current_user`),刻意没登记进 Key 的路由授权表 → 程序凭据打它会拿到 **`401 AUTH_REQUIRED`**(2026-09-23 dev 实测订正:原先写 `403 route_not_grantable` 是错的 —— 纯 JWT 路由上 Key 连授权表都进不到)。**结论不变**:改权限不能由凭据自己发起。
`403 API_KEY_CAPABILITY_DENIED + details.reason="route_not_grantable"` 是**给未来留的 fail-closed 护栏**（`api_key_auth.py:243-250`）：只有"接受 Key 的双认证路由（`get_current_actor`）没登记进能力表"时才命中。**当前代码里一条这样的路由都没有** —— 12 条接受 Key 的双认证路由(`/ask`、`/retrieval/search`、`/conversations/{id}/stream`、知识库与文档的读写)与能力表的 12 个条目一一对应,所以前端不会真的看到它;真出现了按"后端漏登记"处理,不是权限配错。测试钉住了这个口径（`tests/test_api_key_integration.py:165-178`）。

| 方法 + 路径 | 门槛 | 请求体 | 响应 | 备注 |
| --- | --- | --- | --- | --- |
| `GET ""` | VIEWER+ | — | `PermissionGroupOut[]` | 含成员列表 |
| `POST ""` | ADMIN+ | `UpsertPermissionGroupRequest` | `PermissionGroupOut`（201） | — |
| `GET /{group_id}` | VIEWER+ | — | `PermissionGroupOut` | 跨空间一律 404（不泄露存在性） |
| `PATCH /{group_id}` | ADMIN+ | `UpsertPermissionGroupRequest` | `PermissionGroupOut` | **整组替换**（PUT 语义走 PATCH 方法名），半字段更新不支持 |
| `DELETE /{group_id}` | ADMIN+ | — | 204 | 挂着 Key 或成员时 **400 拒绝**，见 §5 |
| `POST /{group_id}/members` | ADMIN+ | `{ user_id }` | `PermissionGroupOut` | 幂等；**分配 ≠ 生效** |
| `DELETE /{group_id}/members/{user_id}` | ADMIN+ | — | 204 | 未挂载 → 404；解除的正是他当前档 → current 一并消失（fail-closed，不是 bug） |
| `PUT /{group_id}/current` | ADMIN+ | `{ user_id }` | `PermissionGroupOut` | 换档 = 权限开关，原 current 自动让位；此人未挂在档上 → 400 |

```ts
// 响应结构（与 server 逐字段对齐）
interface PermissionGroupOut {
  id: string
  name: string
  description: string
  capabilities: string[]   // 'chat' | 'documents'，空数组 = 什么都不能做
  kb_ids: string[]         // all_kbs=false 时生效；[] = 一个库都不给
  all_kbs: boolean
  members: GroupMemberOut[]
  created_at: string | null
}
interface GroupMemberOut {
  user_id: string
  username: string | null   // null = 账号已注销
  nickname: string | null
  role: number | null       // null = 已被移出本空间 → 该行置灰(挂着也没权限)
  is_current: boolean       // 每人每空间至多一个 true
}
// 创建 / PATCH 共用（整组替换）
interface UpsertPermissionGroupRequest {
  name: string              // 1–64
  description?: string      // ≤512
  capabilities: string[]    // ≤8 项；未知值后端 422
  kb_ids: string[]          // ≤50；all_kbs=true 时必须留空
  all_kbs: boolean
}
```

## 3. 破坏性变更：API Key 契约

契约源：`server/src/app/api/routes/api_keys.py:36-76`。

**`ApiKeyOut` 删掉的字段**（前端任何一处引用都会是死代码）：
`capabilities`、`kb_ids`、`is_active`、`member_user_id`、`member_username`、`member_nickname`、`member_role`。

**`ApiKeyOut` 现在的字段**：

```ts
interface ApiKeyOut {
  id: string; name: string; description: string; key_hint: string
  group: { id: string; name: string; capabilities: string[]; kb_ids: string[]; all_kbs: boolean } // ★ 唯一的权限口径
  owner_user_id: string | null   // 归因成员；存量旧 Key 回退签发者；账号注销后 null
  owner_username: string | null  // null = 注销/被移出 → 用它判"已注销"并置灰
  owner_nickname: string | null
  owner_role: number | null      // 本空间角色，实时值（随角色变化）
  created_by: string | null; created_by_name: string | null  // 仅留痕
  revoked_at: string | null; last_used_at: string | null; created_at: string | null
}
interface CreateApiKeyRequest {
  name: string; description?: string
  group_id: string        // ★ 必填：本空间现存档位，缺失 422，非本空间 404
  owner_user_id: string   // ★ 必填：该空间现存成员
}
```

- 语义提醒：`owner_username` 为 null 判"人已失效"的口径**没变**，只是改了名（旧 `member_username`）。被移出空间时 id 仍在、只有 username/nickname 变 null（OPT-26 实测结论，仍然成立）。
- **删除的端点**：`PUT /api-keys/{key_id}/activate` 整条没了。OPT-31 的"正在使用"由 `PUT /permission-groups/{group_id}/current` 表达，且语义从"Key 上的台账标记"升级为"真正的权限开关"。
- **签发不再自动挂人**：建 Key 不会把 owner 加进档位成员列表（成员集合表达"哪些人可以把这档设成当前档"，给共享档位再签一把 Key 不该顺手扩它的成员）。所以"新建 Key 后该成员仍无当前档"是正常态，UI 要能看出来。
- 旧行为"空 `kb_ids` = 不限制"已作废：新口径里"全部库"必须是 `all_kbs = true`（迁移 0017 已按 `all_kbs = (旧 kb_ids 为空)` 回填，所以存量 Key 行为零变化）。

## 4. 运行时语义（前端必须知道的三条）

1. **知识库下拉会自动变成"档位允许的库"** —— 但只在灰度开关打开后（见 §6）。开关关着时人链路 `perm = None`，`GET /knowledge-bases` 返回全量。所以前端**不能只靠后端收窄**，问答页应显式告诉用户"当前生效档位：X"。
2. **`kb_ids: null` 的含义钉死了**：发问/检索请求体里的 `kb_ids` 传 null = "取我档位允许的全部库"（不是"全空间"）。传了范围外的库不会静默降级，直接 403（`details.reason = "out_of_scope"`）。
3. **能力/范围不足的拒绝发生在服务端**，前端只做话术：不要用"你角色不够"去解释 403，档位不足和角色不足是两回事。

## 5. 错误码与文案

契约源：`server/src/app/core/errors.py`、`effective_permission.py:64-103/149-168`、`permission_groups.py:172-205/287-308`。响应统一壳 `{ error: { code, message, details } }`（`http.ts` 的 `ApiError` 已能解）。

| code | HTTP | 何时出现 | 建议话术 |
| --- | --- | --- | --- |
| `PERMISSION_GROUP_REQUIRED` | 403 | 灰度开启后，登录成员没有生效档位（或档位已被删） | 直显 `message`（后端已给"本空间还没有为你分配权限档位，请联系管理员"）。**刻意不用 401** —— 401 会被 `http.ts` 的刷新链踢成重新登录 |
| `API_KEY_SPACE_MISMATCH` | 403 | 第三方拿 A 空间的 Key 打 B 空间的接口(`details.reason = "space_mismatch"`) | **人链路不会出现**(它是 Key 专用的越权闸,`api_key_auth.py:234-241`);前端只需按统一壳直显 message,别归类成"你没权限" |
| `API_KEY_CAPABILITY_DENIED` | 403 | 档位没这个能力（Key 打了未登记的双认证路由也会是这个码，但当前无此路径，见 §2） | 按 `details.actor` 分文案：`member` → "你的档位「{group_name}」未授予{能力}"；`api_key` → 第三方语义，不改 |
| `API_KEY_SCOPE_DENIED` | 403 | 档位没勾任何库（`empty_scope`）或请求指向范围外库（`out_of_scope`） | 直显 `message`，并给"去档位页看我的范围"入口 |
| `VALIDATION_ERROR` | 400/422 | 删档时挂着 Key/成员；换档前人未分配；`all_kbs=true` 还传了 `kb_ids` | **服务端 message 已是中文成品**（例："该档位下还有 3 把 API Key,请先吊销或改指到别的档位"），直显，不要前端再造一套判定。`details` 带 `key_count` / `member_count` 可用于二次确认弹窗。**一个坑（2026-09-23 dev 实测）**：`key_count` **含已吊销**的 Key，而"吊销"是 Key 唯一的下线手段 ⇒ 签发过 Key 的档位实际上删不掉，文案里的"请先吊销"是无效动作。前端**不要**在这条 400 上放"去吊销"按钮，直显 message 即可；语义待拍板（台账 OPT-44） |
| `AUTH_REQUIRED` | 401 | 没带 JWT 或 JWT 失效；**以及第三方拿 `X-API-Key` 去打"只认 JWT"的路由**（`/permission-groups`、`/api-keys`、`/users/*`、`/admin/*`、会话列表等） | 前端只会以第一种形态出现（刷新链跳登录）。第二种是第三方接入方的误用，与前端无关，见 §2 |
| `FORBIDDEN` | 403 | 角色不足（写操作要 ADMIN+） | "仅管理员可操作" |
| `NOT_FOUND` / `MEMBER_NOT_FOUND` | 404 | 档位不在本空间 / 目标不是本空间成员 | "档位不存在或无权访问" |

`API_KEY_CAPABILITY_DENIED` / `API_KEY_SCOPE_DENIED` 沿用了 Key 那一对码，所以**已接入的第三方零回归**；人链路靠 `details.actor = "member" | "api_key"` 区分。

## 6. 灰度开关（决定前端要不要做"无档位"态）

`APP_REQUIRE_PERMISSION_GROUP_FOR_CORE`，**默认 0（关）**（`config.py:76-84`、`deps.py:154-180`）。

- 关：人（JWT）只看角色，和今天完全一致；`PERMISSION_GROUP_REQUIRED` 不会出现。
- 开：清单内接口要求人也有生效档位。上线前必须先给每个真人成员配档并设 current，否则那一刻全员掉进禁用态（需求单 Q6 选的"无档位即全禁"）。
- 只影响人链路；Key 链路一直都按档位收口。

**结论：前端必须实现"无档位"的禁用态文案**，因为开关一开就是生产行为，而不是等前端改造完才开。

## 7. 附带变更（容易漏）

1. **移除成员多了查询参数** `DELETE /spaces/{sid}/members/{uid}?keep_mounts=false|true`（`routes/spaces.py:247-259`）。默认 false = 一并删掉此人的档位挂载行（推荐：留行会在他重新入空间时悄悄带回上次权限）；true = 保留，再拉回来即恢复。两个选择都进审计（detail 键 `keep_permission_mounts`）。→ 移除成员的二次确认要变成**二选一**，不能只是"确定/取消"。
2. **审计新增 6 个动作码**（`domain/enums.py:76-81`）：`permission_group.created` / `.updated` / `.deleted` / `.member_assigned` / `.member_unassigned` / `.current_set`。`views/audit/AuditView.vue:40-64` 的 `ACTION_LABELS` 未登记 → 现在会原样显示英文码。改权限那一条 detail 带 `capabilities_before/after` + `kb_scope_before/after`（回答"谁把 3 个库放成全部库"）。
3. `api_key.activated` 枚举值**保留但后端不再写**（历史审计行反序列化要用），前端 label 留着，别删。
4. 旧的 `docs/api/m3-api-keys-接口说明.md` 里 activate / `is_active` 段落已过期，本次一并订正。

## 8. 待拍板（默认值我按推荐写进工单，如不同意请说）

| 编号 | 问题 | 采用 |
| --- | --- | --- |
| D1 | 档位管理页要不要 `adminOnly` 路由守卫？ | **不要**。读是 VIEWER+、写是 ADMIN+，与 api-keys 页一致用页内 `canManage` 控显隐；`router/index.ts:109-112` 的 adminOnly 文案还写死了"审计日志" |
| D2 | Viewer 成员进档位页看什么？ | 能看档位列表（后端允许），成员抽屉只读、写操作按钮 `v-if="canManage"` 隐藏 |
| D3 | 移除成员弹窗默认勾哪个？ | 默认"**一并清除**"（与后端默认 `keep_mounts=false` 一致），勾"保留"需额外说明风险 |
