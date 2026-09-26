/**
 * OPT-48 实例级档位灰度开关(已按 openapi.json 核对)。
 * 权限口径:superuser 或任意空间 Admin+(role≥30)可读可拨;效果是**全局**的。
 * 403 = 没资格;409 = 有资格但开启者本人在某些空间缺生效档位(先给自己配好档);
 * PUT 成功后本进程立即生效,其它实例进程最多 60s 后跟上(缓存 TTL)。
 */
import { http } from './http'
import type { EnforcementStatusOut, EnforcementToggleRequest } from './types'

/** 读当前生效值、来源(db / env_fallback)与影响面统计 */
export function apiGetPermissionEnforcement(): Promise<EnforcementStatusOut> {
  return http.get<EnforcementStatusOut>('/admin/permission-enforcement').then((r) => r.data)
}

/** 拨动开关(全局生效);409 时 details.spaces 列出操作者缺生效档的空间 */
export function apiPutPermissionEnforcement(
  body: EnforcementToggleRequest,
): Promise<EnforcementStatusOut> {
  return http.put<EnforcementStatusOut>('/admin/permission-enforcement', body).then((r) => r.data)
}
