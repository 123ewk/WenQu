/**
 * OPT-27 空间模型配置(对接说明 §1)。后端分批合并中,路径/字段以文档为准,
 * openapi 重导后逐条核对。权限口径:读 = 空间成员(Viewer+)可见(页面只读),写 = Admin+。
 * Key 字段永远只回显掩码;测试失败统一 424 UPSTREAM_MODEL_ERROR(reason ∈ auth|network|model_name)。
 */
import { http, ApiError } from './http'
import type {
  EmbeddingConfigOut,
  ModelProviderCreateRequest,
  ModelProviderOut,
  ModelProviderUpdateRequest,
  ModelTestResultOut,
  ModelTypeOption,
  SpaceModelConfigOut,
  SpaceModelCreateRequest,
  SpaceModelOut,
  SpaceModelUpdateRequest,
} from './types'

export type ModelKind = 'chat' | 'rerank'

export function apiGetModelConfig(spaceId: string): Promise<SpaceModelConfigOut> {
  return http.get<SpaceModelConfigOut>(`/spaces/${spaceId}/model-config`).then((r) => r.data)
}

/** 供应商类型下拉(名字 + default_base_url 预填值);后端可维护,前端不写死清单 */
export function apiGetModelConfigTypes(spaceId: string): Promise<ModelTypeOption[]> {
  return http
    .get<ModelTypeOption[]>(`/spaces/${spaceId}/model-config/types`)
    .then((r) => r.data)
}

/** 保存 embedding Key;空串 = 清除本空间 Key(调用方须先二次确认) */
export function apiPutEmbeddingKey(spaceId: string, apiKey: string): Promise<EmbeddingConfigOut> {
  return http
    .put<EmbeddingConfigOut>(`/spaces/${spaceId}/model-config/embedding-key`, { api_key: apiKey })
    .then((r) => r.data)
}

/** 测 embedding Key:传空 = 测已存 Key */
export function apiTestEmbeddingKey(spaceId: string, apiKey?: string): Promise<ModelTestResultOut> {
  return http
    .post<ModelTestResultOut>(`/spaces/${spaceId}/model-config/embedding-key/test`, {
      api_key: apiKey || undefined,
    })
    .then((r) => r.data)
}

export function apiListProviders(spaceId: string, kind: ModelKind): Promise<ModelProviderOut[]> {
  return http
    .get<ModelProviderOut[]>(`/spaces/${spaceId}/model-config/providers`, { params: { kind } })
    .then((r) => r.data)
}

export function apiCreateProvider(
  spaceId: string,
  kind: ModelKind,
  body: ModelProviderCreateRequest,
): Promise<ModelProviderOut> {
  return http
    .post<ModelProviderOut>(`/spaces/${spaceId}/model-config/providers`, { kind, ...body })
    .then((r) => r.data)
}

export function apiUpdateProvider(
  spaceId: string,
  providerId: string,
  body: ModelProviderUpdateRequest,
): Promise<ModelProviderOut> {
  return http
    .patch<ModelProviderOut>(`/spaces/${spaceId}/model-config/providers/${providerId}`, body)
    .then((r) => r.data)
}

/** 删除级联删其下模型,调用方二次确认文案须提示受影响模型数 */
export function apiDeleteProvider(spaceId: string, providerId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/model-config/providers/${providerId}`).then(() => undefined)
}

/** 用已存配置发最小上游请求 */
export function apiTestProvider(spaceId: string, providerId: string): Promise<ModelTestResultOut> {
  return http
    .post<ModelTestResultOut>(`/spaces/${spaceId}/model-config/providers/${providerId}/test`)
    .then((r) => r.data)
}

export function apiListModels(spaceId: string, kind: ModelKind): Promise<SpaceModelOut[]> {
  return http
    .get<SpaceModelOut[]>(`/spaces/${spaceId}/model-config/models`, { params: { kind } })
    .then((r) => r.data)
}

export function apiCreateModel(
  spaceId: string,
  kind: ModelKind,
  body: SpaceModelCreateRequest,
): Promise<SpaceModelOut> {
  return http
    .post<SpaceModelOut>(`/spaces/${spaceId}/model-config/models`, { kind, ...body })
    .then((r) => r.data)
}

export function apiUpdateModel(
  spaceId: string,
  modelId: string,
  body: SpaceModelUpdateRequest,
): Promise<SpaceModelOut> {
  return http
    .patch<SpaceModelOut>(`/spaces/${spaceId}/model-config/models/${modelId}`, body)
    .then((r) => r.data)
}

export function apiDeleteModel(spaceId: string, modelId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/model-config/models/${modelId}`).then(() => undefined)
}

export function apiTestModel(spaceId: string, modelId: string): Promise<ModelTestResultOut> {
  return http
    .post<ModelTestResultOut>(`/spaces/${spaceId}/model-config/models/${modelId}/test`)
    .then((r) => r.data)
}

export function apiSetRerankSwitch(spaceId: string, enabled: boolean): Promise<SpaceModelConfigOut> {
  return http
    .put<SpaceModelConfigOut>(`/spaces/${spaceId}/model-config/rerank-switch`, { enabled })
    .then((r) => r.data)
}

/** §1.7 错误码展示口径:424 按 details.reason 细分;其余透传后端 message */
export function modelTestErrorHint(e: unknown): string {
  if (e instanceof ApiError && e.code === 'UPSTREAM_MODEL_ERROR') {
    const reason = (e.details as { reason?: string } | null)?.reason
    if (reason === 'auth') return 'Key 无效'
    if (reason === 'network') return '网络不通或 base_url 错'
    if (reason === 'model_name') return '模型名不存在'
  }
  if (e instanceof ApiError && e.code === 'FORBIDDEN') return '需要空间管理员权限'
  if (e instanceof ApiError && e.code === 'NOT_FOUND') return '资源不存在'
  if (e instanceof ApiError) return e.message
  return '未知错误'
}
