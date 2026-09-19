<script setup lang="ts">
import { Box, Collection, Delete, Edit, MoreFilled, Plus, RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { ApiError, errMessage } from '@/api/http'
import { apiDeleteKb, apiListKbs } from '@/api/knowledge'
import type { KnowledgeBaseOut } from '@/api/types'
import KbFormDialog from '@/components/KbFormDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { fmtBytes, fmtRelative } from '@/utils/format'
import { isAtLeast, ROLE } from '@/utils/roles'

/**
 * 知识库列表(原型 03):卡片网格 + 新建/重命名/删除。
 * 角色门槛:创建/改名 = Editor(20)+;删除 = Admin(30)+;查看 = 任意成员。
 * 卡片统计(document/chunk/size/index_status)来自列表接口聚合字段,四态:
 * empty 灰 / processing 蓝(动)/ ready 绿 / degraded 橙;"有失败"提示走顶栏 #12 的 has_failure。
 */
const router = useRouter()
const auth = useAuthStore()

const loading = ref(false)
const loadError = ref<string | null>(null)
const items = ref<KnowledgeBaseOut[]>([])

const canEdit = computed(() => isAtLeast(auth.role, ROLE.EDITOR))
const canDelete = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

const formOpen = ref(false)
const formMode = ref<'create' | 'rename'>('create')
const editing = ref<KnowledgeBaseOut | null>(null)

const spaceName = computed(() => auth.currentSpace?.name ?? '')

/** index_status → 圆点颜色 + 文案(工单 #8 的四态映射) */
const INDEX_STATUS_META: Record<string, { label: string; cls: string }> = {
  empty: { label: '暂无文档', cls: 'is-empty' },
  processing: { label: '索引中', cls: 'is-processing' },
  degraded: { label: '部分失败', cls: 'is-degraded' },
  ready: { label: '就绪', cls: 'is-ready' },
}

function indexStatusMeta(status: string) {
  return INDEX_STATUS_META[status] ?? { label: status, cls: 'is-empty' }
}

watch(
  () => auth.currentSpaceId,
  () => load(),
  { immediate: true },
)

async function load() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    items.value = []
    return
  }
  loading.value = true
  loadError.value = null
  try {
    items.value = await apiListKbs(spaceId)
  } catch (e) {
    loadError.value = errMessage(e)
    items.value = []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  formMode.value = 'create'
  editing.value = null
  formOpen.value = true
}

function openRename(kb: KnowledgeBaseOut) {
  formMode.value = 'rename'
  editing.value = kb
  formOpen.value = true
}

function onSaved(kb: KnowledgeBaseOut) {
  const index = items.value.findIndex((k) => k.id === kb.id)
  if (index >= 0) items.value[index] = kb
  else items.value.unshift(kb)
}

async function onDelete(kb: KnowledgeBaseOut) {
  const confirmed = await ElMessageBox.confirm(
    `删除知识库「${kb.name}」?该操作将级联删除其全部文档(含原始文件)、文本分块与向量索引数据,删除后不可恢复。`,
    '删除知识库',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteKb(auth.currentSpaceId!, kb.id)
    items.value = items.value.filter((k) => k.id !== kb.id)
    ElMessage.success('知识库已删除')
  } catch (e) {
    if (e instanceof ApiError && e.code === 'FORBIDDEN') ElMessage.warning('当前角色无权删除知识库')
    else ElMessage.error(errMessage(e))
  }
}

function onCommand(command: 'rename' | 'delete', kb: KnowledgeBaseOut) {
  if (command === 'rename') openRename(kb)
  else onDelete(kb)
}

function openKb(kb: KnowledgeBaseOut) {
  router.push({ name: 'kb-detail', params: { kbId: kb.id } })
}
</script>

<template>
  <div class="page-pad" v-loading="loading">
    <div class="page-head-row">
      <div>
        <div class="page-title">知识库</div>
        <div class="page-sub">
          共 {{ items.length }} 个<template v-if="spaceName"> · 当前空间:{{ spaceName }}</template>
        </div>
      </div>
      <el-button v-if="canEdit" type="primary" @click="openCreate">
        <el-icon class="btn-ic"><Plus /></el-icon>新建知识库
      </el-button>
    </div>

    <!-- 错误态 -->
    <div v-if="loadError" class="state-wrap">
      <el-icon class="state-ic"><WarningFilled /></el-icon>
      <div class="state-title">加载失败</div>
      <div class="state-desc">{{ loadError }}</div>
      <el-button class="state-btn" @click="load">
        <el-icon class="btn-ic"><RefreshRight /></el-icon>重试
      </el-button>
    </div>

    <!-- 空态 -->
    <div v-else-if="!loading && items.length === 0" class="state-wrap">
      <svg width="96" height="72" viewBox="0 0 96 72" fill="none" stroke="#9CA3AF" stroke-width="1.5">
        <rect x="18" y="14" width="60" height="46" rx="6" />
        <path d="M18 26h60" />
        <path d="M30 22h12" />
        <path d="M48 52v-12m0 0l-6 6m6-6l6 6" stroke="#4F6EF2" />
      </svg>
      <div class="state-title">创建第一个知识库</div>
      <div class="state-desc">上传团队文档,让知识随时可问、可溯源</div>
      <el-button v-if="canEdit" type="primary" class="state-btn" @click="openCreate">
        <el-icon class="btn-ic"><Plus /></el-icon>新建知识库
      </el-button>
      <div v-else class="state-desc role-hint">当前角色为 Viewer,无权新建知识库(需 Editor 及以上)</div>
    </div>

    <!-- 骨架屏 -->
    <div v-else-if="loading && items.length === 0" class="kb-grid">
      <div v-for="i in 8" :key="i" class="kb-card">
        <div class="skel" style="width: 40px; height: 40px; border-radius: 8px"></div>
        <div class="skel" style="height: 16px; width: 60%; margin-top: 16px"></div>
        <div class="skel" style="height: 12px; width: 85%; margin-top: 10px"></div>
        <div class="skel" style="height: 12px; width: 45%; margin-top: 10px"></div>
        <div class="skel" style="height: 20px; width: 100%; margin-top: 28px"></div>
      </div>
    </div>

    <!-- 卡片网格 -->
    <div v-else class="kb-grid">
      <div v-for="kb in items" :key="kb.id" class="kb-card" @click="openKb(kb)">
        <div class="kb-icon">
          <el-icon :size="20"><Collection /></el-icon>
        </div>
        <div class="kb-name">{{ kb.name }}</div>
        <div class="kb-desc truncate">{{ kb.description || '暂无描述' }}</div>
        <div class="kb-stats">
          <span>文档 {{ kb.document_count.toLocaleString() }} · 分块 {{ kb.chunk_count.toLocaleString() }} · {{ fmtBytes(kb.size_bytes) }}</span>
          <span class="kb-status" :class="indexStatusMeta(kb.index_status).cls" :title="`index_status=${kb.index_status}`">
            <span class="kb-status-dot"></span>{{ indexStatusMeta(kb.index_status).label }}
          </span>
        </div>
        <div class="kb-foot">
          <span class="kb-model">
            <el-icon :size="12"><Box /></el-icon>
            <span class="truncate">{{ kb.embedding_model }}</span>
            <span class="kb-dim">· {{ kb.embedding_dim }}</span>
          </span>
          <span class="kb-updated">{{ fmtRelative(kb.created_at) }}</span>
        </div>

        <!-- ⋯ 菜单:仅在有权限时出现 -->
        <el-dropdown
          v-if="canEdit || canDelete"
          class="kb-more-wrap"
          trigger="click"
          placement="bottom-end"
          @command="(cmd: 'rename' | 'delete') => onCommand(cmd, kb)"
        >
          <button class="kb-more" title="更多操作" @click.stop>
            <el-icon :size="16"><MoreFilled /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="canEdit" command="rename">
                <el-icon><Edit /></el-icon>重命名
              </el-dropdown-item>
              <el-dropdown-item v-if="canDelete" command="delete" divided class="dd-danger">
                <el-icon><Delete /></el-icon>删除
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <KbFormDialog
      v-model="formOpen"
      :mode="formMode"
      :space-id="auth.currentSpaceId ?? ''"
      :kb="editing"
      @saved="onSaved"
    />
  </div>
</template>

<style scoped>
.btn-ic {
  margin-right: 4px;
}
.role-hint {
  margin-top: 12px;
}
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
}
.kb-card {
  position: relative;
  padding: 20px;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  background: #fff;
  cursor: pointer;
  transition: border-color 0.15s;
}
.kb-card:hover {
  border-color: rgba(156, 163, 175, 0.5);
}
.kb-icon {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  background: #eef1fe;
  display: grid;
  place-items: center;
  color: #4f6ef2;
}
.kb-name {
  margin-top: 12px;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
  transition: color 0.15s;
}
.kb-card:hover .kb-name {
  color: #4f6ef2;
}
.kb-desc {
  margin-top: 4px;
  font-size: 13px;
  color: #6b7280;
}
.kb-stats {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 12px;
  font-size: 12px;
  color: #9ca3af;
}
.kb-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  white-space: nowrap;
}
.kb-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #9ca3af;
}
.kb-status.is-processing .kb-status-dot {
  background: #4f6ef2;
  animation: kb-pulse 1.2s ease-in-out infinite;
}
.kb-status.is-ready .kb-status-dot {
  background: #10b981;
}
.kb-status.is-degraded .kb-status-dot {
  background: #f59e0b;
}
.kb-status.is-degraded {
  color: #f59e0b;
}
@keyframes kb-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
.kb-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(229, 231, 235, 0.7);
}
.kb-model {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  /* min-width:0 让这张徽章可被压缩,超长模型名由内层 .truncate 出省略号 */
  min-width: 0;
  height: 20px;
  padding: 0 6px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #f7f8fa;
  font-size: 11px;
  color: #6b7280;
}
.kb-model .el-icon {
  flex-shrink: 0;
}
/* 维度是短信息,不参与压缩;被省略的是较长的模型名 */
.kb-dim {
  flex-shrink: 0;
  white-space: nowrap;
}
.kb-updated {
  flex-shrink: 0;
  white-space: nowrap;
  font-size: 12px;
  color: #9ca3af;
}
/*
 * ⋯ 菜单:Element Plus 把触发器包在 .el-dropdown 里,而该元素是 position:relative。
 * 若把 absolute 加在内部按钮上,包含块就是那个零尺寸的包装元素(按钮被 absolute 抽离后
 * 包装元素宽高为 0),按钮会跑到卡片外的左下角。故定位加在包装元素上,按钮回归常规流。
 */
.kb-more-wrap {
  position: absolute;
  top: 12px;
  right: 12px;
}
.kb-more {
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
.kb-more:hover {
  background: #f7f8fa;
  color: #111827;
}
</style>
