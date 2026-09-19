/**
 * 登录态在 localStorage 中的持久化(键名见 对接文档.md §8.1)。
 * 独立于 Pinia:http.ts 的刷新链不依赖任何 Store 即可覆盖令牌。
 * 活动空间不单独存键:随契约值保存在 user JSON 的 current_space_id 里(单一事实来源)。
 */
import type { AuthResponse, SpaceBriefOut, UserOut } from './types'

const K_ACCESS = 'wenqu_access_token'
const K_REFRESH = 'wenqu_refresh_token'
const K_USER = 'wenqu_user'
const K_SPACES = 'wenqu_spaces'
const K_SIDEBAR = 'wenqu_sidebar_collapsed'

// 历史遗留:活动空间曾用独立键自管理,契约提供 current_space_id 后已废弃;老用户浏览器里有残留,一次性清掉
localStorage.removeItem('wenqu_current_space_id')

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

/** 活动空间以契约为准:读已落盘 user 的 current_space_id(login/register 未选空间时为 null) */
export function getCurrentSpaceId(): string | null {
  return getUser()?.current_space_id ?? null
}

export function isLoggedIn(): boolean {
  return !!getAccessToken() && !!getUser()
}

/** AuthResponse 整体落盘(登录/注册/刷新/切空间共用);活动空间随 user.current_space_id 持久化 */
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

export function clearAuth() {
  for (const key of [K_ACCESS, K_REFRESH, K_USER, K_SPACES]) {
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
