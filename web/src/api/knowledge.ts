import { http } from './http'
import type {
  ChunkPage,
  CreateKBRequest,
  DocumentOut,
  DocumentPage,
  IngestionProgressOut,
  KnowledgeBaseOut,
  UpdateKBRequest,
} from './types'

export function apiListKbs(spaceId: string): Promise<KnowledgeBaseOut[]> {
  return http.get<KnowledgeBaseOut[]>(`/spaces/${spaceId}/knowledge-bases`).then((r) => r.data)
}

/** 创建知识库需 Editor(20)+;嵌入模型由服务端决定 */
export function apiCreateKb(spaceId: string, body: CreateKBRequest): Promise<KnowledgeBaseOut> {
  return http.post<KnowledgeBaseOut>(`/spaces/${spaceId}/knowledge-bases`, body).then((r) => r.data)
}

export function apiGetKb(spaceId: string, kbId: string): Promise<KnowledgeBaseOut> {
  return http.get<KnowledgeBaseOut>(`/spaces/${spaceId}/knowledge-bases/${kbId}`).then((r) => r.data)
}

/** 改名/改描述需 Editor(20)+ */
export function apiUpdateKb(spaceId: string, kbId: string, body: UpdateKBRequest): Promise<KnowledgeBaseOut> {
  return http
    .patch<KnowledgeBaseOut>(`/spaces/${spaceId}/knowledge-bases/${kbId}`, body)
    .then((r) => r.data)
}

/** 删除知识库需 Admin(30)+,级联删除文档与分块 */
export function apiDeleteKb(spaceId: string, kbId: string): Promise<void> {
  return http.delete<void>(`/spaces/${spaceId}/knowledge-bases/${kbId}`).then((r) => r.data)
}

export function apiListDocuments(
  spaceId: string,
  kbId: string,
  limit: number,
  offset: number,
): Promise<DocumentPage> {
  return http
    .get<DocumentPage>(`/spaces/${spaceId}/knowledge-bases/${kbId}/documents`, {
      params: { limit, offset },
    })
    .then((r) => r.data)
}

/**
 * 上传文档(multipart,字段名 file)需 Editor(20)+。
 * 接口只负责存文件与入队,返回 status=pending;后续状态由后台 worker 推进,需轮询文档列表。
 * onProgress 用于顶栏全局上传进度(浏览器上传字节进度,非服务端解析进度)。
 */
export function apiUploadDocument(
  spaceId: string,
  kbId: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<DocumentOut> {
  const form = new FormData()
  form.append('file', file)
  return http
    .post<DocumentOut>(`/spaces/${spaceId}/knowledge-bases/${kbId}/documents`, form, {
      onUploadProgress: (e) => {
        if (!onProgress) return
        const total = e.total ?? file.size
        onProgress(total ? Math.min(100, Math.round((e.loaded / total) * 100)) : 0)
      },
    })
    .then((r) => r.data)
}

export function apiGetDocument(spaceId: string, kbId: string, docId: string): Promise<DocumentOut> {
  return http
    .get<DocumentOut>(`/spaces/${spaceId}/knowledge-bases/${kbId}/documents/${docId}`)
    .then((r) => r.data)
}

export function apiDeleteDocument(spaceId: string, kbId: string, docId: string): Promise<void> {
  return http
    .delete<void>(`/spaces/${spaceId}/knowledge-bases/${kbId}/documents/${docId}`)
    .then((r) => r.data)
}

export function apiListChunks(
  spaceId: string,
  kbId: string,
  docId: string,
  limit: number,
  offset: number,
): Promise<ChunkPage> {
  return http
    .get<ChunkPage>(`/spaces/${spaceId}/knowledge-bases/${kbId}/documents/${docId}/chunks`, {
      params: { limit, offset },
    })
    .then((r) => r.data)
}

/**
 * 空间级入库进度(顶栏浮层数据源):`active` 只含在途 + 近期失败,不含已完成;
 * `total_active === 0` 即全部处理完。与浏览器上传进度(onUploadProgress)是两回事。
 */
export function apiGetIngestionProgress(spaceId: string, limit = 50): Promise<IngestionProgressOut> {
  return http
    .get<IngestionProgressOut>(`/spaces/${spaceId}/ingestion-progress`, { params: { limit } })
    .then((r) => r.data)
}
