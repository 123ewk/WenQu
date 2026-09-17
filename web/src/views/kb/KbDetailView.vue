<script setup lang="ts">
import { ArrowDown, Document, RefreshRight, Search, Upload, WarningFilled } from '@element-plus/icons-vue'
import type { UploadFile, UploadInstance } from 'element-plus'

import { ApiError, errMessage } from '@/api/http'
import {
  apiDeleteDocument,
  apiGetKb,
  apiListDocuments,
  apiUpdateKb,
  apiUploadDocument,
} from '@/api/knowledge'
import { apiRetrievalSearch } from '@/api/retrieval'
import type { DocumentOut, KnowledgeBaseOut, RetrievedChunkOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useUploadStore } from '@/stores/uploads'
import { fmtBytes, fmtStamp } from '@/utils/format'
import { failureReason, INGEST_STEPS, isTerminal, stepState, statusLabel, UPLOAD_ACCEPT } from '@/utils/ingest'
import { isAtLeast, ROLE } from '@/utils/roles'

/**
 * 知识库详情(原型 04,三个 Tab)。
 * Tab A 文档管理:上传 multipart + 状态机轮询(pending→parsing→chunking→embedding→completed/failed)。
 * Tab B 检索测试:POST /retrieval/search,展示 RRF 融合分与双路命中排名。
 * Tab C 设置:改名称/描述(PATCH);接口不支持改嵌入模型,故无"重建索引"流程。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const uploads = useUploadStore()

const kbId = computed(() => String(route.params.kbId ?? ''))
const spaceId = computed(() => auth.currentSpaceId ?? '')

const kb = ref<KnowledgeBaseOut | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const tab = ref<'docs' | 'search' | 'settings'>('docs')

const canEdit = computed(() => isAtLeast(auth.role, ROLE.EDITOR))
const canDelete = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

/* ───── 文档 ───── */

const PAGE_SIZE = 20
const docs = ref<DocumentOut[]>([])
const docTotal = ref(0)
const docPage = ref(1)
const docsLoading = ref(false)
const docsError = ref<string | null>(null)
const expandedFail = ref<string | null>(null)
const uploading = ref(false)
const uploadRef = ref<UploadInstance>()

let pollTimer: ReturnType<typeof setInterval> | null = null

const hasRunning = computed(() => docs.value.some((d) => !isTerminal(d.status)))

watch([spaceId, kbId], () => reload(), { immediate: true })

// 有未完成文档时轮询(页面不可见时暂停),全部结束即停
watch(
  [hasRunning, tab],
  () => {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    if (hasRunning.value && tab.value === 'docs' && !document.hidden) {
      pollTimer = setInterval(() => void loadDocs(true), 2000)
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

async function reload() {
  if (!spaceId.value || !kbId.value) return
  loading.value = true
  loadError.value = null
  try {
    kb.value = await apiGetKb(spaceId.value, kbId.value)
    docPage.value = 1
    await loadDocs()
  } catch (e) {
    kb.value = null
    docs.value = []
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function loadDocs(silent = false) {
  if (!spaceId.value || !kbId.value) return
  if (!silent) docsLoading.value = true
  docsError.value = null
  try {
    const data = await apiListDocuments(spaceId.value, kbId.value, PAGE_SIZE, (docPage.value - 1) * PAGE_SIZE)
    docs.value = data.items
    docTotal.value = data.total
  } catch (e) {
    if (!silent) {
      docsError.value = errMessage(e)
      docs.value = []
    }
  } finally {
    if (!silent) docsLoading.value = false
  }
}

function onPageChange(p: number) {
  docPage.value = p
  loadDocs()
}

/** 上传:接口只存文件并入队,返回 pending;进度用浏览器上传字节进度 */
async function uploadOne(file: File) {
  const taskId = `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const taskKey = uploads.add({
    id: taskId,
    filename: file.name,
    sizeBytes: file.size,
    kbId: kbId.value,
    kbName: kb.value?.name ?? '知识库',
  })
  try {
    await apiUploadDocument(spaceId.value, kbId.value, file, (percent) => {
      uploads.patch(taskKey, { percent })
    })
    uploads.patch(taskKey, { percent: 100, phase: 'queued' })
    ElMessage.success(`「${file.name}」已上传,正在后台解析入库`)
  } catch (e) {
    const message = errMessage(e)
    uploads.patch(taskKey, { phase: 'error', error: message })
    if (e instanceof ApiError && e.code === 'UNSUPPORTED_FORMAT') {
      ElMessage.error('不支持该文件格式,请另存为 pdf/docx/xlsx/pptx/md/txt 后重试')
    } else if (e instanceof ApiError && e.code === 'FILE_TOO_LARGE') {
      ElMessage.error(message)
    } else {
      ElMessage.error(message)
    }
  }
}

async function onUploadChange(uploadFile: UploadFile) {
  if (!uploadFile.raw) return
  uploading.value = true
  try {
    await uploadOne(uploadFile.raw)
  } finally {
    uploading.value = false
    uploadRef.value?.clearFiles()
    docPage.value = 1
    await loadDocs()
  }
}

async function onDeleteDoc(doc: DocumentOut) {
  const confirmed = await ElMessageBox.confirm(
    `删除文档「${doc.filename}」?其原始文件、文本分块与向量索引将一并删除,不可恢复。`,
    '删除文档',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteDocument(spaceId.value, kbId.value, doc.id)
    ElMessage.success('文档已删除')
    await loadDocs()
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

/** 整页拖入上传(原型 04 的「松开即上传」遮罩) */
const dragDepth = ref(0)
const dragging = computed(() => dragDepth.value > 0 && canEdit.value)

function onDragEnter() {
  if (canEdit.value) dragDepth.value += 1
}
function onDragLeave() {
  dragDepth.value = Math.max(0, dragDepth.value - 1)
}
async function onDrop(event: DragEvent) {
  dragDepth.value = 0
  if (!canEdit.value) return
  const files = Array.from(event.dataTransfer?.files ?? [])
  if (!files.length) return
  tab.value = 'docs'
  uploading.value = true
  try {
    for (const file of files) await uploadOne(file)
  } finally {
    uploading.value = false
    docPage.value = 1
    await loadDocs()
  }
}

/* ───── 检索测试 ───── */

const query = ref('')
const topK = ref(8)
const searching = ref(false)
const searchError = ref<string | null>(null)
const results = ref<RetrievedChunkOut[]>([])
const searched = ref(false)
const expanded = ref<string | null>(null)

async function onSearch() {
  const q = query.value.trim()
  if (!q) {
    ElMessage.warning('请输入检索内容')
    return
  }
  searching.value = true
  searchError.value = null
  try {
    results.value = await apiRetrievalSearch(spaceId.value, { query: q, top_k: topK.value, kb_ids: [kbId.value] })
    searched.value = true
    expanded.value = null
  } catch (e) {
    searchError.value = errMessage(e)
    results.value = []
    searched.value = true
  } finally {
    searching.value = false
  }
}

function rankText(item: RetrievedChunkOut): string {
  const parts: string[] = []
  if (item.vector_rank !== null) parts.push(`向量 #${item.vector_rank}`)
  if (item.fulltext_rank !== null) parts.push(`关键词 #${item.fulltext_rank}`)
  return parts.length ? parts.join(' · ') : '未记录来源排名'
}

function chunkLocation(item: RetrievedChunkOut): string {
  const page = item.meta?.page
  const crumbs = item.meta?.breadcrumb ?? []
  const parts = [item.filename]
  if (page) parts.push(`P${page}`)
  if (crumbs.length) parts.push(crumbs[crumbs.length - 1])
  return parts.join(' · ')
}

/* ───── 设置 ───── */

const settingName = ref('')
const settingDesc = ref('')
const savingSetting = ref(false)

watch(kb, (v) => {
  settingName.value = v?.name ?? ''
  settingDesc.value = v?.description ?? ''
})

async function onSaveSetting() {
  if (!settingName.value.trim()) {
    ElMessage.warning('知识库名称不能为空')
    return
  }
  savingSetting.value = true
  try {
    kb.value = await apiUpdateKb(spaceId.value, kbId.value, {
      name: settingName.value.trim(),
      description: settingDesc.value.trim(),
    })
    ElMessage.success('设置已保存')
  } catch (e) {
    ElMessage.error(errMessage(e))
  } finally {
    savingSetting.value = false
  }
}

function openChunks(doc: DocumentOut) {
  router.push({ name: 'kb-chunks', params: { kbId: kbId.value, docId: doc.id } })
}
</script>

<template>
  <div
    class="page-pad"
    v-loading="loading"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
  >
    <!-- 加载失败 -->
    <div v-if="loadError" class="state-wrap">
      <el-icon class="state-ic"><WarningFilled /></el-icon>
      <div class="state-title">知识库加载失败</div>
      <div class="state-desc">{{ loadError }}</div>
      <div class="state-btns">
        <el-button @click="reload">
          <el-icon class="btn-ic"><RefreshRight /></el-icon>重试
        </el-button>
        <el-button @click="router.push('/kb')">返回知识库列表</el-button>
      </div>
    </div>

    <template v-else-if="kb">
      <!-- 页头:名称 + 模型信息 -->
      <div class="kb-head">
        <div class="page-title">{{ kb.name }}</div>
        <el-tooltip v-if="canEdit" content="在「设置」Tab 中修改名称与描述" placement="top">
          <span class="head-info">可重命名</span>
        </el-tooltip>
      </div>
      <div class="kb-meta">
        <span>嵌入模型 <b>{{ kb.embedding_model }}</b> · {{ kb.embedding_dim }} 维</span>
        <span class="divider"></span>
        <span>文档 <b>{{ docTotal }}</b></span>
        <span class="divider"></span>
        <span>创建于 <b>{{ fmtStamp(kb.created_at) }}</b></span>
      </div>

      <el-tabs v-model="tab" class="kb-tabs">
        <!-- ═══ Tab A 文档管理 ═══ -->
        <el-tab-pane label="文档管理" name="docs">
          <div class="toolbar">
            <div class="toolbar-hint">
              支持 PDF / Word / Excel / PPT / Markdown / TXT,旧版 .doc/.xls/.ppt 请另存为新格式;整页拖入即可上传
            </div>
            <el-upload
              v-if="canEdit"
              ref="uploadRef"
              :auto-upload="false"
              :show-file-list="false"
              :accept="UPLOAD_ACCEPT"
              multiple
              @change="onUploadChange"
            >
              <el-button type="primary" :loading="uploading">
                <el-icon class="btn-ic"><Upload /></el-icon>上传文档
              </el-button>
            </el-upload>
          </div>

          <div v-if="docsError" class="inline-error">
            <span>文档列表加载失败:{{ docsError }}</span>
            <el-button size="small" @click="loadDocs()">重试</el-button>
          </div>

          <div class="card table-card">
            <el-table :data="docs" v-loading="docsLoading" style="width: 100%">
              <el-table-column label="文件名" min-width="240">
                <template #default="{ row }">
                  <div class="file-cell">
                    <el-icon class="file-ic"><Document /></el-icon>
                    <span class="file-name truncate" :title="row.filename">{{ row.filename }}</span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="大小" width="100">
                <template #default="{ row }">
                  <span class="cell-dim">{{ fmtBytes(row.size_bytes) }}</span>
                </template>
              </el-table-column>

              <el-table-column label="状态" min-width="320">
                <template #default="{ row }">
                  <div class="status-cell">
                    <!-- 失败:可展开查看原因 -->
                    <template v-if="row.status === 'failed'">
                      <div class="fail-wrap">
                        <button class="fail-btn" @click="expandedFail = expandedFail === row.id ? null : row.id">
                          <span class="fail-badge">失败</span>
                          <el-icon class="caret" :class="{ open: expandedFail === row.id }">
                            <ArrowDown />
                          </el-icon>
                        </button>
                        <div v-if="expandedFail === row.id" class="fail-body">
                          <el-icon class="fail-ic"><WarningFilled /></el-icon>
                          <div>
                            <div class="fail-text">{{ failureReason(row.error_code) }}</div>
                            <div class="fail-hint">契约未提供「重新解析」接口,请删除后重新上传该文件。</div>
                          </div>
                        </div>
                      </div>
                    </template>
                    <!-- 正常:步进条 -->
                    <template v-else>
                      <span class="steps" :title="INGEST_STEPS.join(' → ')">
                        <template v-for="(label, i) in INGEST_STEPS" :key="label">
                          <span
                            class="stg"
                            :class="{
                              'stg-done': stepState(row.status, i) === 'done',
                              'stg-cur': stepState(row.status, i) === 'current',
                            }"
                          ></span>
                          <span
                            v-if="i < INGEST_STEPS.length - 1"
                            class="stg-line"
                            :class="{ 'stg-line-done': stepState(row.status, i) === 'done' }"
                          ></span>
                        </template>
                      </span>
                      <span class="status-text" :class="{ ok: row.status === 'completed' }">
                        {{ statusLabel(row.status) }}
                      </span>
                    </template>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="上传时间" width="150">
                <template #default="{ row }">
                  <span class="cell-dim">{{ fmtStamp(row.created_at) }}</span>
                </template>
              </el-table-column>

              <el-table-column label="操作" width="180" align="right">
                <template #default="{ row }">
                  <div class="row-ops">
                    <el-link
                      v-if="row.status === 'completed'"
                      type="primary"
                      :underline="false"
                      @click="openChunks(row as DocumentOut)"
                    >
                      预览分块
                    </el-link>
                    <span v-else class="cell-none">—</span>
                    <el-link
                      v-if="canEdit"
                      type="danger"
                      :underline="false"
                      @click="onDeleteDoc(row as DocumentOut)"
                    >
                      删除
                    </el-link>
                  </div>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="docTotal > PAGE_SIZE" class="pager">
              <span class="pager-total">共 {{ docTotal }} 个文档</span>
              <el-pagination
                layout="prev, pager, next"
                :total="docTotal"
                :page-size="PAGE_SIZE"
                :current-page="docPage"
                @current-change="onPageChange"
              />
            </div>
            <el-empty v-else-if="!docsLoading && docs.length === 0" description="还没有文档,上传后即可检索与问答" />
          </div>
        </el-tab-pane>

        <!-- ═══ Tab B 检索测试 ═══ -->
        <el-tab-pane label="检索测试" name="search">
          <div class="search-row">
            <el-input
              v-model="query"
              size="large"
              placeholder="输入一句话,测试该知识库的召回效果"
              @keyup.enter="onSearch"
            />
            <el-button type="primary" size="large" :loading="searching" @click="onSearch">
              <el-icon class="btn-ic"><Search /></el-icon>检索
            </el-button>
          </div>

          <div class="search-meta">
            混合检索:向量 ∥ 全文双路召回 + RRF 融合 · topK
            <el-input-number v-model="topK" :min="1" :max="50" size="small" controls-position="right" class="topk-input" />
            · 仅检索当前知识库
          </div>

          <div v-if="searchError" class="inline-error">
            <span>检索失败:{{ searchError }}</span>
          </div>

          <div v-if="results.length" class="result-list">
            <div v-for="item in results" :key="item.chunk_id" class="card result-card">
              <div class="result-main">
                <div class="result-content">{{ item.content }}</div>
                <button class="expand-btn" @click="expanded = expanded === item.chunk_id ? null : item.chunk_id">
                  {{ expanded === item.chunk_id ? '收起得分明细' : '展开得分明细' }}
                </button>
              </div>
              <div class="result-score">
                <div class="score-value mono">{{ item.score.toFixed(3) }}</div>
                <div class="score-source">{{ chunkLocation(item) }}</div>
              </div>
              <div v-if="expanded === item.chunk_id" class="score-body">
                <div class="score-row">
                  <span>双路命中来源</span>
                  <span class="mono">{{ rankText(item) }}</span>
                </div>
                <div class="rank-bars">
                  <div class="rank-item">
                    <span class="rank-label">向量召回</span>
                    <el-progress
                      :percentage="item.vector_rank ? Math.max(4, 100 - (item.vector_rank - 1) * 12) : 0"
                      :stroke-width="6"
                      :show-text="false"
                      :color="item.vector_rank ? '#4F6EF2' : '#E5E7EB'"
                    />
                    <span class="rank-text mono">{{ item.vector_rank ? `#${item.vector_rank}` : '未命中' }}</span>
                  </div>
                  <div class="rank-item">
                    <span class="rank-label">关键词召回</span>
                    <el-progress
                      :percentage="item.fulltext_rank ? Math.max(4, 100 - (item.fulltext_rank - 1) * 12) : 0"
                      :stroke-width="6"
                      :show-text="false"
                      :color="item.fulltext_rank ? '#6B7280' : '#E5E7EB'"
                    />
                    <span class="rank-text mono">{{ item.fulltext_rank ? `#${item.fulltext_rank}` : '未命中' }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <el-empty
            v-else-if="searched && !searchError"
            description="没有召回到相关分块,换个说法或先上传相关文档"
          />
          <div v-else-if="!searched" class="search-placeholder">
            输入关键词后点击「检索」,将展示融合分与双路命中排名。原型中的向量权重 / 阈值 / Rerank
            开关在契约中不存在,未渲染。
          </div>
        </el-tab-pane>

        <!-- ═══ Tab C 设置(Editor+ 可见) ═══ -->
        <el-tab-pane v-if="canEdit" label="设置" name="settings">
          <div class="settings-wrap">
            <div class="card block-card">
              <div class="block-title">基本信息</div>
              <el-form label-position="top" class="block-form">
                <el-form-item label="名称">
                  <el-input v-model="settingName" maxlength="64" />
                </el-form-item>
                <el-form-item label="描述">
                  <el-input v-model="settingDesc" type="textarea" :rows="2" maxlength="200" />
                </el-form-item>
              </el-form>
              <el-button type="primary" :loading="savingSetting" @click="onSaveSetting">保存</el-button>
            </div>

            <div class="card block-card">
              <div class="block-title">嵌入模型</div>
              <div class="block-value">{{ kb.embedding_model }} · {{ kb.embedding_dim }} 维</div>
              <div class="block-note">
                嵌入模型由服务端配置决定;契约未提供可选模型列表与"重建索引"接口,故本页不提供更换与重建入口。
              </div>
            </div>

            <div v-if="canDelete" class="card block-card">
              <div class="block-title">危险操作</div>
              <div class="danger-row">
                <div>
                  <div class="danger-name">删除知识库</div>
                  <div class="danger-desc">将级联删除全部文档、分块与向量索引,不可恢复</div>
                </div>
                <el-button type="danger" @click="router.push('/kb')">去列表删除</el-button>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>

  <!-- 拖拽遮罩:松开即上传到当前知识库 -->
  <Teleport to="body">
    <div v-if="dragging" class="drag-mask">
      <div class="drag-frame">
        <el-icon class="drag-ic" :size="32"><Upload /></el-icon>
        <div class="drag-title">松开即上传</div>
        <div class="drag-desc">支持多文件,将上传至「{{ kb?.name }}」</div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.btn-ic {
  margin-right: 4px;
}
.state-btns {
  display: flex;
  gap: 8px;
  margin-top: 20px;
}
.kb-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.head-info {
  font-size: 12px;
  color: #9ca3af;
}
.kb-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  font-size: 13px;
  color: #6b7280;
}
.kb-meta b {
  color: #111827;
  font-weight: 500;
}
.divider {
  width: 1px;
  height: 12px;
  background: #e5e7eb;
}
.kb-tabs {
  margin-top: 20px;
}
.kb-tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 16px 0 12px;
}
.toolbar-hint {
  font-size: 12px;
  color: #9ca3af;
}
.inline-error {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 13px;
  color: #ef4444;
}

.table-card {
  overflow: hidden;
}
.file-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.file-ic {
  color: #6b7280;
  flex-shrink: 0;
}
.file-name {
  color: #111827;
}
.cell-dim {
  font-size: 13px;
  color: #9ca3af;
}
.cell-none {
  font-size: 13px;
  color: #9ca3af;
}
.row-ops {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}

.status-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.steps {
  display: inline-flex;
  align-items: center;
}
.stg {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #e5e7eb;
  flex-shrink: 0;
}
.stg-done {
  background: #10b981;
}
.stg-cur {
  background: #4f6ef2;
  box-shadow: 0 0 0 2px rgba(79, 110, 242, 0.15);
}
.stg-line {
  width: 12px;
  height: 1px;
  background: #e5e7eb;
  flex-shrink: 0;
}
.stg-line-done {
  background: #10b981;
}
.status-text {
  font-size: 12px;
  color: #374151;
  white-space: nowrap;
}
.status-text.ok {
  color: #10b981;
}
.fail-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: none;
  padding: 0;
  cursor: pointer;
}
.fail-badge {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
  font-size: 11px;
}
.caret {
  color: #9ca3af;
  font-size: 12px;
  transition: transform 0.15s;
}
.caret.open {
  transform: rotate(180deg);
}
.fail-body {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-top: 10px;
  padding: 12px 16px;
  border-radius: 8px;
  background: #f7f8fa;
}
.fail-ic {
  color: #ef4444;
  margin-top: 2px;
  flex-shrink: 0;
}
.fail-text {
  font-size: 13px;
  color: #374151;
  line-height: 1.7;
}
.fail-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #9ca3af;
}
.fail-wrap {
  min-width: 0;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
  padding: 0 16px;
  border-top: 1px solid #e5e7eb;
}
.pager-total {
  font-size: 13px;
  color: #9ca3af;
}

.search-row {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}
.search-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  font-size: 12px;
  color: #9ca3af;
}
.topk-input {
  width: 100px;
}
.search-placeholder {
  margin-top: 16px;
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
  color: #9ca3af;
  line-height: 1.8;
}
.result-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 16px;
}
.result-card {
  padding: 16px;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 16px;
}
.result-main {
  min-width: 0;
}
.result-content {
  font-size: 13px;
  line-height: 1.7;
  color: #374151;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.expand-btn {
  margin-top: 8px;
  border: none;
  background: none;
  padding: 0;
  font-size: 12px;
  color: #4f6ef2;
  cursor: pointer;
}
.result-score {
  text-align: right;
  flex-shrink: 0;
}
.score-value {
  font-size: 20px;
  font-weight: 600;
  color: #4f6ef2;
  line-height: 1;
}
.score-source {
  margin-top: 6px;
  font-size: 12px;
  color: #9ca3af;
  max-width: 220px;
}
.score-body {
  grid-column: 1 / -1;
  padding-top: 12px;
  border-top: 1px solid rgba(229, 231, 235, 0.7);
}
.score-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: #6b7280;
}
.rank-bars {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 10px;
}
.rank-item {
  display: grid;
  grid-template-columns: 80px 1fr 56px;
  align-items: center;
  gap: 10px;
}
.rank-label {
  font-size: 12px;
  color: #6b7280;
}
.rank-text {
  font-size: 12px;
  color: #9ca3af;
  text-align: right;
}

.settings-wrap {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding-top: 16px;
}
.block-card {
  padding: 24px;
}
.block-title {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}
.block-form {
  margin-top: 12px;
}
.block-value {
  margin-top: 10px;
  font-size: 13px;
  color: #111827;
}
.block-note {
  margin-top: 6px;
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.7;
}
.danger-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 16px;
}
.danger-name {
  font-size: 13px;
  font-weight: 500;
  color: #374151;
}
.danger-desc {
  margin-top: 2px;
  font-size: 12px;
  color: #9ca3af;
}
</style>

<style>
.drag-mask {
  position: fixed;
  inset: 0;
  z-index: 55;
  background: rgba(238, 241, 254, 0.92);
}
.drag-frame {
  position: absolute;
  inset: 16px;
  border: 2px dashed #4f6ef2;
  border-radius: 16px;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 4px;
}
.drag-ic {
  color: #4f6ef2;
}
.drag-title {
  margin-top: 12px;
  font-size: 16px;
  font-weight: 600;
  color: #4f6ef2;
}
.drag-desc {
  font-size: 13px;
  color: rgba(79, 110, 242, 0.8);
}
</style>
