<script setup lang="ts">
/**
 * OPT-28 Token 用量页(对接说明 §2),路由 /settings/usage:
 * - Admin+ 看"按成员汇总表"(space 合计 + 成员行,按合计降序);
 * - 所有成员看"我的用量"(普通成员只有这个入口);
 * - 行内 source=estimated 的 token 数打 ≈ 角标(个别供应商不回传精确用量,后端估算)。
 */
import { RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { apiGetMyTokenUsage, apiGetTokenUsage } from '@/api/usage'
import { errMessage } from '@/api/http'
import type { MemberTokenUsage, TokenUsageOut, UsagePeriod } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDateTime } from '@/utils/format'
import { isAtLeast, ROLE, roleLabel } from '@/utils/roles'

const auth = useAuthStore()

const canViewSummary = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

const PERIODS: Array<{ value: UsagePeriod; label: string }> = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: 'all', label: '全部' },
]

const period = ref<UsagePeriod>('30d')
const summary = ref<TokenUsageOut | null>(null)
const mine = ref<MemberTokenUsage | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)

/** 后端排序口径是"按合计降序",前端排,不依赖返回顺序 */
const sortedMembers = computed(() => {
  const rows = summary.value?.members ?? []
  return [...rows].sort((a, b) => b.total_tokens - a.total_tokens)
})

/** 任一行是估算值就要挂口径说明(§2.1 必须放的文案) */
const hasEstimated = computed(() => {
  const rows: Array<{ source?: string } | null> = [...sortedMembers.value, mine.value, summary.value?.space_total ?? null]
  return rows.some((r) => r?.source === 'estimated')
})

async function load() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  loading.value = true
  loadError.value = null
  try {
    const tasks: Array<Promise<unknown>> = [apiGetMyTokenUsage(spaceId, period.value).then((m) => (mine.value = m))]
    if (canViewSummary.value) {
      tasks.push(apiGetTokenUsage(spaceId, period.value).then((s) => (summary.value = s)))
    } else {
      summary.value = null
    }
    await Promise.all(tasks)
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

watch([() => auth.currentSpaceId, period], load, { immediate: true })

function n(v: number | null | undefined): string {
  return (v ?? 0).toLocaleString()
}

/** estimated 行加 ≈ 前缀(精确值不加,避免满屏角标) */
function approx(row: { source?: string }): string {
  return row.source === 'estimated' ? '≈' : ''
}

function memberLabel(m: MemberTokenUsage): string {
  return m.nickname ? `${m.nickname}(${m.username})` : m.username
}
</script>

<template>
  <div class="page-pad">
    <div class="tu-wrap" v-loading="loading">
      <div class="page-head-row">
        <div>
          <div class="page-title">Token 用量</div>
          <div class="page-sub">统计含对话与向量化;个别供应商不回传精确用量,此时为后端估算值(≈)</div>
        </div>
        <div class="tu-tools">
          <el-radio-group v-model="period" size="small">
            <el-radio-button v-for="p in PERIODS" :key="p.value" :value="p.value">{{ p.label }}</el-radio-button>
          </el-radio-group>
        </div>
      </div>

      <div v-if="loadError" class="tu-error">
        <el-icon class="btn-ic"><WarningFilled /></el-icon>{{ loadError }}
        <el-button size="small" @click="load">
          <el-icon class="btn-ic"><RefreshRight /></el-icon>重试
        </el-button>
      </div>

      <!-- Admin+ 汇总表 -->
      <template v-if="canViewSummary && summary">
        <div class="card tu-total">
          <div class="tu-total-item">
            <div class="tu-num">{{ n(summary.space_total.total_tokens) }}</div>
            <div class="tu-cap">空间合计 tokens{{ approx(summary.space_total) }}</div>
          </div>
          <div class="tu-total-item">
            <div class="tu-num">{{ n(summary.space_total.prompt_tokens) }}</div>
            <div class="tu-cap">输入</div>
          </div>
          <div class="tu-total-item">
            <div class="tu-num">{{ n(summary.space_total.completion_tokens) }}</div>
            <div class="tu-cap">输出</div>
          </div>
          <div class="tu-generated">统计生成于 {{ fmtDateTime(summary.generated_at) }}</div>
        </div>

        <div class="card tu-table">
          <el-table :data="sortedMembers" style="width: 100%">
            <el-table-column label="成员" min-width="170">
              <template #default="{ row }">
                <span class="tu-strong">{{ memberLabel(row as MemberTokenUsage) }}</span>
                <span class="tu-role">{{ roleLabel(row.role) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="对话次数" width="100" align="right">
              <template #default="{ row }">{{ n(row.chat_calls) }}</template>
            </el-table-column>
            <el-table-column label="输入 token" width="130" align="right">
              <template #default="{ row }">{{ approx(row) }}{{ n(row.prompt_tokens) }}</template>
            </el-table-column>
            <el-table-column label="输出 token" width="130" align="right">
              <template #default="{ row }">{{ approx(row) }}{{ n(row.completion_tokens) }}</template>
            </el-table-column>
            <el-table-column label="合计" width="130" align="right">
              <template #default="{ row }">
                <span class="tu-strong">{{ approx(row) }}{{ n(row.total_tokens) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="向量化调用" width="110" align="right">
              <template #default="{ row }">{{ n(row.embedding_calls) }}</template>
            </el-table-column>
          </el-table>
          <div v-if="!sortedMembers.length && !loading" class="tu-empty">该时间窗内还没有用量记录</div>
        </div>
      </template>
      <div v-else-if="!canViewSummary" class="tu-note">
        按成员汇总表仅空间 <b>Admin 及以上</b> 可查看,以下展示你本人的用量。
      </div>

      <!-- 我的用量(所有成员) -->
      <div v-if="mine" class="card tu-mine">
        <div class="tu-title">我的用量</div>
        <div class="tu-mine-grid">
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ approx(mine) }}{{ n(mine.total_tokens) }}</span><span class="tu-cap">合计 tokens</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.prompt_tokens) }}</span><span class="tu-cap">输入</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.completion_tokens) }}</span><span class="tu-cap">输出</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.chat_calls) }}</span><span class="tu-cap">对话次数</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.embedding_calls) }}</span><span class="tu-cap">向量化调用</span></div>
        </div>
      </div>

      <div v-if="hasEstimated" class="tu-note">
        带 ≈ 的数字为后端估算值:个别供应商不回传精确用量。
      </div>
    </div>
  </div>
</template>

<style scoped>
.btn-ic {
  margin-right: 4px;
}
.tu-wrap {
  max-width: 980px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.tu-tools {
  display: flex;
  align-items: center;
}
.tu-error {
  display: flex;
  align-items: center;
  font-size: 13px;
  color: #ef4444;
}
.tu-total {
  position: relative;
  padding: 20px;
  display: flex;
  gap: 48px;
}
.tu-num {
  font-size: 22px;
  font-weight: 600;
  color: #111827;
  font-variant-numeric: tabular-nums;
}
.tu-num-sm {
  font-size: 17px;
}
.tu-cap {
  margin-top: 2px;
  font-size: 12px;
  color: #9ca3af;
}
.tu-generated {
  position: absolute;
  right: 20px;
  bottom: 10px;
  font-size: 11px;
  color: #9ca3af;
}
.tu-table {
  padding: 6px 0 0;
}
.tu-strong {
  font-weight: 500;
  color: #111827;
}
.tu-role {
  display: inline-flex;
  align-items: center;
  height: 18px;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 6px;
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  font-size: 11px;
  color: #6b7280;
}
.tu-empty {
  padding: 24px;
  text-align: center;
  font-size: 13px;
  color: #9ca3af;
}
.tu-mine {
  padding: 20px;
}
.tu-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
  margin-bottom: 12px;
}
.tu-mine-grid {
  display: flex;
  gap: 40px;
  flex-wrap: wrap;
}
.tu-mine-item {
  display: flex;
  flex-direction: column;
}
.tu-note {
  padding: 12px 16px;
  border-radius: 8px;
  background: #f7f8fa;
  font-size: 13px;
  line-height: 1.6;
  color: #6b7280;
}
</style>
