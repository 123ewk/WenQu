/**
 * OPT-28 Token 用量(对接说明 §2 v3.0,已按 openapi.json 52/74 核对)。两端点挂空间、只认 JWT:
 * 汇总表 = Admin+(Viewer/Editor 是 403 不是 404,入口按角色隐藏也要接住 403);
 * /me = 任意成员,响应是嵌套 {usage},没有 members 字段。
 */
import { http } from './http'
import type { MyUsageOut, UsagePeriod, UsageSummaryOut } from './types'

export function apiGetTokenUsage(spaceId: string, period: UsagePeriod): Promise<UsageSummaryOut> {
  return http
    .get<UsageSummaryOut>(`/spaces/${spaceId}/token-usage`, { params: { period } })
    .then((r) => r.data)
}

export function apiGetMyTokenUsage(spaceId: string, period: UsagePeriod): Promise<MyUsageOut> {
  return http
    .get<MyUsageOut>(`/spaces/${spaceId}/token-usage/me`, { params: { period } })
    .then((r) => r.data)
}
