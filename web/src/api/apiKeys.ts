import { http } from './http'
import type { ApiKeyCreatedOut, ApiKeyOut, CreateApiKeyRequest } from './types'

/** 列表含**已吊销**项(Viewer+ 可读);`revoked_at` 非 null 即已吊销,前端置灰展示 */
export function apiListApiKeys(spaceId: string): Promise<ApiKeyOut[]> {
  return http.get<ApiKeyOut[]>(`/spaces/${spaceId}/api-keys`).then((r) => r.data)
}

/**
 * 创建需 Editor+。201 响应里的 `plaintext` 是**完整明文的唯一一次展示**:
 * 服务端只存哈希与 `key_hint`,关掉弹窗后无法找回,不要试图再查列表取回。
 */
export function apiCreateApiKey(spaceId: string, body: CreateApiKeyRequest): Promise<ApiKeyCreatedOut> {
  return http.post<ApiKeyCreatedOut>(`/spaces/${spaceId}/api-keys`, body).then((r) => r.data)
}

/** 吊销需 Editor+;软删除且**幂等**(重复吊销仍 204,不报错) */
export function apiRevokeApiKey(spaceId: string, keyId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/api-keys/${keyId}`).then((r) => r.data)
}
