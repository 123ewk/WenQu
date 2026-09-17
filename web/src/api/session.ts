/**
 * 登录态在 localStorage 中的持久化(键名见 对接说明.md)。
 * 独立于 Pinia:http.ts 的刷新链不依赖任何 Store 即可覆盖令牌。
 */
import type { AuthResponse, SpaceBriefOut, UserOut } from './types'

const K_ACCESS = 'wenqu_access_token'
const K_REFRESH = 'wenqu_refresh_token'
const K_USER = 'wenqu_user'
const K_SPACES = 'wenqu_spaces'
const K_SPACE_ID = 'wenqu_current_space_id'
const K_SIDEBAR = 'wenqu_sidebar_collapsed'

function readJson<T>(key: string): T | null {
  const raw = localStorage.getItem(key)
  if (!raw) return null
  try {
    return JSON.parse(raw) as T
  } catch {
    localStorage.removeItem(key)
    return null
  }
}

function writeJson(key: string, value: unknown) {
  localStorage.setItem(key, JSON.stringify(value))
}

export function getAccessToken(): string | null {
  return localStorage.getItem(K_ACCESS)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(K_REFRESH)
}

export function getUser(): UserOut | null {
  return readJson<UserOut>(K_USER)
}

export function getSpaces(): SpaceBriefOut[] {
  return readJson<SpaceBriefOut[]>(K_SPACES) ?? []
}

export function getCurrentSpaceId(): string | null {
  return localStorage.getItem(K_SPACE_ID)
}

export function isLoggedIn(): boolean {
  return !!getAccessToken() && !!getUser()
}

/** AuthResponse 整体落盘(登录/注册/刷新/切空间共用);当前空间 ID 由调用方单独维护 */
export function saveAuth(data: AuthResponse) {
  localStorage.setItem(K_ACCESS, data.access_token)
  localStorage.setItem(K_REFRESH, data.refresh_token)
  writeJson(K_USER, data.user)
  writeJson(K_SPACES, data.spaces)
}

export function saveUser(user: UserOut) {
  writeJson(K_USER, user)
}

export function saveSpaces(spaces: SpaceBriefOut[]) {
  writeJson(K_SPACES, spaces)
}

export function setCurrentSpaceId(id: string | null) {
  if (id) localStorage.setItem(K_SPACE_ID, id)
  else localStorage.removeItem(K_SPACE_ID)
}

export function clearAuth() {
  for (const key of [K_ACCESS, K_REFRESH, K_USER, K_SPACES, K_SPACE_ID]) {
    localStorage.removeItem(key)
  }
}

export function getSidebarCollapsed(): boolean {
  return localStorage.getItem(K_SIDEBAR) === '1'
}

export function setSidebarCollapsed(v: boolean) {
  if (v) localStorage.setItem(K_SIDEBAR, '1')
  else localStorage.removeItem(K_SIDEBAR)
}
