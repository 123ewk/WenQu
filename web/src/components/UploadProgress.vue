<script setup lang="ts">
import { Check, RefreshRight, UploadFilled } from '@element-plus/icons-vue'

import { useUploadStore } from '@/stores/uploads'
import { fmtBytes } from '@/utils/format'

/**
 * 顶栏全局上传进度(原型 03/04 顶栏入口)。
 * 进度来自浏览器上传阶段(axios onUploadProgress);入队后的解析/分块/向量化
 * 需在文档列表按状态轮询查看,契约未提供全局入库进度接口。
 */
const uploads = useUploadStore()

const runningCount = computed(() => uploads.tasks.filter((t) => t.phase === 'uploading').length)
const queuedCount = computed(() => uploads.tasks.filter((t) => t.phase === 'queued').length)
const hasFinished = computed(() => uploads.tasks.some((t) => t.phase !== 'uploading'))

function onVisibleChange(visible: boolean) {
  if (!visible && !runningCount.value) uploads.clearFinished()
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
      <span class="up-title">上传进度</span>
      <span class="up-summary">
        <template v-if="uploads.tasks.length === 0">暂无上传任务</template>
        <template v-else>{{ runningCount }} 个进行中 · {{ queuedCount }} 个已入队</template>
      </span>
    </div>

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
              <span v-else-if="task.phase === 'queued'">解析状态见文档列表</span>
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
</style>
