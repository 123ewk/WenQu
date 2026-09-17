import { http } from './http'
import type { AuthResponse, LoginRequest, RegisterRequest } from './types'

export function apiRegister(body: RegisterRequest): Promise<AuthResponse> {
  return http.post<AuthResponse>('/auth/register', body).then((r) => r.data)
}

export function apiLogin(body: LoginRequest): Promise<AuthResponse> {
  return http.post<AuthResponse>('/auth/login', body).then((r) => r.data)
}

export function apiLogout(): Promise<void> {
  return http.post<void>('/auth/logout').then((r) => r.data)
}

/** 切换活动空间:返回全新令牌对 + 完整 user/spaces,调用方必须整体替换本地状态 */
export function apiSwitchSpace(spaceId: string, refreshToken: string): Promise<AuthResponse> {
  return http
    .post<AuthResponse>('/auth/switch-space', { space_id: spaceId, refresh_token: refreshToken })
    .then((r) => r.data)
}
