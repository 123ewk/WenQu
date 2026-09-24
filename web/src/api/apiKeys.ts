import { http } from './http'
import type { ApiKeyCreatedOut, ApiKeyOut, CreateApiKeyRequest } from './types'

/** 列表含**已吊销**项(Viewer+ 可读);`revoked_at` 非 null 即已吊销,前端置灰展示 */
export function apiListApiKeys(spaceId: string): Promise<ApiKeyOut[]> {
  return http.get<ApiKeyOut[]>(`/spaces/${spaceId}/api-keys`).then((r) => r.data)
}

/**
 * 创建需 Admin+(与"拉成员"同闸)。OPT-41:Key 只是某个**权限档位**的程序化凭据 ——
 * body 带 `group_id`(权限来自档位)+ `owner_user_id`(以谁的身份行事、用量归谁),
 * 能力和知识库范围**不再出现在这里**。
 * 201 响应里的 `plaintext` 是完整明文的唯一一次展示:服务端只存哈希与 `key_hint`,
 * 关掉弹窗后无法找回,不要试图再从列表取回。
 */
export function apiCreateApiKey(
  spaceId: string,
  body: CreateApiKeyRequest,
): Promise<ApiKeyCreatedOut> {
  return http.post<ApiKeyCreatedOut>(`/spaces/${spaceId}/api-keys`, body).then((r) => r.data)
}

/**
 * 吊销需 Admin+(与创建同门槛);软删除且**幂等**(重复吊销仍 204,不报错)。
 * ⚠️ 吊销会同时打断"以这把 Key 调用的所有程序",而明文不可回显 —— 所以"想改权限"应当
 * 去档位页改档位(OPT-41 N8:改一次即时作用于其下全部 Key),不要吊销重签。
 */
export function apiRevokeApiKey(spaceId: string, keyId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/api-keys/${keyId}`).then((r) => r.data)
}
