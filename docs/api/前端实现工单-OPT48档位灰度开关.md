# 前端接入记录 — OPT-48 档位灰度开关（N7 片）

> 后端已随本分支交付并自测;页面 UI 按用户 2026-09-26 拍板**由本条线顺手实现**(不再是"只出工单")。
> 本文档 = 契约变更说明 + 前端实现记录,按惯例给四段(已完成/未完成/禁测/报告)。

## 1. 契约变更(已随 docs/api/openapi.json 重导)

### 端点

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/admin/permission-enforcement` | 读当前生效值、来源与影响面统计 |
| `PUT /api/v1/admin/permission-enforcement` | 拨动;body `{ "enabled": boolean }`;成功回同 GET 结构 |

### 权限口径(2026-09-26 用户修订,替代需求单 D2"仅 superuser")

- **superuser 或 任意空间 Admin+(role≥30)** 可读可拨(守卫 `get_enforcement_manager`);
- **效果是全局的(D1)**:打开 = 整个实例所有空间的真人成员在核心接口(问答/检索/文档增删)
  都必须有"生效档位"。**页面任何位置都必须写明"影响整个实例所有空间"**,不能让用户误以为只影响当前空间;
- `GET` 对有资格的人恒 200;拦人只发生在 `PUT`(403/409)。

### 响应结构

```jsonc
{
  "effective": true,                 // 当前生效值(库里没行 = env 兜底值)
  "source": "db",                    // "db" = 页面拨过; "env_fallback" = 库里无行,回落环境变量
  "updated_at": "2026-09-26T...",    // 库里无行恒 null
  "updated_by_hint": "demo",         // 最后拨动人用户名;库里无行恒 null
  "impact": {
    "spaces": 62,                    // 全实例空间数
    "members_total": 68,             // 全实例成员关系数(同一人多空间各算一条)
    "members_without_active_group": 8, // 没有生效档的成员关系数(打开后立即被拦,只统计不拦)
    "actor_has_active_group": true,  // 操作者本人是否处处有生效档
    "actor_spaces_without_group": [ { "space_id": "...", "space_name": "演示空间" } ]
  }
}
```

### 错误

| 状态 | code | 文案(后端返回) | 前端提示要求 |
|---|---|---|---|
| 403 | `FORBIDDEN` | 「仅空间管理员(Admin)及以上可拨动此实例级开关」 | 只提示无资格;**不得**复用"管理模型"类文案误导 |
| 409 | `PERMISSION_ENFORCEMENT_ACTOR_HAS_NO_GROUP` | 「打开前先给自己配好生效档位…」 | 用 `details.spaces[].space_name` 列出缺档空间,引导先给自己配档 |

### 读值语义(前端展示用)

库里**没有**配置行时,`effective` 来自环境变量 `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE`(默认 false),
`source = "env_fallback"`、`updated_at/updated_by_hint = null` —— 此时应提示"当前值来自环境变量兜底,
页面从未拨过"。PUT 成功后本进程立即生效;其它实例进程最多 60s 后跟上(缓存 TTL),不影响正确性。

## 2. 前端实现(本条线已做,`make web-build` 绿)

| 文件 | 改动 |
|---|---|
| `web/src/api/types.ts` | 追加 `EnforcementSpaceGapOut / EnforcementImpactOut / EnforcementStatusOut / EnforcementToggleRequest` |
| `web/src/api/permissionEnforcement.ts` | 新 API 层:`apiGetPermissionEnforcement / apiPutPermissionEnforcement` |
| `web/src/views/settings/PermissionEnforcementView.vue` | 新页 `/settings/permission-enforcement`:状态卡(开关 + 来源 + 拨动人/时间)、影响面统计、打开前二次确认(elMessageBox,确认文案含"影响整个实例"+无档人数)、403 空态、409 按缺档空间提示 |
| `web/src/router/index.ts` | 注册路由;**刻意不用 `adminOnly` meta**(那是按当前空间角色判的,这里是"任意空间 Admin+"口径) |
| `web/src/components/AppSidebar.vue` | 侧栏入口「档位灰度开关」,`v-if="任意空间 role≥30"`,data-testid `nav-permission-enforcement` |

入口显隐口径:`auth.spaces.some(s => s.role >= 30)`。纯 superuser 且没有任何空间身份的账号
看不到入口(后端仍允许其调 API);若有此类账号需要页面入口,给 `UserOut` 加 `is_superuser`
字段后把 `canManage` 并上即可(本次未做,见"未完成")。

## 3. 四段

### 已完成

- 契约重导 + 类型同步 + API 层 + 页面 + 路由 + 侧栏入口,`make web-build`(vue-tsc + vite)通过;
- 页面文案满足 D1 要求:标题下副标、el-alert 警告、确认弹窗三处都写明"影响整个实例所有空间";
- 409/403 分流提示(见 §1 表);来源 `env_fallback` 与 `db` 在页面上可区分。

### 未完成

- `UserOut.is_superuser` 未加:纯 superuser、零空间身份的账号页面看不到入口(API 可用)。
  dev 现实里 superuser(如测试夹具)要么有空间要么直接调 API,暂无真实需要,留待出现再补;
- 多 worker 的"其它进程 ≤60s 跟上"只在页面上以文案说明,未做跨进程实测(集成测试覆盖了
  本进程失效路径,跨进程 TTL 属部署形态,单测无法等价)。

### 禁测

- 不在共享 dev 库上执行 `alembic upgrade 0022`(三 worktree 共用,升库前必须先备份并经用户点头);
- 不动 :5173(该端口服务 demo-backend 的前端,本分支页面未部署在那里);
- 不做"批量清理 API Key"等破坏性验证。

### 报告

- 门禁:`make web-build` 绿(vue-tsc 类型检查 + vite 构建通过);后端侧 `server-lint`/`server-test`
  581 passed、`server-openapi` 已重导(见台账 OPT-48 行);
- 真机点检(浏览器实际拨开关)按用户拍板留专门一轮:届时需先让本分支后端跑起来(另起端口或
  切 :8000),且共享 dev 库需升 0022(先备份、先问用户)。
