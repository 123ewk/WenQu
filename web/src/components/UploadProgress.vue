<script setup lang="ts">
import { Check, RefreshRight, UploadFilled, WarningFilled } from '@element-plus/icons-vue'

import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useIngestionStore } from '@/stores/ingestion'
import { useUploadStore } from '@/stores/uploads'
import { fmtBytes } from '@/utils/format'
import { statusLabel } from '@/utils/ingest'

/**
 * 顶栏全局上传/入库进度(原型 03/04 顶栏入口)。
 * 上半部分:浏览器上传阶段(axios onUploadProgress);入队后该任务转"已入队"。
 * 下半部分:空间级入库进度(GET /ingestion-progress 轮询),上传完成即刷新一次,
 * total_active > 0 时自动轮询、归零停止;失败项可点击跳到对应知识库详情。
 */
const router = useRouter()
const auth = useAuthStore()
const uploads = useUploadStore()
const ingestion = useIngestionStore()

const runningCount = computed(() => uploads.tasks.filter((t) => t.phase === 'uploading').length)
const queuedCount = computed(() => uploads.tasks.filter((t) => t.phase === 'queued').length)
const hasFinished = computed(() => uploads.tasks.some((t) => t.phase !== 'uploading'))

function onVisibleChange(visible: boolean) {
  if (visible) {
    // 打开浮层即拉一次:归零后轮询已停,跨标签页/其他客户端的入库动态靠这次刷新可见
    if (auth.currentSpaceId) ingestion.refresh(auth.currentSpaceId)
    return
  }
  if (!runningCount.value) uploads.clearFinished()
}

watch(
  () => auth.currentSpaceId,
  (id) => {
    if (id) ingestion.refresh(id)
    else ingestion.reset()
  },
  { immediate: true },
)

/** 失败项跳转:kb_id + document_id 定位到知识库详情的文档列表 */
function goFailed(item: { kb_id: string }) {
  router.push({ name: 'kb-detail', params: { kbId: item.kb_id } })
}
</script>

<template>
  <el-popover :width="320" trigger="click" popper-class="up-pop" @hide="onVisibleChange(false)">
    <template #reference>
      <button class="icon-entry" :class="{ 'is-active': uploads.tasks.length > 0 }" title="上传进度">
        <el-icon :size="18"><UploadFilled /></el-icon>
        <span v-if="uploads.activeCount > 0" class="entry-badge">{{ uploads.activeCount }}</span>
        <span v-else-if="queuedCount > 0" class="entry-badge is-queued">{{ queuedCount }}</span>
      </button>
    </template>

    <div class="up-head">
      <span class="up-title">上传与入库</span>
      <span class="up-summary">
        <template v-if="uploads.tasks.length === 0 && ingestion.active.length === 0">暂无任务</template>
        <template v-else-if="uploads.tasks.length === 0 && ingestion.totalActive === 0">无进行中任务</template>
        <template v-else>
          <template v-if="uploads.tasks.length">{{ runningCount }} 个上传中 · {{ queuedCount }} 个已入队</template>
          <template v-else-if="ingestion.totalActive > 0">入库中 {{ ingestion.totalActive }} 个</template>
        </template>
      </span>
    </div>

    <template v-if="ingestion.hasFailure">
      <div class="up-alert">
        <el-icon><WarningFilled /></el-icon>有文档处理失败,见下方列表
      </div>
    </template>

    <template v-if="uploads.tasks.length">
      <div v-if="runningCount" class="up-total">
        <div class="up-total-row">
          <span>总进度</span><span class="mono">{{ uploads.overallPercent }}%</span>
        </div>
        <el-progress :percentage="uploads.overallPercent" :show-text="false" :stroke-width="6" />
      </div>

      <div class="up-list">
        <div v-for="task in uploads.tasks" :key="task.id" class="up-item">
          <div class="up-item-main">
            <div class="up-item-row">
              <span class="up-name truncate" :title="task.filename">{{ task.filename }}</span>
              <span v-if="task.phase === 'uploading'" class="up-pct mono">{{ task.percent }}%</span>
              <span v-else-if="task.phase === 'queued'" class="up-ok">
                <el-icon><Check /></el-icon>已入队
              </span>
            </div>
            <el-progress
              v-if="task.phase === 'uploading'"
              :percentage="task.percent"
              :show-text="false"
              :stroke-width="4"
            />
            <div class="up-meta">
              <span>{{ fmtBytes(task.sizeBytes) }} · {{ task.kbName }}</span>
              <span v-if="task.phase === 'uploading'">上传中</span>
              <span v-else-if="task.phase === 'queued'">已入队,入库进度见下</span>
              <span v-else class="up-err">{{ task.error }}</span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="hasFinished" class="up-foot">
        <el-button size="small" text @click="uploads.clearFinished()">
          <el-icon><RefreshRight /></el-icon>清除已结束
        </el-button>
      </div>
    </template>

    <!-- 入库进度(空间级聚合):在途 + 近期失败 -->
    <template v-if="ingestion.active.length">
      <div class="up-ingest-head">
        <span>入库中 {{ ingestion.totalActive }} 个</span>
        <span v-if="ingestion.hasFailure" class="up-ingest-fail">有失败</span>
      </div>
      <div class="up-list">
        <div
          v-for="item in ingestion.active"
          :key="item.document_id"
          class="up-item"
          :class="{ 'is-clickable': item.status === 'failed' }"
          @click="item.status === 'failed' && goFailed(item)"
        >
          <div class="up-item-row">
            <span class="up-name truncate" :title="item.filename">{{ item.filename }}</span>
            <span
              class="up-status"
              :class="item.status === 'failed' ? 'is-failed' : 'is-running'"
            >
              {{ item.status === 'failed' ? '失败 · 点击查看' : statusLabel(item.status) }}
            </span>
          </div>
          <div v-if="item.error_message" class="up-meta">
            <span class="up-err truncate" :title="item.error_message">{{ item.error_message }}</span>
          </div>
        </div>
      </div>
    </template>
  </el-popover>
</template>

<style scoped>
.icon-entry {
  position: relative;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: none;
  display: grid;
  place-items: center;
  color: #6b7280;
  cursor: pointer;
  flex-shrink: 0;
}
.icon-entry:hover {
  background: #f7f8fa;
  color: #111827;
}
.entry-badge {
  position: absolute;
  top: -2px;
  right: -2px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: #4f6ef2;
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}
.entry-badge.is-queued {
  background: #10b981;
}
.up-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 2px 10px;
  border-bottom: 1px solid #e5e7eb;
}
.up-title {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.up-summary {
  font-size: 12px;
  color: #9ca3af;
}
.up-total {
  padding: 10px 2px;
  border-bottom: 1px solid #e5e7eb;
}
.up-total-row {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 12px;
  color: #6b7280;
}
.up-list {
  max-height: 260px;
  overflow-y: auto;
}
.up-item {
  padding: 10px 2px;
  border-bottom: 1px solid rgba(229, 231, 235, 0.7);
}
.up-item-main {
  min-width: 0;
}
.up-item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.up-name {
  flex: 1;
  font-size: 12px;
  color: #374151;
}
.up-pct {
  font-size: 12px;
  color: #9ca3af;
}
.up-ok {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  color: #10b981;
}
.up-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 11px;
  color: #9ca3af;
}
.up-err {
  color: #ef4444;
}
.up-foot {
  display: flex;
  justify-content: flex-end;
  padding-top: 8px;
}
.up-alert {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 2px 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(239, 68, 68, 0.06);
  font-size: 12px;
  color: #ef4444;
}
.up-ingest-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 2px 2px;
  font-size: 12px;
  color: #6b7280;
}
.up-ingest-fail {
  color: #ef4444;
}
.up-item.is-clickable {
  cursor: pointer;
  border-radius: 6px;
}
.up-item.is-clickable:hover {
  background: #f7f8fa;
}
.up-status {
  font-size: 12px;
  color: #4f6ef2;
  white-space: nowrap;
}
.up-status.is-failed {
  color: #ef4444;
}
</style>
