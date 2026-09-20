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

/** 空间级检索参数(真实驱动检索,改完立即生效无需重建索引);权重之和 ≤ 1 */
export interface RetrievalParams {
  rrf_k: number
  vector_weight: number
  fulltext_weight: number
  min_score: number
  default_top_k: number
}

export interface SpaceOut {
  id: string
  name: string
  description: string
  role: number
  retrieval_params: RetrievalParams
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
  /** 契约必填,省略 422 */
  name: string
  /** 省略或 null = 不改动(`098cc68` 修复);显式 "" = 清空 */
  description?: string | null
  /** 省略 = 不改动 */
  retrieval_params?: RetrievalParams
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

/* ───── OPT-27:空间模型配置(挂空间) ─────
 * 后端分批合并中,本组类型按《给前端的对接说明-OPT27…》§1 手工定义;
 * 每批 openapi 重导后须逐字段核对勘误。 */

export interface EmbeddingConfigOut {
  /** 内置锁死模型(text-embedding-v3),页面不可改 */
  model_name: string
  /** 固定 1024;换模型会使已入库向量失效 */
  dims: number
  /** 引导用户去建 API Key 的官网地址 */
  doc_url: string
  key_configured: boolean
  /** 只回显掩码(如 `sk-1...9b`),明文永不回传 */
  key_hint: string | null
}

export interface SpaceModelConfigOut {
  embedding: EmbeddingConfigOut
  /** true = 本空间未配 Key、开发环境静默回落运维全局 Key(生产恒 false,须空间自配) */
  fallback_active: boolean
  rerank: { enabled: boolean }
}

/** GET .../model-config/types 下拉项:key 即提交的 provider_type,base_url 用它预填 */
export interface ModelTypeOption {
  key: string
  display_name: string
  default_base_url: string
}

export interface ModelProviderOut {
  id: string
  /** chat | rerank(§1.5:rerank 复用同一套 providers/models 端点,靠 kind 区分) */
  kind: string
  provider_type: string
  /** 空间内唯一,列表展示名 */
  name: string
  base_url: string
  key_hint: string | null
}

export interface ModelProviderCreateRequest {
  kind?: string
  provider_type: string
  name: string
  base_url: string
  /** 必填;明文只在这一跳出现,落库即加密 */
  api_key: string
}

export interface ModelProviderUpdateRequest {
  name?: string
  base_url?: string
  /** 不传 = 不修改已存 Key */
  api_key?: string
}

export interface SpaceModelOut {
  id: string
  kind: string
  provider_id: string
  provider_name: string
  /** 空间内唯一,展示名 */
  model_id: string
  /** 实际调上游用的模型名,如 deepseek-chat */
  model_name: string
  /** chat 新建必填,OPT-29 进度条分母;rerank 无此字段 */
  context_tokens: number | null
  /** 同空间同 kind 仅一个默认(后端互斥自动顶替);问答未指定模型时用它 */
  is_default: boolean
}

export interface SpaceModelCreateRequest {
  kind?: string
  provider_id: string
  model_name: string
  context_tokens?: number | null
  is_default?: boolean
}

export interface SpaceModelUpdateRequest {
  model_name?: string
  context_tokens?: number | null
  is_default?: boolean
}

/** 测试类端点成功回包;失败走 424 UPSTREAM_MODEL_ERROR(details.reason ∈ auth|network|model_name) */
export interface ModelTestResultOut {
  ok: boolean
}

/* ───── OPT-28:Token 用量(挂空间) ─────
 * 同 OPT-27:类型按对接说明 §2 手工定义,openapi 重导后核对。 */

/** 时间窗取值;"all" 的字面量待契约核对(文档只示例了 period=30d) */
export type UsagePeriod = '7d' | '30d' | 'all'

/** 用量口径:upstream = 供应商回传的精确值;estimated = 后端估算(展示打 ≈ 角标) */
export type UsageSource = 'upstream' | 'estimated'

export interface TokenUsageTotals {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  chat_calls?: number
  embedding_calls?: number
  source?: UsageSource
}

/** 按成员汇总行(Admin+ 汇总表) */
export interface MemberTokenUsage extends TokenUsageTotals {
  user_id: string
  username: string
  nickname: string
  role: number
}

export interface TokenUsageOut {
  period: string
  generated_at: string | null
  space_total: TokenUsageTotals
  members: MemberTokenUsage[]
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
  /** OPT-4 父子分块:父块内子块预览,未触发切分时空数组(子块只活在检索索引,别处不出现) */
  children?: ChunkPreviewItem[]
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
  /* ───── OPT-26:Key 绑定空间成员,以下字段随列表/创建响应返回 ───── */
  /** 绑定成员 id;存量旧 Key 回退签发者,账号注销后为 null(实测:被移出空间时 id 仍在) */
  member_user_id: string | null
  /** 成员注销或被移出空间后为 null —— 前端以"username 为 null"判定"已注销"并置灰 */
  member_username: string | null
  member_nickname: string | null
  /** 绑定时点的空间角色数值(40/30/20/10),与成员页同一口径 */
  member_role: number | null
  /** 签发操作者:仅留痕,不参与运行时身份(签发者被移出不影响 Key) */
  created_by: string | null
  created_by_name: string | null
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
  /** ★ OPT-26 新增必填:该空间的现存成员 user_id;缺失 422,非本空间成员 404 MEMBER_NOT_FOUND */
  member_user_id: string
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

/** agent 模式(OPT-10)单次工具调用轨迹;output_preview 为截断展示文本,非全文 */
export interface AgentToolCallOut {
  id: string
  name: string
  args: Record<string, unknown>
  ok: boolean
  duration_ms: number
  output_preview: string
}

/** ReAct 一轮:thought 是模型思考文本(可能为空),tool_calls 为该轮调用 */
export interface AgentStepOut {
  round: number
  thought: string
  tool_calls: AgentToolCallOut[]
}

export interface MessageOut {
  id: string
  /** user | assistant */
  role: string
  content: string
  seq: number
  citations: CitationOut[]
  model_id: string | null
  /** OPT-10 agent 模式的工具调用轨迹;直检模式恒为 null/缺省,判空回退 citations 合成 */
  agent_steps?: AgentStepOut[] | null
  created_at: string | null
}

export interface AskRequest {
  question: string
  /** 首次提问不传,由首个 meta 事件返回新会话 id */
  conversation_id?: string | null
  kb_ids?: string[] | null
  top_k?: number
  model_id?: string | null
  /** OPT-10:true = ReAct 工具循环(SSE 增 tool_call/tool_result 事件);缺省 false 行为不变 */
  agent?: boolean
}
