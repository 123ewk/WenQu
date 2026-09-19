/**
 * 与 docs/api/openapi.json 保持一致的手写契约类型。
 * 修改接口后,后端会重新导出契约,此处字段必须随之同步。
 */

export interface UserOut {
  id: string
  username: string
  nickname: string
  created_at: string | null
  /** 当前活动空间(从 access token 推导);注册/登录未选空间为 null */
  current_space_id: string | null
}

/** 登录/注册/刷新响应中 spaces[] 的元素,role 为当前用户在该空间的角色 */
export interface SpaceBriefOut {
  id: string
  name: string
  role: number
  description?: string
}

export interface SpaceOut {
  id: string
  name: string
  description: string
  role: number
  created_at: string | null
}

export interface MemberOut {
  user_id: string
  username: string
  nickname: string
  role: number
  joined_at: string | null
}

export interface AuthResponse {
  user: UserOut
  spaces: SpaceBriefOut[]
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  /** 本响应绑定的活动空间:登录/注册为 null,switch-space 后为新空间,refresh 延续原绑定 */
  current_space_id: string | null
}

export interface AuditLogOut {
  id: string
  actor_id: string | null
  action: string
  target: string
  detail: Record<string, unknown>
  ip: string
  created_at: string | null
}

export interface AuditPage {
  items: AuditLogOut[]
  total: number
}

/* ───── 请求体 ───── */

export interface RegisterRequest {
  username: string
  nickname: string
  password: string
}

export interface LoginRequest {
  username: string
  password: string
}

export interface CreateSpaceRequest {
  name: string
  description?: string
}

export interface UpdateSpaceRequest {
  name?: string
  description?: string
}

export interface AddMemberRequest {
  username: string
  role: number
}

export interface ChangeRoleRequest {
  role: number
}

export interface UpdateProfileRequest {
  nickname: string
}

export interface ChangePasswordRequest {
  old_password: string
  new_password: string
}

/* ───── M2:知识库 / 文档 / 分块 ───── */

export interface KnowledgeBaseOut {
  id: string
  space_id: string
  name: string
  description: string
  /** 嵌入模型由服务端配置,创建/改名接口不接受该字段 */
  embedding_model: string
  embedding_dim: number
  created_at: string | null
}

export interface CreateKBRequest {
  name: string
  description?: string
}

export interface UpdateKBRequest {
  name: string
  description?: string
}

/** 入库状态机:pending → parsing → chunking → embedding → completed / failed */
export type DocumentStatus = 'pending' | 'parsing' | 'chunking' | 'embedding' | 'completed' | 'failed'

export interface DocumentOut {
  id: string
  kb_id: string
  filename: string
  /** pdf/docx/xlsx/pptx/md/txt */
  format: string
  size_bytes: number
  status: DocumentStatus
  /** failed 时为 INGEST_FAILED */
  error_code: string | null
  created_at: string | null
}

export interface DocumentPage {
  items: DocumentOut[]
  total: number
}

/* ───── 空间级入库进度(顶栏浮层) ───── */

/** 在途(pending/parsing/chunking/embedding)+ 近期失败的文档;已完成的不返回 */
export interface IngestionProgressItem {
  document_id: string
  kb_id: string
  filename: string
  status: string
  error_code: string | null
  error_message: string | null
  updated_at: string | null
}

export interface IngestionProgressOut {
  active: IngestionProgressItem[]
  /** 各状态文档数(含 completed),键为状态名 */
  counts: Record<string, number>
  /** 未终结数量;为 0 即"都处理完了" */
  total_active: number
  /** 有失败文档时为真(独立于 index_status 的优先级) */
  has_failure: boolean
}

/** 分块元数据:breadcrumb(标题路径)/page/kind(text|table,后端不区分标题块) */
export interface ChunkMeta {
  breadcrumb?: string[]
  page?: number | null
  kind?: string
}

export interface ChunkOut {
  id: string
  seq: number
  content: string
  meta: ChunkMeta
}

export interface ChunkPage {
  items: ChunkOut[]
  total: number
}

/* ───── M2:检索 ───── */

export interface SearchRequest {
  query: string
  top_k?: number
  kb_ids?: string[] | null
  model_id?: string | null
}

export interface RetrievedChunkOut {
  chunk_id: string
  document_id: string
  kb_id: string
  filename: string
  content: string
  /** RRF 融合分 */
  score: number
  /** 向量召回排名,未命中为 null */
  vector_rank: number | null
  /** 全文召回排名,未命中为 null */
  fulltext_rank: number | null
  meta: ChunkMeta
}

export interface ChunkPreviewRequest {
  text: string
  /** 仅支持 md / txt */
  format?: string
}

export interface ChunkPreviewItem {
  content: string
  tokens: number
  breadcrumb: string[]
  page: number | null
  kind: string
}

/* ───── M3:API Key ───── */

/**
 * 能力取值与后端 `ApiCapability` 一致(契约里是 list[str],故这里也保留 string):
 * `chat` = 对话检索(POST /ask 与 /search);`documents` = 文档管理。
 */
export type ApiCapability = 'chat' | 'documents'

export interface ApiKeyOut {
  id: string
  name: string
  description: string
  /** 形如 sk-live-****ab12;完整明文只在创建响应里出现一次 */
  key_hint: string
  capabilities: string[]
  /** 空数组 = 全部知识库 */
  kb_ids: string[]
  /** 非 null 即已吊销 */
  revoked_at: string | null
  last_used_at: string | null
  created_at: string | null
}

export interface ApiKeyCreatedOut extends ApiKeyOut {
  /** 完整明文 Key:仅创建响应返回一次,之后任何接口都取不回 */
  plaintext: string
}

export interface CreateApiKeyRequest {
  name: string
  description?: string
  /** 空数组合法但等于"什么都不能做"(后端 fail-closed) */
  capabilities?: string[]
  /** 空数组 = 全部知识库 */
  kb_ids?: string[]
}

/* ───── M2:会话与问答 ───── */

export interface ConversationOut {
  id: string
  space_id: string
  title: string
  created_at: string | null
  updated_at: string | null
}

export interface CitationOut {
  /** 从 1 开始,对应答案正文里的 [n] */
  index: number
  chunk_id: string
  document_id: string
  kb_id: string
  filename: string
  excerpt: string
  score: number
  breadcrumb: string[]
  page: number | null
}

export interface MessageOut {
  id: string
  /** user | assistant */
  role: string
  content: string
  seq: number
  citations: CitationOut[]
  model_id: string | null
  created_at: string | null
}

export interface AskRequest {
  question: string
  /** 首次提问不传,由首个 meta 事件返回新会话 id */
  conversation_id?: string | null
  kb_ids?: string[] | null
  top_k?: number
  model_id?: string | null
}
