import { http } from './http'
import type { ChangePasswordRequest, UpdateProfileRequest, UserOut } from './types'

export function apiGetMe(): Promise<UserOut> {
  return http.get<UserOut>('/users/me').then((r) => r.data)
}

export function apiUpdateMe(body: UpdateProfileRequest): Promise<UserOut> {
  return http.patch<UserOut>('/users/me', body).then((r) => r.data)
}

/** 204;成功后后端吊销该用户全部会话,前端必须清凭据并跳登录页 */
export function apiChangePassword(body: ChangePasswordRequest): Promise<void> {
  return http.post<void>('/users/me/password', body).then((r) => r.data)
}
