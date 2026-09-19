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
  /** 最近一次成功登录;失败登录不改写 */
  last_login_at: string | null
  /** 直连客户端地址(未解析 X-Forwarded-For,挂网关时显示代理 IP → 文案保持中性) */
  last_login_ip: string | null
  /** 有头像时为 "/users/me/avatar"(不带 /api/v1 前缀);仅对自己有意义 */
  avatar_url: string | null
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
  /** 展示枚举:success | denied(denied 仅记 403 越权与登录/注册 401);筛选被拒记录用 action=access.denied */
  result: 'success' | 'denied'
  /** 操作人昵称冗余;账号已删除为 null */
  actor_name: string | null
}

export interface AuditPage {
  items: AuditLogOut[]
  total: number
}

/** 审计筛选参数;since/until 为 ISO8601 闭区间 */
export interface AuditFilter {
  action?: string
  actor_id?: string
  since?: string
  until?: string
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

/** 知识库索引聚合状态:判序 empty → processing → degraded → ready(在途优先于失败) */
export type KbIndexStatus = 'empty' | 'processing' | 'degraded' | 'ready'

export interface KnowledgeBaseOut {
  id: string
  space_id: string
  name: string
  description: string
  /** 嵌入模型由服务端配置,创建/改名接口不接受该字段 */
  embedding_model: string
  embedding_dim: number
  /** 以下四项为列表/详情接口一次算齐的聚合统计 */
  document_count: number
  chunk_count: number
  /** 原始文件体积之和(字节) */
  size_bytes: number
  index_status: KbIndexStatus
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
  /** 任务侧真实失败原因(可能偏运维向);error_code 是稳定机器码,分支用它 */
  error_message: string | null
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
  /** 入库时算好的 token 数;可能为 null */
  tokens: number | null
}

export interface ChunkPage {
  items: ChunkOut[]
  total: number
}

/* ───── 模型清单(对话选模型;全局,不挂空间) ───── */

export interface ModelItem {
  /** 提交值:对话传 model_id 用它,不是 model 字段 */
  id: string
  /** 供应商显示名(可做下拉分组) */
  provider: string
  provider_key: string
  model: string
  /** 仅 embedding 有意义 */
  dims: number | null
  /** 仅 chat 有意义 */
  context_tokens: number | null
}

export interface ModelCatalogOut {
  chat: ModelItem[]
  embedding: ModelItem[]
  rerank: ModelItem[]
  /** 各类别的默认模型 id */
  defaults: Record<string, string>
}

/* ───── M2:检索 ───── */

export interface SearchRequest {
  query: string
  /** 缺省用空间配置值 */
  top_k?: number
  kb_ids?: string[] | null
  model_id?: string | null
  /** 以下为请求级覆盖(仅本次生效,不写回空间配置);vector/fulltext 必须成对且和 ≤ 1 */
  rrf_k?: number
  vector_weight?: number
  fulltext_weight?: number
  min_score?: number
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
