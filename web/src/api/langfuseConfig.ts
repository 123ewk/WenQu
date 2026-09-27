/**
 * OPT-49 Langfuse 可观测配置(全局 superuser 专属;按"模型配置"标准管理)。
 * secret_key 三态:null = 不改动 / "" = 清除 / 非空 = 替换;响应只回 key_hint。
 */
import { http } from './http'
import type { LangfuseConfigOut, LangfuseConfigPut } from './types'

export function apiGetLangfuseConfig(): Promise<LangfuseConfigOut> {
  return http.get<LangfuseConfigOut>('/admin/langfuse-config').then((r) => r.data)
}

export function apiPutLangfuseConfig(body: LangfuseConfigPut): Promise<LangfuseConfigOut> {
  return http.put<LangfuseConfigOut>('/admin/langfuse-config', body).then((r) => r.data)
}
