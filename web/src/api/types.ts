/**
 * 与 docs/api/openapi.json 保持一致的手写契约类型。
 * 修改接口后,后端会重新导出契约,此处字段必须随之同步。
 */

export interface UserOut {
  id: string
  username: string
  nickname: string
  created_at: string | null
}

/** 登录/注册/刷新响应中 spaces[] 的元素,role 为当前用户在该空间的角色 */
export interface SpaceBriefOut {
  id: string
  name: string
  role: number
  description?: string
}

export interface SpaceOut {
  id: string
  name: string
  description: string
  role: number
  created_at: string | null
}

export interface MemberOut {
  user_id: string
  username: string
  nickname: string
  role: number
  joined_at: string | null
}

export interface AuthResponse {
  user: UserOut
  spaces: SpaceBriefOut[]
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface AuditLogOut {
  id: string
  actor_id: string | null
  action: string
  target: string
  detail: Record<string, unknown>
  ip: string
  created_at: string | null
}

export interface AuditPage {
  items: AuditLogOut[]
  total: number
}

/* ───── 请求体 ───── */

export interface RegisterRequest {
  username: string
  nickname: string
  password: string
}

export interface LoginRequest {
  username: string
  password: string
}

export interface CreateSpaceRequest {
  name: string
  description?: string
}

export interface UpdateSpaceRequest {
  name?: string
  description?: string
}

export interface AddMemberRequest {
  username: string
  role: number
}

export interface ChangeRoleRequest {
  role: number
}

export interface UpdateProfileRequest {
  nickname: string
}

export interface ChangePasswordRequest {
  old_password: string
  new_password: string
}
