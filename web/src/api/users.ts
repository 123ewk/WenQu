import { http } from './http'
import type { ChangePasswordRequest, UpdateProfileRequest, UserOut } from './types'

export function apiGetMe(): Promise<UserOut> {
  return http.get<UserOut>('/users/me').then((r) => r.data)
}

export function apiUpdateMe(body: UpdateProfileRequest): Promise<UserOut> {
  return http.patch<UserOut>('/users/me', body).then((r) => r.data)
}

/** 204;成功后后端吊销该用户全部会话,前端必须清凭据并跳登录页 */
export function apiChangePassword(body: ChangePasswordRequest): Promise<void> {
  return http.post<void>('/users/me/password', body).then((r) => r.data)
}

/**
 * 头像三端点(仅自己):
 * - 读取返回图片字节,需要 Authorization → 前端取 blob 渲染,不能直接写进 <img src>;
 * - 上传 multipart 字段名 file,让 axios 自行设置 Content-Type;返回更新后的 UserOut;
 * - 删除 204。
 * 约束(运行时配置,契约表达不了):PNG/JPEG/WEBP、默认 2MB、单边 ≤ 4096px —— 前端自校验。
 */
export function apiUploadAvatar(file: File): Promise<UserOut> {
  const form = new FormData()
  form.append('file', file)
  return http.post<UserOut>('/users/me/avatar', form).then((r) => r.data)
}

export async function apiGetAvatar(): Promise<Blob> {
  const r = await http.get('/users/me/avatar', { responseType: 'blob' })
  return r.data as Blob
}

export function apiDeleteAvatar(): Promise<void> {
  return http.delete<void>('/users/me/avatar').then((r) => r.data)
}
