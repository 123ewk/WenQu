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
 * 已按 docs/api/openapi.json(52 路径 / 74 方法)逐字段核对。
 * 读口只有一个聚合 GET .../model-config(没有单独的 providers/models 列表接口);
 * 写接口分两类:POST/PATCH 供应商与模型回实体行,其余(embedding-key、
 * rerank-switch、两条 test 探活)只回 OkOut{ok} —— 改完必须重拉聚合才能拿到掩码/默认态。 */

export interface EmbeddingCardOut {
  /** 内置锁死模型(text-embedding-v3),页面不可改 */
  model_name: string
  /** 固定 1024;换模型会使已入库向量失效 */
  dims: number
  /** 引导用户去建 API Key 的官网地址 */
  doc_url: string
  key_configured: boolean
  /** 掩码字符串(4+****+4),后端直接给,前端原样显示;未配置为空串 */
  key_hint: string
  /** 仅 dev 可能 true(空间没配 Key、全局 Key 顶上);prod 恒 false,不要写死 */
  fallback_active: boolean
}

export interface RerankCardOut {
  /** 开关与"有没有配 rerank 模型"解耦;开了但没默认模型 = 等同关闭 */
  enabled: boolean
  default_model: string | null
}

/** 聚合视图:进页面一次拿全四区块,providers/models 只含本空间层的行(全局合并清单走 GET /models,别混用) */
export interface SpaceModelConfigOut {
  embedding: EmbeddingCardOut
  rerank: RerankCardOut
  providers: SpaceProviderOut[]
  models: SpaceModelOut[]
}

/** GET .../model-config/types 下拉项:key 即提交的 provider_type,base_url 用 default_base_url 预填 */
export interface ProviderTypeOut {
  key: string
  /** 显示名字段就叫 label(不是 display_name) */
  label: string
  doc_url: string
  default_base_url: string
  /** 协议由类型决定(如 openai_compatible / cohere_rerank / dashscope_rerank);前端不枚举协议,分块看 SpaceProviderOut.kinds */
  protocol: string
  /** 该类型可挂的模型类别;供应商下拉按它分开渲染(chat 区排 rerank 类型,反之亦然) */
  kinds: string[]
  /** OPT-36:模型名提示清单(非白名单),下拉数据源;[] = 无预设退纯输入框;顺序即下拉顺序 */
  model_names: string[]
}

export interface SpaceProviderOut {
  id: string
  /** 本空间层内唯一(与全局同名不算冲突,且会遮蔽全局那条);也是 model_id 合成前缀 */
  name: string
  /** 由 provider_type 决定,建好锁死(PATCH 不可改) */
  protocol: string
  base_url: string
  key_hint: string
  /** 关掉后其模型从 GET /models 清单消失 */
  enabled: boolean
  /** 该供应商下的模型数;为 0 时【测试连接】会 422,按钮应禁用 */
  model_count: number
  /** OPT-35:该供应商允许挂载的模型类别(按 protocol 从类型表派生,非"实际挂了哪些 kind");[] = 未知协议,两区块都不列 */
  kinds: string[]
}

export interface SpaceProviderCreateRequest {
  /** 必须 ∈ /types 的 key(未知 422 并列出可选值) */
  provider_type: string
  name: string
  /** 留空 = 用该类型的 default_base_url;只收不含空白的 ASCII,中间空格/中文 422 */
  base_url?: string
  /** 空 = 免密钥本地端点(Ollama/vLLM);明文只在这一跳出现,落库即加密 */
  api_key?: string
  enabled?: boolean
}

export interface SpaceProviderUpdateRequest {
  name?: string
  base_url?: string
  /** 不传 = 不动;空串 = 清除;非空 = 更换(表单"留空即不改"时必须整个字段 omit) */
  api_key?: string
  enabled?: boolean
}

export interface SpaceModelOut {
  /** 表主键:PATCH/DELETE/test 的路径参数用它,不是 model_id */
  id: string
  /** 对外 id(进 GET /models 与 /ask 的那个);本空间层内唯一,与全局同名=遮蔽 */
  model_id: string
  provider_id: string
  provider_name: string
  /** chat | rerank(空间层没有 embedding) */
  kind: string
  /** 实际调上游用的名字,如 deepseek-chat */
  model_name: string
  /** chat 新建必填(缺了 422),OPT-29 进度条分母;rerank 不需要 */
  context_tokens: number | null
  /** 关掉 = 从 GET /models 清单消失,且不能被设成默认 */
  enabled: boolean
  /** 同空间同 kind 仅一个默认(后端互斥,新的顶掉旧的);问答未指定模型时用它 */
  is_default: boolean
}

export interface SpaceModelCreateRequest {
  provider_id: string
  /** 必须与供应商 protocol 匹配:重排协议(`*_rerank`)只挂 rerank,其余只挂 chat(错给 422) */
  kind: string
  model_name: string
  /** 不传 = 后端合成 `{供应商 name}/{model_name}`;自定义撞库 409 MODEL_ID_TAKEN */
  model_id?: string
  context_tokens?: number | null
  enabled?: boolean
  is_default?: boolean
}

/** 只开放 model_name/context_tokens/enabled/is_default;provider_id、kind、model_id 建好即锁死(改=删除重建) */
export interface SpaceModelUpdateRequest {
  model_name?: string
  context_tokens?: number | null
  enabled?: boolean
  is_default?: boolean
}

/** PUT embedding-key 入参;空串 = 清除本空间 Key(prod 清完向量化直接不可用) */
export interface EmbeddingKeyRequest {
  api_key?: string
}

/** POST embedding-key/test 入参;不传 = 测已存 Key */
export interface EmbeddingKeyTestRequest {
  api_key?: string | null
}

export interface RerankSwitchRequest {
  enabled: boolean
}

/** 写/探活类端点的统一回包;掩码与默认态只以重拉的聚合为准 */
export interface OkOut {
  ok: boolean
}

/* ───── OPT-28:Token 用量(挂空间) ─────
 * 已按 openapi.json 核对:响应里没有 `source` 字段(那是库里的列),
 * 区分厂商实测/本地估算靠 estimated_tokens(估算部分行的 total 之和,>0 必须标 ≈)。 */

/** 时间窗是契约里的 Literal(7d|30d|all),传未知值(如 1y)直接 422,不静默回落 */
export type UsagePeriod = '7d' | '30d' | 'all'

/** 六项全必填 int;对话与向量化的 token 混在 total 里,按类别看用量用 *_calls */
export interface UsageOut {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  /** 其中"本地估算"口径那部分的 total 之和;估算与实测不混算 */
  estimated_tokens: number
  /** 上游调用次数,不是提问次数(Agent 一轮提问多行) */
  chat_calls: number
  embedding_calls: number
}

/** 当前花名册逐成员行,零用量成员也在(全 0);Σmembers ≤ space_total(差值=已离成员,不等是设计如此) */
export interface MemberUsageOut extends UsageOut {
  user_id: string
  username: string
  nickname: string
  role: number
}

/** GET .../token-usage(Admin+;Viewer/Editor 是 403 不是 404) */
export interface UsageSummaryOut {
  period: UsagePeriod
  /** period=all 时为 null(不是 0),别当日期用 */
  since: string | null
  /** 服务端 UTC 时刻,直接显示"截至 …" */
  generated_at: string
  space_total: UsageOut
  members: MemberUsageOut[]
}

/** GET .../token-usage/me(任意成员可读;没有 members 字段,别复用汇总表数据层) */
export interface MyUsageOut {
  period: UsagePeriod
  since: string | null
  generated_at: string
  usage: UsageOut
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

/* ───── OPT-41:权限档位(权限的唯一真相,人与 Key 共用) ───── */

/** 档位上的一个成员挂载行;`is_current` = 此人此刻生效的就是这一档 */
export interface GroupMemberOut {
  user_id: string
  username: string | null
  nickname: string | null
  /** null = 人已不在本空间(只在"移出时选择保留挂载"下出现),展示置灰 */
  role: number | null
  is_current: boolean
}

export interface PermissionGroupOut {
  id: string
  name: string
  description: string
  /** 空数组合法但含义是"什么都不能做"(fail-closed),不是"全部允许" */
  capabilities: string[]
  /** all_kbs=false 时的白名单;`[]` + `all_kbs=false` = 一个库都不给 */
  kb_ids: string[]
  /** 放开全部库必须是显式勾选;为真时每次请求实时展开(新建库自动在范围内) */
  all_kbs: boolean
  members: GroupMemberOut[]
  created_at: string | null
}

/** 建档与改档共用(PATCH 走整组替换语义,不做半字段补丁) */
export interface UpsertPermissionGroupRequest {
  name: string
  description?: string
  capabilities?: string[]
  kb_ids?: string[]
  all_kbs?: boolean
}

/** 清空档位下"已吊销凭据"的结果(OPT-44);0 = 本来就没有历史凭据,不是错误 */
export interface PurgeRevokedKeysOut {
  deleted_count: number
}

/**
 * 删档被拒(400 VALIDATION_ERROR)时 `error.details` 的字段。
 * 后端把在用的与已吊销的分开计数:前者出路是"吊销",后者是"清空历史凭据"。
 */
export interface GroupDeleteBlockedDetails {
  group_id?: string
  key_count?: number
  active_key_count?: number
  revoked_key_count?: number
  member_count?: number
}

/** Key 响应里内联的档位快照:要看/要改权限去档位页,Key 这边只读 */
export interface ApiKeyGroupRef {
  id: string
  name: string
  capabilities: string[]
  kb_ids: string[]
  all_kbs: boolean
}

export interface ApiKeyOut {
  id: string
  name: string
  description: string
  /** 形如 sk-live-****ab12;完整明文只在创建响应里出现一次 */
  key_hint: string
  /** ★ OPT-41:能力与知识库范围都在档位上,Key 行不再带权限字段 */
  group: ApiKeyGroupRef
  /* ───── OPT-26 起 Key 以某个成员身份行事;OPT-41 明确这组字段只管身份不管权限 ───── */
  /** 归因成员 id;存量旧 Key 回退签发者 */
  owner_user_id: string | null
  /** 成员注销或被移出空间后为 null —— 以"username 为 null"判定"已失效"并置灰 */
  owner_username: string | null
  owner_nickname: string | null
  /** 归因成员的角色数值(40/30/20/10),与成员页同一口径 */
  owner_role: number | null
  /** 签发操作者:仅留痕,不参与运行时身份(签发者被移出不影响 Key) */
  created_by: string | null
  created_by_name: string | null
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
  /** ★ OPT-41 必填:权限档位 id;档位不属于本空间 → 404 */
  group_id: string
  /** ★ 归因成员(该空间现存成员);只决定身份与用量归因,不决定权限 */
  owner_user_id: string
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
  /** OPT-29 上下文窗口进度条数据源(仅 assistant 消息;老消息/后端未合并时缺省) */
  context_used_tokens?: number | null
  context_window_tokens?: number | null
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
