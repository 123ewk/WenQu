/**
 * OPT-27 空间模型配置(对接说明 §1 v3.0,已按 openapi.json 52/74 核对)。
 * 权限口径:读 = 空间成员可见(非 Admin 只读,读接口不 403);写与探活 = Admin+。
 * 读口只有聚合 GET /model-config(没有单独的 providers/models 列表接口);
 * PUT embedding-key / rerank-switch 与所有 test 探活端点只回 {ok},改完必须重拉聚合。
 */
import { http, ApiError } from './http'
import type {
  EmbeddingKeyRequest,
  EmbeddingKeyTestRequest,
  OkOut,
  ProviderTypeOut,
  RerankSwitchRequest,
  SpaceModelConfigOut,
  SpaceModelCreateRequest,
  SpaceModelOut,
  SpaceModelUpdateRequest,
  SpaceProviderCreateRequest,
  SpaceProviderOut,
  SpaceProviderUpdateRequest,
} from './types'

export type ModelKind = 'chat' | 'rerank'

/** 进页面一次拿全四区块;每次写后要重新调它拿掩码/默认态 */
export function apiGetModelConfig(spaceId: string): Promise<SpaceModelConfigOut> {
  return http.get<SpaceModelConfigOut>(`/spaces/${spaceId}/model-config`).then((r) => r.data)
}

/** 供应商类型下拉(key/label/default_base_url/protocol/kinds);下拉按 kinds 分开渲染 */
export function apiGetProviderTypes(spaceId: string): Promise<ProviderTypeOut[]> {
  return http.get<ProviderTypeOut[]>(`/spaces/${spaceId}/model-config/types`).then((r) => r.data)
}

/** 保存 embedding Key;空串 = 清除本空间 Key(调用方须先二次确认);只回 ok,重拉聚合 */
export function apiPutEmbeddingKey(spaceId: string, body: EmbeddingKeyRequest): Promise<OkOut> {
  return http
    .put<OkOut>(`/spaces/${spaceId}/model-config/embedding-key`, body)
    .then((r) => r.data)
}

/** 测 embedding Key;不传 api_key = 测已存 Key;一把 Key 都没有时后端给 503 而非 424 */
export function apiTestEmbeddingKey(
  spaceId: string,
  body: EmbeddingKeyTestRequest = {},
): Promise<OkOut> {
  return http
    .post<OkOut>(`/spaces/${spaceId}/model-config/embedding-key/test`, body)
    .then((r) => r.data)
}

/** 只回 ok;开关与"是否已配 rerank 模型"解耦,拨完重拉聚合 */
export function apiPutRerankSwitch(spaceId: string, body: RerankSwitchRequest): Promise<OkOut> {
  return http
    .put<OkOut>(`/spaces/${spaceId}/model-config/rerank-switch`, body)
    .then((r) => r.data)
}

/** → 201,回供应商行(含 key_hint 掩码);base_url/api_key 留空传空串即可(有默认值) */
export function apiCreateProvider(
  spaceId: string,
  body: SpaceProviderCreateRequest,
): Promise<SpaceProviderOut> {
  return http
    .post<SpaceProviderOut>(`/spaces/${spaceId}/model-config/providers`, body)
    .then((r) => r.data)
}

/** api_key 语义:字段不传 = 不动,空串 = 清除,非空 = 更换;协议不可改 */
export function apiUpdateProvider(
  spaceId: string,
  providerId: string,
  body: SpaceProviderUpdateRequest,
): Promise<SpaceProviderOut> {
  return http
    .patch<SpaceProviderOut>(`/spaces/${spaceId}/model-config/providers/${providerId}`, body)
    .then((r) => r.data)
}

/** 204,级联删其下模型;调用方二次确认文案须提示受影响模型数(model_count) */
export function apiDeleteProvider(spaceId: string, providerId: string): Promise<void> {
  return http
    .delete<void>(`/spaces/${spaceId}/model-config/providers/${providerId}`)
    .then(() => undefined)
}

/** 取该供应商下最早的一个模型探活;model_count=0 时后端 422,按钮应提前禁用 */
export function apiTestProvider(spaceId: string, providerId: string): Promise<OkOut> {
  return http
    .post<OkOut>(`/spaces/${spaceId}/model-config/providers/${providerId}/test`)
    .then((r) => r.data)
}

/** → 201,回模型行;kind 必须与供应商 protocol 匹配;model_id 不传 = 后端合成 */
export function apiCreateModel(
  spaceId: string,
  body: SpaceModelCreateRequest,
): Promise<SpaceModelOut> {
  return http
    .post<SpaceModelOut>(`/spaces/${spaceId}/model-config/models`, body)
    .then((r) => r.data)
}

/** 路径参数是表主键 id(不是 model_id);设默认会顶掉同 kind 旧默认,回包后应刷新列表 */
export function apiUpdateModel(
  spaceId: string,
  modelPk: string,
  body: SpaceModelUpdateRequest,
): Promise<SpaceModelOut> {
  return http
    .patch<SpaceModelOut>(`/spaces/${spaceId}/model-config/models/${modelPk}`, body)
    .then((r) => r.data)
}

export function apiDeleteModel(spaceId: string, modelPk: string): Promise<void> {
  return http
    .delete<void>(`/spaces/${spaceId}/model-config/models/${modelPk}`)
    .then(() => undefined)
}

/** chat 发一条 ping 取首个增量;rerank 试排两条短文档 */
export function apiTestModel(spaceId: string, modelPk: string): Promise<OkOut> {
  return http
    .post<OkOut>(`/spaces/${spaceId}/model-config/models/${modelPk}/test`)
    .then((r) => r.data)
}

/**
 * §1.7 错误码展示口径。422(含 SSRF"模型服务地址不安全:"/ASCII 字符口径)直接透传
 * 后端 message 按场景展示;409 名称冲突在表单内联提示;424 按 details.reason 三分。
 */
export function modelConfigErrorHint(e: unknown): string {
  if (e instanceof ApiError) {
    const reason = (e.details as { reason?: string } | null)?.reason
    if (e.code === 'UPSTREAM_MODEL_ERROR') {
      if (reason === 'auth') return 'Key 无效'
      if (reason === 'model_name') return '模型名不存在'
      if (reason === 'network') return e.message
    }
    if (e.code === 'MODEL_PROVIDER_NAME_TAKEN' || e.code === 'MODEL_ID_TAKEN')
      return '该名称已存在(仅本空间层内判重)'
    if (e.code === 'FORBIDDEN') return '需要空间管理员权限'
    if (e.code === 'SPACE_NOT_FOUND') return '资源不存在'
    return e.message || '操作失败'
  }
  return '未知错误'
}

/** 424 之外的"该按钮能不能点"预判:供应商还没有模型时测试必 422 */
export function providerTestBlockedHint(provider: SpaceProviderOut): string {
  return provider.model_count === 0
    ? '该供应商下还没有模型,请先添加模型再测试连接'
    : ''
}

/** api_key/base_url 字符口径:只收不含空白的可见 ASCII(首尾空白后端会去掉);
 * name/model_name 允许中文,不要用这条规则 */
export const ASCII_NO_SPACE_RE = /^[\x21-\x7e]*$/
