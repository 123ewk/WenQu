<script setup lang="ts">
import { ArrowDown, ArrowUp, Close, Collection, Delete, Document, Promotion, WarningFilled } from '@element-plus/icons-vue'

import {
  apiCreateConversation,
  apiDeleteConversation,
  apiListConversations,
  apiListMessages,
  askStream,
} from '@/api/chat'
import { ApiError, errMessage } from '@/api/http'
import { apiListKbs } from '@/api/knowledge'
import type { CitationOut, ConversationOut, KnowledgeBaseOut, MessageOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtClock, fmtDayGroup } from '@/utils/format'
import { renderMarkdown } from '@/utils/markdown'

/**
 * 对话页(原型 02,核心页)。
 * SSE 五类事件:meta(新会话 id)/ citations / delta(累加正文)/ done / error。
 * 引用角标 [n] 对应 citations[n-1],点击打开右侧引用抽屉展示 excerpt 与溯源信息。
 * 契约未提供:会话重命名、非流式重试、查看完整原文块 —— 对应入口不渲染(见 对接缺口清单)。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const spaceId = computed(() => auth.currentSpaceId ?? '')

/* ───── 会话列表 ───── */

const conversations = ref<ConversationOut[]>([])
const listLoading = ref(false)
const listError = ref<string | null>(null)

const groupedConversations = computed(() => {
  const order: Array<'今天' | '昨天' | '本周更早' | '更早'> = ['今天', '昨天', '本周更早', '更早']
  const buckets = new Map<string, ConversationOut[]>()
  for (const c of conversations.value) {
    const key = fmtDayGroup(c.updated_at ?? c.created_at)
    const list = buckets.get(key) ?? []
    list.push(c)
    buckets.set(key, list)
  }
  return order.filter((k) => buckets.has(k)).map((k) => ({ label: k, items: buckets.get(k)! }))
})

/* ───── 消息 ───── */

/** 历史消息来自接口;`stopped` 为前端本地标记(用户中断且模型尚未产出正文) */
type LocalMessage = MessageOut & { stopped?: boolean }

const messages = ref<LocalMessage[]>([])
const msgLoading = ref(false)
const msgError = ref<string | null>(null)

/** 当前会话 id:优先取路由参数,其次取本轮 SSE meta 事件返回的新 id */
const activeConversationId = computed(() => (route.params.conversationId as string | undefined) ?? null)

/** 流式中的临时状态 */
const streaming = ref(false)
const streamText = ref('')
const streamCitations = ref<CitationOut[]>([])
let abortController: AbortController | null = null

/* ───── 引用抽屉 ───── */

const drawerOpen = ref(false)
const drawerCitations = ref<CitationOut[]>([])

function openCitations(citations: CitationOut[], index?: number) {
  drawerCitations.value = citations
  drawerOpen.value = true
  if (index) {
    nextTick(() => {
      document.querySelector(`[data-cite-card="${index}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    })
  }
}

function closeDrawer() {
  drawerOpen.value = false
}

/* ───── 知识库范围 ───── */

const kbs = ref<KnowledgeBaseOut[]>([])
const selectedKbIds = ref<string[]>([])

const kbScopeLabel = computed(() => {
  if (kbs.value.length === 0) return '知识库范围:暂无知识库'
  if (selectedKbIds.value.length === 0) return '知识库范围:全部'
  if (selectedKbIds.value.length === kbs.value.length) return `知识库范围:全部(${kbs.value.length})`
  return `知识库范围:已选 ${selectedKbIds.value.length} 个`
})

const allKbSelected = computed({
  get: () => kbs.value.length > 0 && selectedKbIds.value.length === kbs.value.length,
  set: (v: boolean) => {
    selectedKbIds.value = v ? kbs.value.map((k) => k.id) : []
  },
})

/* ───── 输入 ───── */

const draft = ref('')
const textarea = ref<HTMLTextAreaElement>()
const canSend = computed(() => draft.value.trim().length > 0 && !streaming.value)
/** 展开的检索工具卡片(按消息 id) */
const expandedTool = ref<string | null>(null)

function toggleTool(messageId: string) {
  expandedTool.value = expandedTool.value === messageId ? null : messageId
}

/** 工具卡片里展示的检索范围:由本轮引用的来源知识库名去重得到 */
function scopeSummary(citations: CitationOut[]): string {
  const kbIds = [...new Set(citations.map((c) => c.kb_id))]
  const names = kbIds.map((id) => kbs.value.find((k) => k.id === id)?.name ?? '未知知识库')
  return names.length ? names.join('、') : '—'
}

const EXAMPLE_QUESTIONS = [
  '合同审批的金额分界是多少?',
  '这个知识库里有哪些规范要求?',
  '帮我总结一下关键流程',
]

function useChip(text: string) {
  draft.value = text
  autoGrow()
  textarea.value?.focus()
}

function autoGrow() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`
}

/* ───── 数据加载 ───── */

// 切换空间:清空当前会话并重新拉列表;首帧挂载不重置(否则会丢掉路由里的会话 id)
watch(
  () => auth.currentSpaceId,
  async (_next, prev) => {
    if (!spaceId.value) return
    abortStream()
    if (prev !== undefined) resetConversation()
    await Promise.all([loadConversations(), loadKbs()])
  },
  { immediate: true },
)

// 直接打开 /chat/:id(含刷新)也要加载历史消息,故 immediate
watch(
  activeConversationId,
  async (id) => {
    if (!id) {
      messages.value = []
      return
    }
    await loadMessages(id)
  },
  { immediate: true },
)

async function loadConversations() {
  listLoading.value = true
  listError.value = null
  try {
    conversations.value = await apiListConversations(spaceId.value)
  } catch (e) {
    listError.value = errMessage(e)
    conversations.value = []
  } finally {
    listLoading.value = false
  }
}

async function loadKbs() {
  try {
    kbs.value = await apiListKbs(spaceId.value)
    selectedKbIds.value = kbs.value.map((k) => k.id)
  } catch {
    kbs.value = []
    selectedKbIds.value = []
  }
}

async function loadMessages(conversationId: string) {
  msgLoading.value = true
  msgError.value = null
  try {
    const list = await apiListMessages(spaceId.value, conversationId)
    messages.value = list
    const lastAssistant = [...list].reverse().find((m) => m.role === 'assistant' && m.citations.length > 0)
    if (lastAssistant) {
      drawerCitations.value = lastAssistant.citations
      drawerOpen.value = true
    }
  } catch (e) {
    msgError.value = errMessage(e)
    messages.value = []
  } finally {
    msgLoading.value = false
  }
}

function resetConversation() {
  messages.value = []
  streamText.value = ''
  streamCitations.value = []
  drawerOpen.value = false
  msgError.value = null
  if (route.params.conversationId) router.push('/chat')
}

/* ───── 发送 / 流式 ───── */

function abortStream() {
  abortController?.abort()
  abortController = null
  streaming.value = false
}

async function onSend() {
  const question = draft.value.trim()
  if (!question || streaming.value) return

  draft.value = ''
  nextTick(autoGrow)

  const userMessage: LocalMessage = {
    id: `local-user-${Date.now()}`,
    role: 'user',
    content: question,
    seq: messages.value.length,
    citations: [],
    model_id: null,
    created_at: new Date().toISOString(),
  }
  messages.value = [...messages.value, userMessage]

  streaming.value = true
  streamText.value = ''
  streamCitations.value = []
  drawerOpen.value = false
  drawerCitations.value = []

  abortController = new AbortController()
  let conversationId = activeConversationId.value
  let streamError: string | null = null
  let aborted = false
  const citedIndexes: number[] = []

  try {
    await askStream(
      spaceId.value,
      {
        question,
        conversation_id: conversationId,
        kb_ids: selectedKbIds.value.length === kbs.value.length ? null : selectedKbIds.value,
        top_k: 6,
      },
      (event) => {
        if (event.type === 'meta') {
          conversationId = event.conversation_id
        } else if (event.type === 'citations') {
          streamCitations.value = event.citations
          drawerCitations.value = event.citations
        } else if (event.type === 'delta') {
          streamText.value += event.text
        } else if (event.type === 'done') {
          citedIndexes.push(...(event.cited_indexes ?? []))
        } else if (event.type === 'error') {
          streamError = event.message
        }
      },
      abortController.signal,
    )
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') {
      aborted = true // 用户主动停止:保留已生成内容
    } else if (e instanceof ApiError && e.code === 'MODEL_CALL_FAILED') {
      streamError = '模型调用失败,请稍后重试'
    } else {
      streamError = errMessage(e)
    }
  } finally {
    streaming.value = false
    abortController = null
  }

  const answerText = streamText.value

  // 落地为一条正式助手消息;cited_indexes 决定抽屉里展示哪几条引用
  if (answerText || streamError) {
    const citations = streamError
      ? []
      : (citedIndexes.length
          ? streamCitations.value.filter((c) => citedIndexes.includes(c.index))
          : streamCitations.value)
    messages.value = [
      ...messages.value,
      {
        id: `local-assistant-${Date.now()}`,
        role: 'assistant',
        content: streamError ? `生成失败:${streamError}` : answerText,
        seq: messages.value.length,
        citations,
        model_id: null,
        created_at: new Date().toISOString(),
      },
    ]
  } else if (aborted) {
    // 中断时模型尚未产出正文:显式标注已停止,避免留下无回答的孤立提问
    messages.value = [
      ...messages.value,
      {
        id: `local-assistant-${Date.now()}`,
        role: 'assistant',
        content: '',
        seq: messages.value.length,
        citations: [],
        model_id: null,
        created_at: new Date().toISOString(),
        stopped: true,
      },
    ]
  }
  streamText.value = ''
  streamCitations.value = []

  if (conversationId && conversationId !== activeConversationId.value) {
    // 首次提问由后端新建会话:写入路由并刷新侧栏
    await router.replace(`/chat/${conversationId}`)
    await loadConversations()
  } else if (conversationId) {
    await loadConversations()
  }

  nextTick(() => {
    document.querySelector('.chat-scroll')?.scrollTo({ top: 999999, behavior: 'smooth' })
  })
}

function onStop() {
  abortStream()
  ElMessage.info('已停止生成')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    if (canSend.value) void onSend()
  }
}

/* ───── 会话操作 ───── */

async function onNewConversation() {
  // 无残留消息时直接留在空态;有会话则先建一个空会话,便于侧栏立即可见
  abortStream()
  if (!messages.value.length) {
    resetConversation()
    return
  }
  try {
    const conversation = await apiCreateConversation(spaceId.value)
    await loadConversations()
    await router.push(`/chat/${conversation.id}`)
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

async function onDeleteConversation(conversation: ConversationOut) {
  const confirmed = await ElMessageBox.confirm(
    `删除会话「${conversation.title || '新会话'}」?该会话的全部消息与引用记录将被移除。`,
    '删除会话',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteConversation(spaceId.value, conversation.id)
    if (conversation.id === activeConversationId.value) resetConversation()
    await loadConversations()
    ElMessage.success('会话已删除')
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

function onPickConversation(conversation: ConversationOut) {
  if (conversation.id === activeConversationId.value) return
  abortStream()
  router.push(`/chat/${conversation.id}`)
}

/* ───── Markdown 渲染与角标点击 ───── */

function assistantHtml(message: LocalMessage): string {
  return renderMarkdown(message.content, message.citations.length)
}

function onContentClick(event: MouseEvent, message: LocalMessage) {
  const target = event.target as HTMLElement
  const cite = target.closest('[data-cite]')
  if (cite) {
    openCitations(message.citations, Number(cite.getAttribute('data-cite')))
    return
  }
  const copy = target.closest('[data-copy]')
  if (copy) {
    const block = copy.closest('.codeblock')
    const code = block?.querySelector('code')?.textContent ?? ''
    navigator.clipboard?.writeText(code).then(
      () => ElMessage.success('已复制到剪贴板'),
      () => ElMessage.warning('复制失败,请手动选择文本'),
    )
  }
}

function viewDocument(citation: CitationOut) {
  router.push({ name: 'kb-detail', params: { kbId: citation.kb_id } })
}

function chunkLocation(citation: CitationOut): string {
  const parts: string[] = []
  if (citation.page) parts.push(`P${citation.page}`)
  if (citation.breadcrumb?.length) parts.push(citation.breadcrumb[citation.breadcrumb.length - 1])
  return parts.join(' · ') || '—'
}

onUnmounted(abortStream)
</script>

<template>
  <div class="chat-layout">
    <!-- ═══ 会话列表 260px ═══ -->
    <section class="sess-col">
      <div class="sess-new">
        <el-button type="primary" class="w-full" @click="onNewConversation">
          <el-icon class="btn-ic"><Promotion /></el-icon>新建对话
        </el-button>
      </div>

      <div class="sess-list" v-loading="listLoading">
        <div v-if="listError" class="sess-error">
          <span>{{ listError }}</span>
          <el-button size="small" text @click="loadConversations">重试</el-button>
        </div>

        <template v-else-if="conversations.length">
          <template v-for="group in groupedConversations" :key="group.label">
            <div class="sess-group">{{ group.label }}</div>
            <button
              v-for="conversation in group.items"
              :key="conversation.id"
              class="sess-item"
              :class="{ on: conversation.id === activeConversationId }"
              @click="onPickConversation(conversation)"
            >
              <div class="sess-title">{{ conversation.title || '新会话' }}</div>
              <div class="sess-meta">
                <span>{{ fmtClock(conversation.updated_at ?? conversation.created_at) }}</span>
              </div>
              <span class="sess-del" title="删除会话" @click.stop="onDeleteConversation(conversation)">
                <el-icon :size="12"><Delete /></el-icon>
              </span>
            </button>
          </template>
        </template>

        <el-empty v-else-if="!listLoading" description="还没有会话" :image-size="72" />
      </div>
    </section>

    <!-- ═══ 聊天区 + 引用抽屉 ═══ -->
    <div class="chat-main">
      <div class="chat-column">
        <div class="chat-scroll">
          <!-- 空态 -->
          <div v-if="!messages.length && !streaming" class="chat-empty">
            <div class="empty-icon">
              <el-icon :size="24"><Promotion /></el-icon>
            </div>
            <div class="empty-title">向你的知识库提问</div>
            <div class="empty-desc">
              <template v-if="kbs.length">
                已接入当前空间 {{ kbs.length }} 个知识库,回答自带引用溯源
              </template>
              <template v-else>当前空间还没有知识库,先到「知识库」页上传文档</template>
            </div>
            <div class="chips">
              <button v-for="example in EXAMPLE_QUESTIONS" :key="example" class="chip" @click="useChip(example)">
                {{ example }}
              </button>
            </div>
          </div>

          <!-- 消息列表 -->
          <div v-else class="chat-thread">
            <div class="thread-divider">
              <span class="line"></span><span class="text">对话</span><span class="line"></span>
            </div>

            <div v-for="message in messages" :key="message.id" class="msg-block">
              <!-- 用户消息 -->
              <div v-if="message.role === 'user'" class="msg-user">
                <div class="bubble">{{ message.content }}</div>
              </div>

              <!-- 助手消息 -->
              <div v-else class="msg-assistant">
                <div class="msg-head">
                  {{ message.model_id || '问渠助手' }}
                  <template v-if="message.created_at"> · {{ fmtClock(message.created_at) }}</template>
                </div>

                <!-- 检索工具卡片(对应原型的 Agent 工具调用卡,默认折叠) -->
                <div v-if="message.citations.length" class="tool-card">
                  <button class="tool-head" @click="toggleTool(message.id)">
                    <el-icon class="tool-ic" :size="14"><Promotion /></el-icon>
                    <span class="tool-name">检索知识库</span>
                    <span class="tool-ok"><span class="dot ok"></span>已完成</span>
                    <span class="tool-open">{{ message.citations.length }} 条引用</span>
                    <el-icon class="tool-caret" :class="{ open: expandedTool === message.id }" :size="14">
                      <ArrowDown />
                    </el-icon>
                  </button>
                  <div v-if="expandedTool === message.id" class="tool-body">
                    <div class="tool-line">
                      <span class="tool-key">检索范围</span>
                      <span class="tool-val">{{ scopeSummary(message.citations) }}</span>
                    </div>
                    <div class="tool-line">
                      <span class="tool-key">返回</span>
                      <span class="tool-val">
                        命中 {{ message.citations.length }} 个分块,最高融合分
                        {{ Math.max(...message.citations.map((c) => c.score)).toFixed(3) }}
                      </span>
                    </div>
                    <button class="tool-btn" @click="openCitations(message.citations)">打开引用抽屉</button>
                  </div>
                </div>

                <div
                  v-if="message.stopped"
                  class="miss-notice is-muted"
                >
                  已停止生成,本轮未产生回答。可重新提问。
                </div>
                <div
                  v-else-if="message.content.startsWith('生成失败:')"
                  class="miss-notice is-error"
                >
                  <el-icon :size="14"><WarningFilled /></el-icon>{{ message.content }}
                </div>
                <template v-else>
                  <div
                    v-if="!message.citations.length"
                    class="miss-notice"
                  >
                    <el-icon :size="14"><WarningFilled /></el-icon>知识库中没有找到相关内容,以下回答未引用知识库资料。
                  </div>
                  <div class="md" v-html="assistantHtml(message)" @click="onContentClick($event, message)"></div>
                </template>
              </div>
            </div>

            <!-- 流式中的助手消息 -->
            <div v-if="streaming" class="msg-block">
              <div class="msg-assistant">
                <div class="msg-head">正在生成…</div>
                <div v-if="streamCitations.length" class="tool-card">
                  <div class="tool-head">
                    <el-icon class="tool-ic" :size="14"><Promotion /></el-icon>
                    <span class="tool-name">检索知识库</span>
                    <span class="tool-ok"><span class="dot ok"></span>已完成</span>
                    <button class="tool-open" @click="openCitations(streamCitations)">
                      查看 {{ streamCitations.length }} 条引用
                    </button>
                  </div>
                </div>
                <div v-else-if="!streamText" class="msg-head is-muted">正在检索知识库…</div>
                <div v-if="streamText" class="md">
                  <span v-html="renderMarkdown(streamText, streamCitations.length)"></span>
                  <span class="blink"></span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="chat-input-wrap">
          <div v-if="msgError" class="inline-error">{{ msgError }}</div>
          <div class="chat-input-row">
            <!-- 知识库范围 -->
            <el-popover :width="300" trigger="click" popper-class="kb-scope-pop">
              <template #reference>
                <button class="scope-btn" :title="kbScopeLabel">
                  <el-icon class="scope-ic" :size="16"><Collection /></el-icon>
                  <span class="scope-label truncate">{{ kbScopeLabel }}</span>
                </button>
              </template>
              <div class="scope-head">
                <span class="scope-title">检索范围</span>
                <el-checkbox v-model="allKbSelected" size="small">全选</el-checkbox>
              </div>
              <div class="scope-body">
                <el-checkbox-group v-model="selectedKbIds" class="scope-list">
                  <el-checkbox v-for="kb in kbs" :key="kb.id" :value="kb.id" class="scope-item">
                    <span class="truncate">{{ kb.name }}</span>
                  </el-checkbox>
                </el-checkbox-group>
                <div v-if="!kbs.length" class="scope-empty">当前空间还没有知识库</div>
              </div>
            </el-popover>

            <textarea
              ref="textarea"
              v-model="draft"
              rows="1"
              class="chat-textarea"
              placeholder="向知识库提问…"
              @input="autoGrow"
              @keydown="onKeydown"
            ></textarea>

            <button v-if="!streaming" class="send-btn" :disabled="!canSend" title="发送" @click="onSend">
              <el-icon :size="16"><ArrowUp /></el-icon>
            </button>
            <button v-else class="stop-btn" title="停止生成" @click="onStop">
              <span class="stop-square"></span>停止生成
            </button>
          </div>
        </div>
      </div>

      <!-- 引用抽屉 380px -->
      <aside class="cite-drawer" :class="{ open: drawerOpen }">
        <div class="cite-inner">
          <div class="cite-head">
            <el-icon class="cite-ic" :size="16"><Collection /></el-icon>
            <span class="cite-title">引用来源</span>
            <span class="cite-count">{{ drawerCitations.length }}</span>
            <button class="icon-btn ml-auto" title="关闭" @click="closeDrawer">
              <el-icon><Close /></el-icon>
            </button>
          </div>
          <div class="cite-body">
            <div
              v-for="citation in drawerCitations"
              :key="citation.chunk_id + citation.index"
              class="cite-card"
              :data-cite-card="citation.index"
            >
              <div class="cite-card-head">
                <span class="cite-idx">{{ citation.index }}</span>
                <el-icon class="cite-file-ic" :size="14"><Document /></el-icon>
                <span class="cite-file truncate" :title="citation.filename">{{ citation.filename }}</span>
                <span class="cite-loc">{{ chunkLocation(citation) }}</span>
              </div>
              <div class="cite-excerpt">{{ citation.excerpt }}</div>
              <div class="cite-foot">
                <span class="cite-score">融合分 <b class="mono">{{ citation.score.toFixed(3) }}</b></span>
                <el-button size="small" @click="viewDocument(citation)">查看文档</el-button>
              </div>
            </div>
            <el-empty v-if="!drawerCitations.length" description="暂无引用" :image-size="72" />
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.chat-layout {
  display: flex;
  height: 100%;
  min-height: 0;
}
.btn-ic {
  margin-right: 4px;
}

/* ─── 会话列表 ─── */
.sess-col {
  width: 260px;
  flex-shrink: 0;
  border-right: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.sess-new {
  padding: 12px;
  flex-shrink: 0;
}
.w-full {
  width: 100%;
}
.sess-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 12px;
}
.sess-group {
  padding: 12px 8px 4px;
  font-size: 11px;
  color: #9ca3af;
}
.sess-item {
  position: relative;
  width: 100%;
  padding: 10px 12px;
  border: none;
  border-radius: 8px;
  background: none;
  text-align: left;
  cursor: pointer;
  display: block;
}
.sess-item:hover {
  background: #f7f8fa;
}
.sess-item.on {
  background: #eef1fe;
}
.sess-title {
  font-size: 13px;
  color: #111827;
  line-height: 1.4;
  padding-right: 24px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.sess-meta {
  margin-top: 6px;
  font-size: 11px;
  color: #9ca3af;
}
.sess-del {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  display: none;
  align-items: center;
  justify-content: center;
  color: #9ca3af;
  background: rgba(255, 255, 255, 0.95);
  border: 1px solid #e5e7eb;
}
.sess-item:hover .sess-del {
  display: flex;
}
.sess-del:hover {
  color: #ef4444;
}
.sess-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px;
  font-size: 12px;
  color: #ef4444;
}

/* ─── 聊天区 ─── */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  min-height: 0;
}
.chat-column {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chat-scroll {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.chat-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding-bottom: 64px;
}
.empty-icon {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  display: grid;
  place-items: center;
  color: #9ca3af;
}
.empty-title {
  margin-top: 16px;
  font-size: 16px;
  font-weight: 600;
  color: #111827;
}
.empty-desc {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
.chips {
  display: flex;
  gap: 8px;
  margin-top: 20px;
  flex-wrap: wrap;
  justify-content: center;
}
.chip {
  height: 32px;
  padding: 0 14px;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  background: #fff;
  font-size: 13px;
  color: #6b7280;
  cursor: pointer;
  transition: all 0.15s;
}
.chip:hover {
  border-color: #4f6ef2;
  color: #4f6ef2;
}

.chat-thread {
  max-width: 768px;
  margin: 0 auto;
  padding: 24px;
}
.thread-divider {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
}
.thread-divider .line {
  flex: 1;
  height: 1px;
  background: #e5e7eb;
}
.thread-divider .text {
  font-size: 12px;
  color: #9ca3af;
}
.msg-block {
  margin-bottom: 28px;
}
.msg-user {
  display: flex;
  justify-content: flex-end;
}
.bubble {
  max-width: 80%;
  padding: 10px 16px;
  border-radius: 12px 12px 4px 12px;
  background: #f7f8fa;
  font-size: 14px;
  line-height: 1.6;
  color: #111827;
  white-space: pre-wrap;
}
.msg-head {
  margin-bottom: 8px;
  font-size: 12px;
  color: #9ca3af;
}
.msg-head.is-muted {
  color: #9ca3af;
}
.tool-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  margin-bottom: 6px;
}
.tool-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 40px;
  padding: 0 14px;
  border: none;
  background: none;
  text-align: left;
  cursor: pointer;
}
.tool-ic {
  color: #6b7280;
  flex-shrink: 0;
}
.tool-name {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.tool-ok {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border-radius: 6px;
  background: rgba(16, 185, 129, 0.1);
  color: #10b981;
  font-size: 11px;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.dot.ok {
  background: #10b981;
}
.tool-open {
  margin-left: auto;
  font-size: 12px;
  color: #4f6ef2;
  flex-shrink: 0;
}
.tool-caret {
  color: #9ca3af;
  flex-shrink: 0;
  transition: transform 0.15s;
}
.tool-caret.open {
  transform: rotate(180deg);
}
.tool-body {
  padding: 10px 14px 12px;
  border-top: 1px solid rgba(229, 231, 235, 0.7);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tool-line {
  display: flex;
  gap: 10px;
  font-size: 12px;
  line-height: 1.6;
}
.tool-key {
  width: 56px;
  flex-shrink: 0;
  color: #9ca3af;
}
.tool-val {
  color: #374151;
  min-width: 0;
}
.tool-btn {
  align-self: flex-start;
  margin-top: 2px;
  border: none;
  background: none;
  padding: 0;
  font-size: 12px;
  color: #4f6ef2;
  cursor: pointer;
}
.tool-detail {
  margin-bottom: 8px;
  font-size: 12px;
  color: #9ca3af;
}
.miss-notice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.1);
  color: #b45309;
  font-size: 13px;
  line-height: 1.6;
}
.miss-notice.is-error {
  background: rgba(239, 68, 68, 0.08);
  color: #dc2626;
}
.miss-notice.is-muted {
  background: #f7f8fa;
  color: #6b7280;
}

/* ─── 输入区 ─── */
.chat-input-wrap {
  flex-shrink: 0;
  border-top: 1px solid #e5e7eb;
  background: #fff;
}
.inline-error {
  max-width: 768px;
  margin: 0 auto;
  padding: 8px 24px 0;
  font-size: 12px;
  color: #ef4444;
}
.chat-input-row {
  max-width: 768px;
  margin: 0 auto;
  padding: 14px 24px;
  display: flex;
  align-items: flex-end;
  gap: 10px;
}
.scope-btn {
  height: 36px;
  max-width: 190px;
  padding: 0 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: #374151;
  cursor: pointer;
  flex-shrink: 0;
}
.scope-btn:hover {
  background: #f7f8fa;
}
.scope-ic {
  color: #6b7280;
  flex-shrink: 0;
}
.scope-label {
  min-width: 0;
}
.scope-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 2px 10px;
  border-bottom: 1px solid #e5e7eb;
}
.scope-title {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.scope-body {
  max-height: 240px;
  overflow-y: auto;
  padding-top: 6px;
}
.scope-list {
  display: flex;
  flex-direction: column;
}
.scope-item {
  height: 32px;
  width: 100%;
  margin-right: 0 !important;
}
.scope-empty {
  padding: 8px 4px;
  font-size: 12px;
  color: #9ca3af;
}
.chat-textarea {
  flex: 1;
  min-width: 0;
  max-height: 160px;
  padding: 8px 14px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  outline: none;
  resize: none;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
  color: #111827;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.chat-textarea::placeholder {
  color: #9ca3af;
}
.chat-textarea:focus {
  border-color: #4f6ef2;
  box-shadow: 0 0 0 2px rgba(79, 110, 242, 0.15);
}
.send-btn {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border: none;
  border-radius: 8px;
  background: #4f6ef2;
  color: #fff;
  display: grid;
  place-items: center;
  cursor: pointer;
  transition: background 0.15s;
}
.send-btn:hover:not(:disabled) {
  background: #3e5be0;
}
.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.stop-btn {
  height: 36px;
  padding: 0 14px;
  flex-shrink: 0;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  font-size: 13px;
  color: #374151;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.stop-btn:hover {
  background: #f7f8fa;
}
.stop-square {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: #ef4444;
}

/* ─── 引用抽屉 ─── */
.cite-drawer {
  width: 0;
  flex-shrink: 0;
  overflow: hidden;
  border-left: 1px solid #e5e7eb;
  transition: width 0.2s;
  background: #fff;
}
.cite-drawer.open {
  width: 380px;
}
.cite-inner {
  width: 380px;
  height: 100%;
  display: flex;
  flex-direction: column;
}
.cite-head {
  height: 56px;
  flex-shrink: 0;
  padding: 0 16px;
  border-bottom: 1px solid #e5e7eb;
  display: flex;
  align-items: center;
  gap: 8px;
}
.cite-ic {
  color: #6b7280;
}
.cite-title {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}
.cite-count {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  background: #eef1fe;
  color: #4f6ef2;
  font-size: 11px;
}
.icon-btn {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 8px;
  background: none;
  display: grid;
  place-items: center;
  color: #6b7280;
  cursor: pointer;
}
.icon-btn:hover {
  background: #f7f8fa;
  color: #111827;
}
.cite-body {
  flex: 1;
  overflow-y: auto;
}
.cite-card {
  padding: 16px;
  border-bottom: 1px solid rgba(229, 231, 235, 0.8);
}
.cite-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cite-idx {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #4f6ef2;
  color: #fff;
  font-size: 10px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.cite-file-ic {
  color: #4f6ef2;
  flex-shrink: 0;
}
.cite-file {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.cite-loc {
  font-size: 12px;
  color: #9ca3af;
  flex-shrink: 0;
}
.cite-excerpt {
  margin-top: 10px;
  padding: 12px;
  border: 1px solid rgba(229, 231, 235, 0.7);
  border-radius: 8px;
  background: #f7f8fa;
  font-size: 13px;
  line-height: 1.7;
  color: #374151;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.cite-foot {
  margin-top: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.cite-score {
  font-size: 12px;
  color: #9ca3af;
}
.cite-score b {
  color: #374151;
  font-weight: 500;
}
</style>
