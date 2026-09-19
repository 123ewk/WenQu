import { ApiError, apiBaseUrl, authHeader, http, refreshAccessToken } from './http'
import type { AskRequest, CitationOut, ConversationOut, MessageOut } from './types'

export function apiListConversations(spaceId: string): Promise<ConversationOut[]> {
  return http.get<ConversationOut[]>(`/spaces/${spaceId}/conversations`).then((r) => r.data)
}

export function apiCreateConversation(spaceId: string, title = '新会话'): Promise<ConversationOut> {
  return http.post<ConversationOut>(`/spaces/${spaceId}/conversations`, { title }).then((r) => r.data)
}

export function apiDeleteConversation(spaceId: string, conversationId: string): Promise<void> {
  return http
    .delete<void>(`/spaces/${spaceId}/conversations/${conversationId}`)
    .then((r) => r.data)
}

/** 会话重命名:title 1–128 字;列表按 updated_at 倒序,重命名后会话会排到最前(属预期) */
export function apiRenameConversation(
  spaceId: string,
  conversationId: string,
  title: string,
): Promise<ConversationOut> {
  return http
    .patch<ConversationOut>(`/spaces/${spaceId}/conversations/${conversationId}`, { title })
    .then((r) => r.data)
}

/** 历史消息:assistant 消息自带 citations,可直接复现引用角标与抽屉 */
export function apiListMessages(spaceId: string, conversationId: string): Promise<MessageOut[]> {
  return http
    .get<MessageOut[]>(`/spaces/${spaceId}/conversations/${conversationId}/messages`)
    .then((r) => r.data)
}

/**
 * SSE 五种事件(按 type 分派)。后端(OPT-3 起)给所有事件载荷附加单调 `seq`(从 1 起,
 * 按生成流独立计数),前端用它做断线续流的游标。
 * `error` 的 `code` 是**可选**的:检索失败带 `code`(如 MODEL_NOT_CONFIGURED),
 * 检索兜底与模型调用失败只有人话 `message` —— 前端按 code 分支前必须先判空。
 * 另注意失败路径**不发 `done`**,收尾不能以 done 为唯一结束信号;
 * `done.partial=true` 表示生成被中断、落库的是部分答案。
 */
export type AskEvent = (
  | { type: 'meta'; conversation_id: string }
  | { type: 'citations'; citations: CitationOut[] }
  | { type: 'delta'; text: string }
  | { type: 'done'; message_id: string; cited_indexes?: number[]; partial?: boolean }
  | { type: 'error'; message: string; code?: string }
) & { seq?: number }

function parseError(status: number, data: unknown): ApiError {
  const shell = (data as { error?: { code?: unknown; message?: unknown; details?: unknown } })?.error
  if (shell && typeof shell.code === 'string') {
    return new ApiError(
      shell.code,
      typeof shell.message === 'string' && shell.message ? shell.message : '请求失败',
      status,
      shell.details ?? null,
    )
  }
  return new ApiError('INTERNAL_ERROR', `请求失败(HTTP ${status})`, status, data ?? null)
}

/**
 * 流式问答(POST SSE)。
 * 用 fetch + ReadableStream 而非 EventSource:后者无法携带 POST 请求体与 Authorization 头。
 * 校验类错误(401/404/422)发生在流开始前,可正常拿到状态码;模型调用失败只会在流内以
 * error 事件返回(HTTP 仍为 200)。
 */
export async function askStream(
  spaceId: string,
  body: AskRequest,
  onEvent: (event: AskEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${apiBaseUrl()}/spaces/${spaceId}/ask`
  const send = () =>
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader() },
      body: JSON.stringify(body),
      signal,
    })

  let res = await send()
  if (res.status === 401) {
    // 与 axios 通道共用一次性 refresh 轮换;失败则交由调用方提示重新登录
    await refreshAccessToken()
    res = await send()
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    throw parseError(res.status, data)
  }
  await readSse(res, onEvent)
}

/**
 * 断线续流(OPT-3,GET SSE):`after` = 最后收到的 `seq`,服务端精确补播其后的缺失事件,
 * 生成仍在进行则接着实时推;已结束则纯回放(含 done)。**无需前端去重**。
 * 不可续(进程重启/事件淘汰/会话无生成记录)→ 404 → 调用方回退 `GET .../messages`。
 */
export async function resumeStream(
  spaceId: string,
  conversationId: string,
  after: number,
  onEvent: (event: AskEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${apiBaseUrl()}/spaces/${spaceId}/conversations/${conversationId}/stream?after=${after}`
  const send = () => fetch(url, { headers: { ...authHeader() }, signal })

  let res = await send()
  if (res.status === 401) {
    await refreshAccessToken()
    res = await send()
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    throw parseError(res.status, data)
  }
  await readSse(res, onEvent)
}

/** 逐帧读取 SSE 响应体:按空行分帧,忽略所有不以 `data:` 开头的行(注释/心跳) */
async function readSse(res: Response, onEvent: (event: AskEvent) => void): Promise<void> {
  if (!res.body) {
    throw new ApiError('INTERNAL_ERROR', '当前浏览器不支持流式响应', 0)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = (raw: string) => {
    for (const line of raw.split('\n')) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      try {
        onEvent(JSON.parse(payload) as AskEvent)
      } catch {
        /* 忽略无法解析的帧(如心跳) */
      }
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      dispatch(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim()) dispatch(buffer)
}
