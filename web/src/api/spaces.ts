import { http } from './http'
import type {
  AddMemberRequest,
  AuditFilter,
  AuditPage,
  ChangeRoleRequest,
  CreateSpaceRequest,
  MemberOut,
  SpaceOut,
  UpdateSpaceRequest,
} from './types'

export function apiListSpaces(): Promise<SpaceOut[]> {
  return http.get<SpaceOut[]>('/spaces').then((r) => r.data)
}

export function apiCreateSpace(body: CreateSpaceRequest): Promise<SpaceOut> {
  return http.post<SpaceOut>('/spaces', body).then((r) => r.data)
}

/** 非成员收到 404(SPACE_NOT_FOUND),前端按"空间不存在或无权访问"处理 */
export function apiGetSpace(spaceId: string): Promise<SpaceOut> {
  return http.get<SpaceOut>(`/spaces/${spaceId}`).then((r) => r.data)
}

export function apiUpdateSpace(spaceId: string, body: UpdateSpaceRequest): Promise<SpaceOut> {
  return http.patch<SpaceOut>(`/spaces/${spaceId}`, body).then((r) => r.data)
}

export function apiDeleteSpace(spaceId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}`).then((r) => r.data)
}

export function apiListMembers(spaceId: string): Promise<MemberOut[]> {
  return http.get<MemberOut[]>(`/spaces/${spaceId}/members`).then((r) => r.data)
}

export function apiAddMember(spaceId: string, body: AddMemberRequest): Promise<MemberOut> {
  return http.post<MemberOut>(`/spaces/${spaceId}/members`, body).then((r) => r.data)
}

/** role=40 表示转让所有权,转让后自己降为 Admin */
export function apiUpdateMemberRole(spaceId: string, userId: string, body: ChangeRoleRequest): Promise<MemberOut> {
  return http.patch<MemberOut>(`/spaces/${spaceId}/members/${userId}`, body).then((r) => r.data)
}

/**
 * OPT-41:移出成员时要决定他的权限档位挂载行怎么处理。
 * keepMounts=false(默认)= 一并清除,重新拉回来要重配档位;
 * true = 保留挂载行,重新入空间即恢复上次权限 —— 会带回旧权限,慎用。两种选择都进审计。
 */
export function apiRemoveMember(spaceId: string, userId: string, keepMounts = false): Promise<void> {
  return http
    .delete<void>(`/spaces/${spaceId}/members/${userId}`, { params: { keep_mounts: keepMounts } })
    .then((r) => r.data)
}

export function apiLeaveSpace(spaceId: string): Promise<void> {
  return http.post<void>(`/spaces/${spaceId}/leave`).then((r) => r.data)
}

/** total 是筛选后的总数;action 非法值后端返回 422(不是静默空列表) */
export function apiListAuditLogs(
  spaceId: string,
  limit: number,
  offset: number,
  filter?: AuditFilter,
): Promise<AuditPage> {
  return http
    .get<AuditPage>(`/spaces/${spaceId}/audit-logs`, {
      params: { limit, offset, ...cleanParams(filter) },
    })
    .then((r) => r.data)
}

function cleanParams(filter?: AuditFilter): Record<string, unknown> {
  if (!filter) return {}
  return Object.fromEntries(Object.entries(filter).filter(([, v]) => v !== undefined && v !== null && v !== ''))
}
