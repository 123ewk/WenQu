import { http } from './http'
import type { ApiKeyCreatedOut, ApiKeyOut, CreateApiKeyRequest } from './types'

/** 列表含**已吊销**项(Viewer+ 可读);`revoked_at` 非 null 即已吊销,前端置灰展示 */
export function apiListApiKeys(spaceId: string): Promise<ApiKeyOut[]> {
  return http.get<ApiKeyOut[]>(`/spaces/${spaceId}/api-keys`).then((r) => r.data)
}

/**
 * 创建需 Admin+(OPT-26:门槛从 Editor+ 提到 Admin+,因 Key 现在绑定成员身份)。
 * body 必带 `member_user_id`(该空间现存成员)。201 响应里的 `plaintext` 是**完整明文的唯一一次展示**:
 * 服务端只存哈希与 `key_hint`,关掉弹窗后无法找回,不要试图再查列表取回。
 */
export function apiCreateApiKey(spaceId: string, body: CreateApiKeyRequest): Promise<ApiKeyCreatedOut> {
  return http.post<ApiKeyCreatedOut>(`/spaces/${spaceId}/api-keys`, body).then((r) => r.data)
}

/** 吊销需 Admin+(OPT-26,与创建同门槛);软删除且**幂等**(重复吊销仍 204,不报错) */
export function apiRevokeApiKey(spaceId: string, keyId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/api-keys/${keyId}`).then((r) => r.data)
}

/**
 * 设为「正在使用」(OPT-31):需 Admin+,同成员原生效 Key 自动置 false(互斥),重复调用幂等。
 * 纯管理标记 —— 未标"正在使用"的未吊销 Key 仍可正常调用(成员可换着用),后端不拦。
 * 已吊销/成员已注销的 Key → 400 VALIDATION_ERROR;200 返回更新后的 Key(同列表结构)。
 */
export function apiActivateApiKey(spaceId: string, keyId: string): Promise<ApiKeyOut> {
  return http.put<ApiKeyOut>(`/spaces/${spaceId}/api-keys/${keyId}/activate`).then((r) => r.data)
}
