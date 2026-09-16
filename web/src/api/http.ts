/**
 * HTTP 客户端与 401 处理链(实现细则见 docs/前端实现提示词.md 第七节):
 * 1. 请求拦截器统一附带 Bearer access_token;
 * 2. 收到 401(AUTH_REQUIRED/TOKEN_EXPIRED)时用 refresh_token 换新令牌对并重放原请求(仅一次);
 * 3. 并发 401 通过共享的 refreshing Promise 防抖,只发起一次刷新;
 * 4. refresh_token 是一次性的,刷新成功后新值立即覆盖旧值;
 * 5. 刷新失败(REFRESH_TOKEN_INVALID)→ 清空凭据并通知外层跳转登录页;网络错误不清凭据。
 */
import axios, { AxiosError } from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import * as session from './session'
import type { AuthResponse } from './types'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details: unknown

  constructor(code: string, message: string, status = 0, details: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }
}

const baseURL = import.meta.env.VITE_API_BASE_URL || '/api/v1'

export const http = axios.create({ baseURL, timeout: 20000 })

http.interceptors.request.use((config) => {
  const token = session.getAccessToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

/** 这些端点自身的 401(如登录密码错误)不代表会话过期,不走刷新链 */
const AUTH_PATHS = ['/auth/login', '/auth/register', '/auth/refresh', '/auth/switch-space', '/auth/logout']

/** 会话彻底失效时的回调,由 main.ts 注册(清空 Store 并跳登录页) */
let authExpiredHandler: (() => void) | null = null

export function setAuthExpiredHandler(fn: () => void) {
  authExpiredHandler = fn
}

interface RetriableConfig extends InternalAxiosRequestConfig {
  _retry?: boolean
}

let refreshing: Promise<void> | null = null

function isHardExpired(e: unknown): boolean {
  return e instanceof ApiError && e.code === 'REFRESH_TOKEN_INVALID'
}

async function rotateTokens(): Promise<void> {
  const refreshToken = session.getRefreshToken()
  if (!refreshToken) {
    throw new ApiError('REFRESH_TOKEN_INVALID', '登录已失效,请重新登录', 401)
  }
  try {
    // 用裸 axios 调用,避免被本实例的拦截器二次处理
    const { data } = await axios.post<AuthResponse>(`${baseURL}/auth/refresh`, {
      refresh_token: refreshToken,
    })
    session.saveAuth(data) // 一次性 refresh_token:新值立即覆盖旧值
  } catch (e) {
    if (axios.isAxiosError(e) && e.response?.status === 401) {
      throw new ApiError('REFRESH_TOKEN_INVALID', '登录已失效,请重新登录', 401)
    }
    // 网络抖动/后端重启等瞬时错误:不清凭据、不踢登录页
    throw new ApiError('NETWORK_ERROR', '网络异常,请稍后重试', 0)
  }
}

http.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    const config = error.config as RetriableConfig | undefined
    const status = error.response?.status
    const data = error.response?.data as { error?: { code?: string } } | undefined
    const code = data?.error?.code
    const path = config?.url ?? ''

    const refreshable =
      status === 401 &&
      !!config &&
      !config._retry &&
      !AUTH_PATHS.some((p) => path.includes(p)) &&
      (code === 'AUTH_REQUIRED' || code === 'TOKEN_EXPIRED' || !code)

    if (refreshable) {
      config._retry = true
      try {
        refreshing ??= rotateTokens().finally(() => {
          refreshing = null
        })
        await refreshing
        return http(config) // 用新令牌重放原请求(最多重试一次)
      } catch (refreshError) {
        if (isHardExpired(refreshError)) {
          session.clearAuth()
          authExpiredHandler?.()
          return Promise.reject(refreshError)
        }
        // 瞬时错误:透传原始请求错误,不打断登录态
        return Promise.reject(normalizeError(error))
      }
    }
    return Promise.reject(normalizeError(error))
  },
)

function normalizeError(error: AxiosError): ApiError {
  const status = error.response?.status ?? 0
  const data = error.response?.data as Record<string, unknown> | undefined
  const shell = data?.error as { code?: unknown; message?: unknown; details?: unknown } | undefined
  if (shell && typeof shell.code === 'string') {
    return new ApiError(
      shell.code,
      typeof shell.message === 'string' && shell.message ? shell.message : '请求失败',
      status,
      shell.details ?? null,
    )
  }
  // FastAPI 原生 422 形状(后端统一壳之外的兜底)
  if (Array.isArray(data?.detail)) {
    return new ApiError('VALIDATION_ERROR', '请求参数不合法', status, data.detail)
  }
  if (status === 0) {
    return new ApiError('NETWORK_ERROR', '网络异常,请确认后端服务已启动(make server-run)', 0)
  }
  return new ApiError('INTERNAL_ERROR', `请求失败(HTTP ${status})`, status, data ?? null)
}

/** 422 details(字段级错误数组)→ { 字段名: 提示 } */
export function fieldErrors(details: unknown): Record<string, string> {
  if (!Array.isArray(details)) return {}
  const out: Record<string, string> = {}
  for (const item of details) {
    if (item && typeof item === 'object' && 'loc' in item && 'msg' in item) {
      const loc = (item as { loc: unknown[] }).loc
      const key = String(loc[loc.length - 1])
      const msg = String((item as { msg: unknown }).msg)
      if (!(key in out)) out[key] = msg
    }
  }
  return out
}

/** 统一取错误提示文案(视图层直接喂给 ElMessage) */
export function errMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return '未知错误'
}
