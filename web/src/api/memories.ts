/**
 * 外部事实记忆 + 历史滚动压缩(OPT-45)。
 *
 * 前端必须知道的三条口径(详见 docs/api/前端接入工单-OPT45记忆卡片与上下文压缩.md):
 * 1. **记忆归属 = (空间, 用户)**:全部走 JWT,只看得见本人本空间的记忆;他空间/他人一律 404 防枚举;
 * 2. **AI 提议 ≠ 已存记忆**:`memory_proposal` 帧只是草稿,用户点"确认"才落库(下面的 apiSaveMemory);
 *    点"忽略"写一条带 TTL 的去重留痕,同结论短期内不再弹卡;
 * 3. **压缩是叠加摘要,原文不删**:`apiCompress` 返回 `compressed=false` 是 HTTP 200 非错误,按 `reason` 分诊。
 */
import { http } from './http'
import type {
  CompressOut,
  ContextOut,
  DismissProposalRequest,
  MemoryOut,
  SaveMemoryRequest,
} from './types'

/** E1 确认 / 编辑后确认 → 落库;内容去重,重复确认返回同一 id(201) */
export function apiSaveMemory(spaceId: string, body: SaveMemoryRequest): Promise<MemoryOut> {
  return http.post<MemoryOut>(`/spaces/${spaceId}/memories`, body).then((r) => r.data)
}

/** E2 忽略提议 → 写去重留痕(204 无体);只传正文与 kind,后端自算哈希 */
export function apiDismissProposal(spaceId: string, body: DismissProposalRequest): Promise<void> {
  return http.post<void>(`/spaces/${spaceId}/memories/proposals/dismiss`, body).then(() => undefined)
}

/** E6 手动压缩;model_id 不传则用会话/空间默认 chat 模型 */
export function apiCompress(
  spaceId: string,
  conversationId: string,
  modelId?: string | null,
): Promise<CompressOut> {
  return http
    .post<CompressOut>(`/spaces/${spaceId}/conversations/${conversationId}/compress`, {
      model_id: modelId ?? null,
    })
    .then((r) => r.data)
}

/** E7 悬浮窗读数(占用/窗口、记忆条数、可压缩数、能否压缩、缓存两态);刷新后读,不进 SSE done 帧 */
export function apiGetContext(spaceId: string, conversationId: string): Promise<ContextOut> {
  return http
    .get<ContextOut>(`/spaces/${spaceId}/conversations/${conversationId}/context`)
    .then((r) => r.data)
}

export type {
  CompressOut,
  ContextOut,
  DismissProposalRequest,
  MemoryOut,
  SaveMemoryRequest,
}
