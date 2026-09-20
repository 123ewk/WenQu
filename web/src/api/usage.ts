/**
 * OPT-28 Token 用量(对接说明 §2)。两端点挂空间:
 * 汇总表 = Admin+(非 Admin 403,前端仅展示"我的用量");/me = 任意成员。
 * 后端分批合并中,`period` 的"全部"取值字面量待契约重导核对。
 */
import { http } from './http'
import type { MemberTokenUsage, TokenUsageOut, UsagePeriod } from './types'

export function apiGetTokenUsage(spaceId: string, period: UsagePeriod): Promise<TokenUsageOut> {
  return http
    .get<TokenUsageOut>(`/spaces/${spaceId}/token-usage`, { params: { period } })
    .then((r) => r.data)
}

/** 单人对象,字段与汇总行同款;普通成员只有这个入口 */
export function apiGetMyTokenUsage(spaceId: string, period: UsagePeriod): Promise<MemberTokenUsage> {
  return http
    .get<MemberTokenUsage>(`/spaces/${spaceId}/token-usage/me`, { params: { period } })
    .then((r) => r.data)
}
