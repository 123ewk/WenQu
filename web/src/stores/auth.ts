/**
 * 认证与空间状态:
 * - 登录/注册/切换空间返回的 AuthResponse 必须整体替换本地 user/spaces/令牌;
 * - 令牌持久化在 localStorage(F5 恢复登录态),本 Store 不持有令牌,请求头由 http.ts 注入;
 * - refresh_token 是一次性的,切换空间/刷新都会轮换,因此令牌轮换操作串行执行,
 *   且内部函数不叠加串行(否则嵌套排队会死锁)。
 */
import { defineStore } from 'pinia'

import { apiGetMe, apiUpdateMe } from '@/api/users'
import { apiLogin, apiLogout, apiRegister, apiSwitchSpace } from '@/api/auth'
import * as session from '@/api/session'
import { apiCreateSpace, apiListSpaces } from '@/api/spaces'
import type { AuthResponse, SpaceBriefOut, UserOut } from '@/api/types'
import { isAtLeast, ROLE } from '@/utils/roles'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserOut | null>(session.getUser())
  const spaces = ref<SpaceBriefOut[]>(session.getSpaces())
  const currentSpaceId = ref<string | null>(session.getCurrentSpaceId())

  const currentSpace = computed(() => spaces.value.find((s) => s.id === currentSpaceId.value) ?? null)
  /** 当前用户在当前空间的角色(Viewer=10/Editor=20/Admin=30/Owner=40) */
  const role = computed(() => currentSpace.value?.role ?? null)
  const isAdmin = computed(() => isAtLeast(role.value, ROLE.ADMIN))
  const isOwner = computed(() => isAtLeast(role.value, ROLE.OWNER))
  const isLoggedIn = computed(() => !!user.value && session.isLoggedIn())

  /** AuthResponse 整体落地;默认保留当前空间,失效则回退到第一个空间 */
  function applyAuth(data: AuthResponse, opts?: { switchTo?: string }) {
    session.saveAuth(data)
    user.value = data.user
    spaces.value = data.spaces
    const kept =
      currentSpaceId.value && data.spaces.some((s) => s.id === currentSpaceId.value)
        ? currentSpaceId.value
        : null
    const target = opts?.switchTo ?? kept ?? data.spaces[0]?.id ?? null
    session.setCurrentSpaceId(target)
    currentSpaceId.value = target
  }

  /** 令牌轮换类操作串行执行,避免并发使用已作废的一次性 refresh_token */
  let queue: Promise<unknown> = Promise.resolve()
  function serialized<T>(fn: () => Promise<T>): Promise<T> {
    const run = queue.then(fn, fn)
    queue = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }

  async function switchUnlocked(spaceId: string) {
    const refreshToken = session.getRefreshToken()
    if (!refreshToken) throw new Error('登录已失效,请重新登录')
    applyAuth(await apiSwitchSpace(spaceId, refreshToken), { switchTo: spaceId })
  }

  /** 拉取最新空间列表并同步持久化;当前空间被删/被移出时自动切入下一个(不嵌套串行) */
  async function refreshSpacesUnlocked(): Promise<void> {
    const list = await apiListSpaces()
    spaces.value = list.map((s) => ({ id: s.id, name: s.name, role: s.role, description: s.description }))
    session.saveSpaces(spaces.value)
    if (currentSpaceId.value && !spaces.value.some((s) => s.id === currentSpaceId.value)) {
      const next = spaces.value[0]?.id ?? null
      currentSpaceId.value = next
      session.setCurrentSpaceId(next)
      if (next) await switchUnlocked(next)
    }
  }

  async function login(username: string, password: string) {
    applyAuth(await apiLogin({ username, password }))
  }

  async function register(username: string, nickname: string, password: string) {
    applyAuth(await apiRegister({ username, nickname, password }))
  }

  function switchSpace(spaceId: string): Promise<void> {
    return serialized(() => switchUnlocked(spaceId))
  }

  function refreshSpaces(): Promise<void> {
    return serialized(refreshSpacesUnlocked)
  }

  async function createSpace(name: string, description: string): Promise<void> {
    await serialized(async () => {
      const space = await apiCreateSpace({ name, description: description || undefined })
      await refreshSpacesUnlocked()
      await switchUnlocked(space.id) // 创建后切入新空间(轮换令牌)
    })
  }

  async function updateNickname(nickname: string): Promise<void> {
    const updated = await apiUpdateMe({ nickname })
    user.value = updated
    session.saveUser(updated)
  }

  async function logout(): Promise<void> {
    try {
      await apiLogout()
    } finally {
      // 后端 401/网络失败也不阻塞登出:本地凭据必须清空
      resetAuth()
    }
  }

  function resetAuth() {
    session.clearAuth()
    user.value = null
    spaces.value = []
    currentSpaceId.value = null
  }

  /** F5 后校验令牌并静默刷新用户与空间列表(失败交由 401 链处理) */
  async function bootstrap(): Promise<void> {
    if (!isLoggedIn) return
    try {
      const me = await apiGetMe()
      user.value = me
      session.saveUser(me)
      await refreshSpaces()
    } catch {
      /* 静默:令牌失效时拦截器已处理 */
    }
  }

  return {
    user,
    spaces,
    currentSpaceId,
    currentSpace,
    role,
    isAdmin,
    isOwner,
    isLoggedIn,
    login,
    register,
    switchSpace,
    createSpace,
    refreshSpaces,
    updateNickname,
    logout,
    resetAuth,
    bootstrap,
  }
})
