# 前端接入记录 — OPT-48 v2 权限挡位功能开关（N7/R3 片）

> **v2 现行（2026-09-26 用户重述需求）**：开关改为**每空间一个**，移入权限档位页；
> 初版"实例级独立页 + 全局开关"契约作废。本文档只描述 v2。

## 1. 契约（已随 docs/api/openapi.json 重导）

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/spaces/{space_id}/permission-group-toggle` | 读本空间开关状态与影响面（本空间 Admin+，其余 403） |
| `PUT /api/v1/spaces/{space_id}/permission-group-toggle` | 拨动；body `{ "enabled": boolean }`；成功回同 GET |

### 响应结构

```jsonc
{
  "enabled": true,
  "updated_at": "2026-09-26T...",   // 表无行 = 从未拨过 = 恒 null
  "updated_by_hint": "uitester01",  // 最后拨动人用户名
  "impact": {
    "members_total": 3,              // 本空间成员关系数
    "members_without_active_group": 3,
    "actor_has_active_group": false  // 操作者本人自查
  }
}
```

### 语义与错误（v2 四项拍板）

1. **拦截范围 = 核心功能清单**：打开后仅问答/检索/会话流/知识库与文档要求生效挡位
   （403 `PERMISSION_GROUP_REQUIRED`）；成员/模型配置/审计/用量等管理页继续只看角色；
2. **谁能拨**：本空间 Admin+(role≥30)，superuser 兜底；他人 403
   「仅空间管理员(Admin)及以上可拨动本空间的权限挡位功能开关」；
3. **自锁不拦**：没挡也能开（`actor_has_active_group=false` 时前端弹窗提示后果即可）；
4. **配挡时机**：开关未打开的空间，`POST /permission-groups/{gid}/members`（分配）与
   `PUT /permission-groups/{gid}/current`（换当前）→ **409 `PERMISSION_GROUP_TOGGLE_OFF`**
   「本空间的权限挡位功能开关未打开…」；解除分配不拦。
5. 拨动后本进程立即生效；其它实例进程最多 60s 后跟上（缓存 TTL）。

## 2. 前端实现（本条线已做，`make web-build` 绿）

| 文件 | 改动 |
|---|---|
| `web/src/api/types.ts` | `SpaceToggleImpactOut / SpaceToggleStatusOut / SpaceToggleRequest` |
| `web/src/api/permissionGroups.ts` | `apiGetSpaceToggle / apiPutSpaceToggle`（并入档位 API 模块） |
| `web/src/views/permissionGroups/PermissionGroupsView.vue` | 页顶开关卡（本空间 Admin+ 可见）：状态+拨动人/时间+本空间影响面；打开/关闭二次确认（打开时写明拦截范围+无档人数+自挡提示）；pg-note 动态提示"开关未打开时分配/设为当前不可用" |
| 删除 | 实例级独立页、`/settings/permission-enforcement` 路由、侧栏入口、`api/permissionEnforcement.ts` |

## 3. 四段

### 已完成

- 契约重导 + 类型/API 层 + 档位页开关卡 + 旧实例级 UI 全部拆除，`make web-build` 绿；
- 409（配挡时机）/403（无资格）提示直接透出后端文案（errMessage），不误导。

### 未完成

- v2 的真机浏览器验收轮未跑（§11 那轮验的是废弃的 v1）：需要共享 dev 库升新版 0022 +
  :8010/:5174 起 v2 栈，等用户发起；
- 拨动历史列表 UI 未做（audit_logs 里有完整留痕，当前只在卡片上显示最后一次拨动人/时间）。

### 禁测

- 共享 dev 库的升/降级仍须先备份并经用户点头；
- :5173/:8000（demo-backend 栈）不动。

### 报告

- 门禁：`make web-build` 绿；后端 `server-lint` 绿、`server-test` **578 passed**、`server-openapi` 已重导；
- 提交：R1 `83061ae`（后端 v2）、R2 `359f739`（v2 集成测试）、R3 `9e998a5`（本片）。
