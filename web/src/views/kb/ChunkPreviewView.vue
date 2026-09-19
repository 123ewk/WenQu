<script setup lang="ts">
import { Close, Document, RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { errMessage } from '@/api/http'
import { apiGetDocument, apiListChunks } from '@/api/knowledge'
import type { ChunkOut, DocumentOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtStamp } from '@/utils/format'
import { isAtLeast, ROLE } from '@/utils/roles'

/**
 * 分块预览(原型 05):960px 右侧全高抽屉,左 40% 分块列表 / 右 60% 选中分块详情。
 * 数据来自 GET .../documents/{doc_id}/chunks(分页,50/页),元数据取 meta.breadcrumb/page/kind。
 * 后端 kind 只有 text/table,原型里的"标题"类型不存在,按 text 展示。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const spaceId = computed(() => auth.currentSpaceId ?? '')
const kbId = computed(() => String(route.params.kbId ?? ''))
const docId = computed(() => String(route.params.docId ?? ''))
const canEdit = computed(() => isAtLeast(auth.role, ROLE.EDITOR))

const PAGE_SIZE = 50

const doc = ref<DocumentOut | null>(null)
const chunks = ref<ChunkOut[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref<string | null>(null)
const selectedId = ref<string | null>(null)

const selected = computed(() => chunks.value.find((c) => c.id === selectedId.value) ?? null)

watch([spaceId, kbId, docId], () => load(), { immediate: true })

async function load() {
  if (!spaceId.value || !kbId.value || !docId.value) return
  loading.value = true
  error.value = null
  try {
    doc.value = await apiGetDocument(spaceId.value, kbId.value, docId.value)
    await loadChunks()
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function loadChunks() {
  const data = await apiListChunks(
    spaceId.value,
    kbId.value,
    docId.value,
    PAGE_SIZE,
    (page.value - 1) * PAGE_SIZE,
  )
  chunks.value = data.items
  total.value = data.total
  selectedId.value = data.items[0]?.id ?? null
}

async function onPageChange(p: number) {
  page.value = p
  loading.value = true
  try {
    await loadChunks()
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

function chunkIndex(chunk: ChunkOut): string {
  return `#${String(chunk.seq + 1).padStart(4, '0')}`
}

function kindLabel(kind: string | undefined): string {
  return kind === 'table' ? '表格' : '段落'
}

/** tokens 优先(入库时算好,可 null);null 回退字符数 */
function tokensLabel(chunk: ChunkOut): string {
  return chunk.tokens != null ? `${chunk.tokens} tokens` : `${chunk.content.length} 字`
}

function preview(content: string): string {
  const flat = content.replace(/\s+/g, ' ').trim()
  return flat.length > 64 ? `${flat.slice(0, 64)}…` : flat
}

function close() {
  router.push({ name: 'kb-detail', params: { kbId: kbId.value } })
}

/** 契约未提供重新解析/重试接口,按文档说明提示用户重新上传 */
function onReparse() {
  ElMessage.info('契约未提供重新解析接口,请删除该文档后重新上传')
}
</script>

<template>
  <!-- 抽屉:覆盖在知识库详情之上 -->
  <div class="drawer-mask" @click="close"></div>
  <aside class="drawer" @click.stop>
    <!-- 头部 -->
    <div class="drawer-head">
      <el-icon class="head-ic"><Document /></el-icon>
      <span class="head-name truncate" :title="doc?.filename">{{ doc?.filename ?? '分块预览' }}</span>
      <span class="head-sep"></span>
      <span class="head-policy">分块策略:512 token / 重叠 15%</span>
      <div class="head-ops">
        <el-button v-if="canEdit" size="small" @click="onReparse">
          <el-icon class="btn-ic"><RefreshRight /></el-icon>重新解析
        </el-button>
        <button class="icon-btn" title="关闭" @click="close">
          <el-icon><Close /></el-icon>
        </button>
      </div>
    </div>

    <div v-if="error" class="state-wrap">
      <el-icon class="state-ic"><WarningFilled /></el-icon>
      <div class="state-title">分块加载失败</div>
      <div class="state-desc">{{ error }}</div>
      <el-button class="state-btn" @click="load">重试</el-button>
    </div>

    <div v-else class="drawer-body" v-loading="loading">
      <!-- 左 40%:分块列表 -->
      <div class="chunk-col">
        <div class="chunk-col-head">
          <span class="chunk-total">共 <b>{{ total }}</b> 个分块</span>
          <div class="kind-legend">
            <span class="kind kind-text">段落</span>
            <span class="kind kind-table">表格</span>
          </div>
        </div>
        <div class="chunk-list">
          <button
            v-for="chunk in chunks"
            :key="chunk.id"
            class="chunk-item"
            :class="{ on: chunk.id === selectedId }"
            @click="selectedId = chunk.id"
          >
            <div class="chunk-item-top">
              <span class="chunk-no mono">{{ chunkIndex(chunk) }}</span>
              <span class="kind" :class="chunk.meta?.kind === 'table' ? 'kind-table' : 'kind-text'">
                {{ kindLabel(chunk.meta?.kind) }}
              </span>
              <span class="chunk-tokens mono">{{ tokensLabel(chunk) }}</span>
            </div>
            <div class="chunk-preview">{{ preview(chunk.content) }}</div>
          </button>
          <el-empty v-if="!loading && chunks.length === 0" description="该文档暂无分块(可能仍在入库中)" />
        </div>
        <div v-if="total > PAGE_SIZE" class="chunk-pager">
          <el-pagination
            layout="prev, pager, next"
            small
            :total="total"
            :page-size="PAGE_SIZE"
            :current-page="page"
            @current-change="onPageChange"
          />
        </div>
      </div>

      <!-- 右 60%:选中分块详情 -->
      <div class="detail-col">
        <div class="detail-head">
          <span class="detail-title">分块详情</span>
          <span v-if="selected" class="detail-no mono">{{ chunkIndex(selected) }}</span>
        </div>
        <div class="detail-body">
          <template v-if="selected">
            <div class="meta-grid">
              <div>
                <div class="meta-label">页码</div>
                <div class="meta-value">{{ selected.meta?.page ? `P${selected.meta.page}` : '—' }}</div>
              </div>
              <div class="meta-span">
                <div class="meta-label">所属标题路径</div>
                <div class="meta-value truncate" :title="(selected.meta?.breadcrumb ?? []).join(' > ')">
                  {{ (selected.meta?.breadcrumb ?? []).join(' > ') || '—' }}
                </div>
              </div>
              <div>
                <div class="meta-label">字符数 / 类型</div>
                <div class="meta-value">
                  <template v-if="selected.tokens != null" class="mono">{{ selected.tokens }} tokens · </template>{{ selected.content.length }} 字 · {{ kindLabel(selected.meta?.kind) }}
                </div>
              </div>
            </div>
            <div class="detail-content">{{ selected.content }}</div>
            <div v-if="doc" class="detail-foot">来源文档:{{ doc.filename }} · 上传于 {{ fmtStamp(doc.created_at) }}</div>
          </template>
          <el-empty v-else description="从左侧选择一个分块查看详情" />
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.drawer-mask {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(17, 24, 39, 0.25);
}
.drawer {
  position: fixed;
  top: 0;
  right: 0;
  z-index: 45;
  width: 960px;
  max-width: 100vw;
  height: 100%;
  background: #fff;
  border-left: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
}
.drawer-head {
  height: 56px;
  flex-shrink: 0;
  padding: 0 20px;
  border-bottom: 1px solid #e5e7eb;
  display: flex;
  align-items: center;
  gap: 12px;
}
.head-ic {
  color: #6b7280;
  flex-shrink: 0;
}
.head-name {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
  max-width: 320px;
}
.head-sep {
  width: 1px;
  height: 16px;
  background: #e5e7eb;
}
.head-policy {
  font-size: 12px;
  color: #9ca3af;
  flex-shrink: 0;
}
.head-ops {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 4px;
}
.btn-ic {
  margin-right: 4px;
}
.icon-btn {
  width: 32px;
  height: 32px;
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

.drawer-body {
  flex: 1;
  min-height: 0;
  display: flex;
}
.chunk-col {
  width: 40%;
  flex-shrink: 0;
  border-right: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
}
.chunk-col-head {
  flex-shrink: 0;
  padding: 10px 16px;
  border-bottom: 1px solid #e5e7eb;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.chunk-total {
  font-size: 12px;
  color: #9ca3af;
}
.chunk-total b {
  color: #374151;
  font-weight: 500;
}
.kind-legend {
  display: flex;
  gap: 6px;
}
.kind {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 16px;
  white-space: nowrap;
}
.kind-text {
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  color: #6b7280;
}
.kind-table {
  background: rgba(245, 158, 11, 0.1);
  color: #b45309;
}
.chunk-list {
  flex: 1;
  overflow-y: auto;
}
.chunk-item {
  width: 100%;
  height: 64px;
  padding: 0 16px;
  border: none;
  border-bottom: 1px solid rgba(229, 231, 235, 0.7);
  border-left: 2px solid transparent;
  background: none;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 6px;
  text-align: left;
  cursor: pointer;
}
.chunk-item:hover {
  background: #f7f8fa;
}
.chunk-item.on {
  background: #eef1fe;
  border-left-color: #4f6ef2;
}
.chunk-item-top {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chunk-no {
  width: 36px;
  font-size: 11px;
  color: #9ca3af;
  flex-shrink: 0;
}
.chunk-tokens {
  margin-left: auto;
  font-size: 11px;
  color: #9ca3af;
  flex-shrink: 0;
}
.chunk-preview {
  padding-left: 44px;
  font-size: 12px;
  color: #6b7280;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.chunk-pager {
  flex-shrink: 0;
  border-top: 1px solid #e5e7eb;
  display: flex;
  justify-content: center;
  padding: 6px 0;
}

.detail-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.detail-head {
  flex-shrink: 0;
  padding: 12px 20px;
  border-bottom: 1px solid #e5e7eb;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.detail-title {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.detail-no {
  font-size: 12px;
  color: #9ca3af;
}
.detail-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}
.meta-grid {
  display: grid;
  grid-template-columns: 80px 1fr 140px;
  gap: 12px;
  padding: 12px 16px;
  border-radius: 8px;
  background: #f7f8fa;
}
.meta-label {
  font-size: 11px;
  color: #9ca3af;
}
.meta-value {
  margin-top: 2px;
  font-size: 13px;
  color: #111827;
}
.meta-span {
  min-width: 0;
}
.detail-content {
  margin-top: 16px;
  font-size: 14px;
  line-height: 1.8;
  color: #374151;
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-foot {
  margin-top: 20px;
  padding-top: 12px;
  border-top: 1px solid #e5e7eb;
  font-size: 12px;
  color: #9ca3af;
}
</style>
