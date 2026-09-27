/**
 * OPT-49 v2 空间级 Langfuse:配置管理(空间 Admin+)+ trace 产品内查看。
 * 查看口径:成员强制只看自己(后端覆盖 user_id 参数),Admin+ 可看全空间或指定成员。
 * secret_key 三态:null = 不改动 / "" = 清除 / 非空 = 替换;响应只回 key_hint。
 */
import { http } from './http'
import type {
  LangfuseConfigOut,
  LangfuseConfigPut,
  LangfuseTraceDetailOut,
  LangfuseTraceListOut,
} from './types'

export function apiGetSpaceLangfuseConfig(spaceId: string): Promise<LangfuseConfigOut> {
  return http.get<LangfuseConfigOut>(`/spaces/${spaceId}/langfuse-config`).then((r) => r.data)
}

export function apiPutSpaceLangfuseConfig(spaceId: string, body: LangfuseConfigPut): Promise<LangfuseConfigOut> {
  return http.put<LangfuseConfigOut>(`/spaces/${spaceId}/langfuse-config`, body).then((r) => r.data)
}

export function apiListSpaceLangfuseTraces(
  spaceId: string,
  params?: { user_id?: string; limit?: number },
): Promise<LangfuseTraceListOut> {
  return http.get<LangfuseTraceListOut>(`/spaces/${spaceId}/langfuse-traces`, { params }).then((r) => r.data)
}

export function apiGetSpaceLangfuseTrace(spaceId: string, traceId: string): Promise<LangfuseTraceDetailOut> {
  return http.get<LangfuseTraceDetailOut>(`/spaces/${spaceId}/langfuse-traces/${traceId}`).then((r) => r.data)
}
