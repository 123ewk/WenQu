import { http } from './http'
import type { ChunkPreviewItem, ChunkPreviewRequest, RetrievedChunkOut, SearchRequest } from './types'

/** 混合检索测试:向量 ∥ 全文双路召回 + RRF 融合 */
export function apiRetrievalSearch(spaceId: string, body: SearchRequest): Promise<RetrievedChunkOut[]> {
  return http.post<RetrievedChunkOut[]>(`/spaces/${spaceId}/retrieval/search`, body).then((r) => r.data)
}

/** 分块试切:粘贴文本即时看分块,纯调参工具不落库(format 仅 md/txt) */
export function apiChunkPreview(body: ChunkPreviewRequest): Promise<ChunkPreviewItem[]> {
  return http.post<ChunkPreviewItem[]>('/chunks/preview', body).then((r) => r.data)
}
