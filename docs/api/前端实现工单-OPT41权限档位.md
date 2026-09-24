# 前端实现工单 — OPT-41 权限档位（F 片）

> 契约真源：`docs/api/给前端的契约变更说明-OPT41权限档位.md`（下称"契约"）。本单只列**怎么做**，字段/错误码以契约为准。
> 工作区：`web/`（Vue 3 + TS + Element Plus，无 i18n，中文文案内联；组件与 ElMessage 自动导入，图标要手写 import）。
> 门禁：`make web-build`（= `vue-tsc -b && vite build`），**前端零测试**，所以门禁绿 + 浏览器实测是唯一验收口。

## 0. 当前状态（先对齐再动手）

- ✅ 已完成：`web/src/api/permissionGroups.ts`（88 行，6 个函数）、`web/src/api/types.ts` 档位/Key 新类型、`web/src/api/apiKeys.ts`（删 `apiActivateApiKey`）—— **均未提交**。
- ❌ 未完成：视图层一行未动 → **现在 `make web-build` 必然红**（`ApiKeysView.vue:4` 还 import 已删函数、`:245` 还传 `member_user_id`）。
- 🚫 本单禁止：改后端、改迁移、动 `auto-imports.d.ts` / `components.d.ts`（vite 自动维护）、给 `router/index.ts` 的档位路由加 `adminOnly`（契约 D1）。
- ⚠️ 待确认（未点头前不开工该条）：`SpaceView.vue` 移除弹窗的 `keep_mounts` 默认勾法（契约 D3 已给推荐"一并清除"）。

## 1. 施工顺序（每片单独提交，片内跑门禁）

```
F1 API 层与类型（收尾+提交）  →  F2 档位管理页(新建)  →  F3 ApiKeysView 返工
→ F4 成员移除二选一  →  F5 问答页/审计页适配  → F6 门禁 + 浏览器实测
```

理由：F2 产出档位列表页后，F3 的"选档位"下拉才有地方跳；F3 是编译断裂主源，紧随其后跑一次 `web-build`。

---

## 2. F1 · API 层收尾（`web/src/api/`）

1. `permissionGroups.ts` 现有 6 个函数已对齐后端 8 条路由（缺 `GET /{group_id}` 单条详情 —— 列表已含 members，**不补**，避免多余接口）。
2. `types.ts` 复核 `ApiKeyGroupRef` 与契约 §3 逐字段一致（`group`、`owner_*`）。
3. `spaces.ts:47` `apiRemoveMember` 加第三参：

```ts
/** OPT-41:keepMounts=true 保留此人的档位挂载行(再拉回来即恢复);默认 false=一并清除 */
export function apiRemoveMember(spaceId: string, userId: string, keepMounts = false): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/members/${userId}`, {
    params: { keep_mounts: keepMounts },
  }).then((r) => r.data)
}
```

## 3. F2 · 新页 `web/src/views/permissionGroups/PermissionGroupsView.vue`

骨架直接复用 `ApiKeysView.vue` 的三态（错误 `:369-376` / 空 `:379-391` / 表格 `:395-465`）与 `.ak-*` 样式（`:577-867`），弹窗范式照 `components/KbFormDialog.vue`（`defineModel<boolean>` + `watch(visible)` 里 `clearValidate()` + 422 回填 `fieldErrors`）。

**列表列**：名称 / 描述 / 能力（`capabilityLabel`，空数组显示灰色"无能力（什么都不能做）"）/ 知识库范围 / 成员数（含 current 徽标）/ 创建时间 / 操作。

**范围列口径（务必按契约 §1 fail-closed 表写）**：
- `all_kbs === true` → "全部知识库（含今后新建，实时展开）"
- `all_kbs === false && kb_ids.length` → "指定 {n} 个库"，hover 展开库名
- `all_kbs === false && !kb_ids.length` → 红色徽标"**不含任何知识库**"

**建档/编辑弹窗**（创建与 PATCH 共用一个 `UpsertPermissionGroupRequest`，**整组替换**）：
- `name`（1–64，必填）、`description`（≤512）。
- `capabilities` 复选：`chat` = 对话检索、`documents` = 文档管理；零勾选允许提交，但要 inline 提示"等于什么都不能做"。
- `all_kbs` 开关 + `kb_ids` 勾选区**互斥**：开关为真时禁用并清空勾选（后端会 422，前端先拦）；开关为假且零勾选时，确认按钮上方给红字"这一档不会检索到任何知识库"。**"全部库"必须显式勾选，漏填不能变成放开全空间。**
- 编辑时把 `members` 里 `role === null` 的行置灰并标"已移出空间"。

**成员抽屉**（每档一个）：
- 顶部显示本空间成员下拉（`apiListMembers`，已在挂载的置灰）→ `apiAssignGroupMember`。文案明确"分配 ≠ 生效，生效要点『设为当前』"。
- 每行两个动作：`设为当前`（`apiSetCurrentGroup`，`is_current` 行显示徽标并禁用按钮）/ `解除分配`（`apiUnassignGroupMember`）。
- **解除当前档必须二次确认**，文案："解除后此人没有生效档位，问答与检索会立即被拒（fail-closed），不会自动换到别的档。"
- 换档是权限开关：确认文案要写"会改变 {人} 此刻可检索的库范围"。

**删除档位**：二次确认里带后端给的 `key_count`/`member_count` 语义；400 的 `message` 直显（服务端已给成品中文），不要前端自己拼。

**登记点**：
- `web/src/router/index.ts` 在 `api-keys`（`:61-66`）之后插一条 `permission-groups` 懒加载路由，`meta: { title: '权限档位', desc: '一档 = 一种能做什么 + 能看哪些库 · 写操作需 Admin 及以上' }`，**不加 `adminOnly`**。
- `web/src/components/AppSidebar.vue` 在 `:132-134`（API Key 项）之后插 `nav-item`，图标从 `@element-plus/icons-vue` 补 import（顶栏标题走 `AppLayout.vue:27-28` 读 `route.meta`，无需额外面包屑）。

## 4. F3 · `ApiKeysView.vue` 返工（编译主断点）

必删：`:4` 的 `apiActivateApiKey` import、`:298-325` `onActivate`/`activatingId`、`:446-459` 操作列"使用中/设为正在使用"、`:400` 的 `row.is_active` 徽标、`:467-474` 底部三态统计里 active/unused 两档、`:87-92` `FILTER_OPTIONS` 对应项（保留 全部/未吊销/已吊销）、`:97-107` `keyStatus`/`statusCounts` 里 `is_active` 分支、`:127` 按 current 排序的键。

必改（字段搬家，不改逻辑形状）：
- `member_username/nickname/role` → `owner_username/owner_nickname/owner_role`（`:115` 搜索、`:120-126` 排序、`:137` `isDeadKey`、`:144-148` `memberLabel`、`:410-418` 列）。列名"绑定成员"→"**归因成员**"，并在 `:133-137` 的注释里改成 `owner_username` 口径。
- 权限两列改读 `row.group`：`:424-429` → `row.group.capabilities`；`:430-434` `kbScopeLabel(row)` 改消费 `group.all_kbs` / `group.kb_ids`。
- 新增列"**所属档位**"（`row.group.name`，点击跳 `/permission-groups`）。
- 创建弹窗（`:479-573`）：表单 `:46-53` 加 `groupId`、`memberUserId`→`ownerUserId`、删 `capabilities`/`kbIds`；`:511-521` 能力复选整段删；`:522-545` 知识库勾选整段删；换成**档位下拉 + 只读预览**（能力 + 范围 + "改权限请去权限档位页"链接）。`:215-222` `canSubmit` 改为 `name && groupId && ownerUserId`；`:242-249` body 传 `group_id` + `owner_user_id`。
- 顶部说明条 `:341-349` 与页头注释 `:13-22` 重写："Key 只是凭据，权限看档位；一把档位可签多把 Key，轮换互不影响；改档位即时对其下全部 Key 与新范围生效。"
- `:187` `loadKbs` 保留（档位预览文案要把 `kb_ids` 翻成库名）。

## 5. F4 · `SpaceView.vue:128-142` 移除成员二选一

`ElMessageBox.confirm` 的"确定/取消"不够表达三态，改成小弹窗（`el-radio-group` 两项）：
- 默认选中"**一并清除其档位挂载**"（`keep_mounts=false`，与后端默认一致）；
- 备选"保留挂载行（此人重新入空间即恢复上次权限）"+ 风险说明。
审计已覆盖两种选择（detail 键 `keep_permission_mounts`），弹窗里提一句"两种操作都会记入审计"。

## 6. F5 · 问答页与审计页适配

1. `ChatView.vue:157-162` `kbScopeLabel`：现在"零选中显示为全部"，与新口径冲突风险 → 改为显示"**档位允许的全部库**"，并在下拉（`:980-999`）顶部加一行"当前生效档位：{name}"（数据源：`apiListPermissionGroups` 里 `members` 含 `user_id===自己 && is_current` 的那一档；找不到 = 无档位态）。
2. `:493` 的 `kb_ids: 全勾 ? null : selectedKbIds` **保持不变**（契约 §4.2 钉死 null = 档位允许的全部）。
3. 错误分码（`:501-516` + `streamErrorMessage :407-411`）新增：`PERMISSION_GROUP_REQUIRED` / `API_KEY_SCOPE_DENIED` / `API_KEY_CAPABILITY_DENIED`（`details.actor === 'member'`）三类 → 直显后端 `message` + 给"查看我的档位"跳转，**不要**复用"请重新登录"或"角色不足"的现成话术。
4. `AuditView.vue:40-64` `ACTION_LABELS` 补 6 条 `permission_group.*` 中文标签，并在 `:67-87` 前缀分组表加 `['permission_group.', '权限档位']`；`api_key.activated` 的旧标签保留（历史行仍要能渲染）。

## 7. F6 · 验收

**门禁**：`make web-install`（当前 `web/node_modules` 不存在）→ `make web-build` 必须绿。

**浏览器实测清单**（后端灰度先关着跑一轮，再 `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE=1` 跑一轮）：
1. 建档（只勾 `chat` + 2 个库）→ 分配某人 → 设为当前 → 用该人登录，问答页显示档位名且库下拉只剩那 2 个库。
2. 同档再签第二把 Key：两把 Key 的 `group` 一致；吊销其一不影响另一把。
3. 建 Key 不选人/不选档 → 前端拦；绕过前端直调 → 422。
4. 改档位（把范围从 2 个库放成全部库）→ **不重发 Key**，第三方调用范围即时变化；审计 `permission_group.updated` 的 before/after 两栏可见。
5. 删档：挂着 Key → 400 文案直显；解除全部成员与吊销全部 Key 后删成功。
6. 解除某人当前档 → 灰度开启时他问答页立刻 403 `PERMISSION_GROUP_REQUIRED`，话术是"联系管理员配档"，**且没有被踢回登录页**。
7. 移除成员（清除 / 保留两选一）→ 重新拉回该成员：清除版需要重配档，保留版上次档位自动还在（`role===null` 置灰解除）。
8. Viewer 成员登录档位页：能看到列表，看不到任何写按钮，直接调写接口 403。
