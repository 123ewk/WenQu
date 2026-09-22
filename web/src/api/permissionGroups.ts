/**
 * 权限档位(OPT-41)—— 一档 = "能做什么能力 + 能看哪些知识库"的一份配置。
 *
 * 前端必须知道的三条口径:
 * 1. **权限的唯一真相在档位**:API Key 只是档位的程序化凭据,人带 JWT 也吃自己
 *    "当前档"的范围与能力。所以"改权限"永远在档位页做,改一次即时作用于其下已签发的 Key,
 *    不需要重发明文;
 * 2. **fail-closed**:`capabilities: []` = 什么都不能做;`all_kbs=false` 且 `kb_ids=[]`
 *    = 一个库都不给。"放开全部库"必须是显式勾选 `all_kbs=true`,不能靠"什么都不选"表达;
 * 3. **只认 JWT**:这组端点刻意不对 API Key 开放(程序凭据不能改自己的权限),
 *    写入门槛 Admin+,读入门槛 Viewer+。
 */
import { http } from './http'
import type { PermissionGroupOut, UpsertPermissionGroupRequest } from './types'

export function apiListPermissionGroups(spaceId: string): Promise<PermissionGroupOut[]> {
  return http.get<PermissionGroupOut[]>(`/spaces/${spaceId}/permission-groups`).then((r) => r.data)
}

export function apiCreatePermissionGroup(
  spaceId: string,
  body: UpsertPermissionGroupRequest,
): Promise<PermissionGroupOut> {
  return http
    .post<PermissionGroupOut>(`/spaces/${spaceId}/permission-groups`, body)
    .then((r) => r.data)
}

/** 整组替换语义(PUT 走 PATCH 方法名):半字段补丁会让"能力/范围"两套口径漂移 */
export function apiUpdatePermissionGroup(
  spaceId: string,
  groupId: string,
  body: UpsertPermissionGroupRequest,
): Promise<PermissionGroupOut> {
  return http
    .patch<PermissionGroupOut>(`/spaces/${spaceId}/permission-groups/${groupId}`, body)
    .then((r) => r.data)
}

/**
 * 删除只允许删空档:还挂着凭据或成员 → 400 VALIDATION_ERROR,
 * details 带 `key_count` / `member_count`,文案由服务端给(前端直接展示即可)。
 */
export function apiDeletePermissionGroup(spaceId: string, groupId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/permission-groups/${groupId}`).then((r) => r.data)
}

/** 分配 = 让他"可以用这一档",不自动生效(生效要显式 set-current);重复分配幂等 */
export function apiAssignGroupMember(
  spaceId: string,
  groupId: string,
  userId: string,
): Promise<PermissionGroupOut> {
  return http
    .post<PermissionGroupOut>(`/spaces/${spaceId}/permission-groups/${groupId}/members`, {
      user_id: userId,
    })
    .then((r) => r.data)
}

/** 解除分配:若他正生效这一档,current 指针一并消失(fail-closed,不做"自动换一档") */
export function apiUnassignGroupMember(
  spaceId: string,
  groupId: string,
  userId: string,
): Promise<void> {
  return http
    .delete<void>(`/spaces/${spaceId}/permission-groups/${groupId}/members/${userId}`)
    .then((r) => r.data)
}

/**
 * 设为当前档(取代 OPT-31 的"激活某把 Key"):同一人在同一空间原 current 自动让位。
 * 未先分配 → 400 VALIDATION_ERROR("该成员尚未挂在这个档位上,请先分配")。
 */
export function apiSetCurrentGroup(
  spaceId: string,
  groupId: string,
  userId: string,
): Promise<PermissionGroupOut> {
  return http
    .put<PermissionGroupOut>(`/spaces/${spaceId}/permission-groups/${groupId}/current`, {
      user_id: userId,
    })
    .then((r) => r.data)
}

export type { GroupMemberOut, PermissionGroupOut, UpsertPermissionGroupRequest }
