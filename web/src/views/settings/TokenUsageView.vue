<script setup lang="ts">
/**
 * OPT-28 Token 用量页(对接说明 §2 v3.0,已按 openapi.json 核对),路由 /settings/usage:
 * - Admin+ 看"按成员汇总表"(space 合计 + 成员行);Viewer/Editor 直达本页拿 403 也只留"我的用量";
 * - 所有成员看"我的用量"(GET /token-usage/me,响应是嵌套 {usage});
 * - 响应里没有 source 字段:实测/估算靠 estimated_tokens(估算那部分的 total 之和),>0 合计打 ≈。
 */
import { RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { apiGetMyTokenUsage, apiGetTokenUsage } from '@/api/usage'
import { ApiError, errMessage } from '@/api/http'
import type { MemberUsageOut, MyUsageOut, UsageOut, UsageSummaryOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDateTime } from '@/utils/format'
import { isAtLeast, ROLE, roleLabel } from '@/utils/roles'

const auth = useAuthStore()

const canViewSummary = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

const PERIODS: Array<{ value: '7d' | '30d' | 'all'; label: string }> = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: 'all', label: '全部' },
]

const period = ref<'7d' | '30d' | 'all'>('30d')
const summary = ref<UsageSummaryOut | null>(null)
const mine = ref<MyUsageOut | null>(null)
/** 角色被降级后旧页面点进来:汇总表 403,只留"我的用量",不算页面错误(§2.1 门槛口径) */
const summaryForbidden = ref(false)
const loading = ref(false)
const loadError = ref<string | null>(null)

/** 后端排序口径:按 total 降序、同分按 username(§2.1);前端排,不依赖返回顺序 */
const sortedMembers = computed(() => {
  const rows = summary.value?.members ?? []
  return [...rows].sort((a, b) => b.total_tokens - a.total_tokens || a.username.localeCompare(b.username))
})

/** Σmembers ≤ space_total 是设计如此:差值 = 已退空间/已注销成员与无归属行的既成用量(§2.1) */
const otherTokens = computed(() => {
  const s = summary.value
  if (!s) return 0
  return Math.max(0, s.space_total.total_tokens - s.members.reduce((acc, m) => acc + m.total_tokens, 0))
})

/** 任一行含估算部分就要挂口径说明(§2.3 必须放的文案) */
const hasEstimated = computed(() => {
  const rows: UsageOut[] = [
    ...sortedMembers.value,
    mine.value?.usage ?? null,
    summary.value?.space_total ?? null,
  ].filter((r): r is UsageOut => !!r)
  return rows.some((r) => r.estimated_tokens > 0)
})

async function load() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  loading.value = true
  loadError.value = null
  try {
    // 先渲染人人都有的"我的用量";Admin+ 再拉汇总表,汇总失败不拖垮整页
    mine.value = await apiGetMyTokenUsage(spaceId, period.value)
    summaryForbidden.value = false
    if (canViewSummary.value) {
      try {
        summary.value = await apiGetTokenUsage(spaceId, period.value)
      } catch (e) {
        summary.value = null
        if (e instanceof ApiError && e.status === 403) summaryForbidden.value = true
        else throw e
      }
    } else {
      summary.value = null
    }
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

/** 估算部分的 total 之和 > 0 → 合计打 ≈(实测/估算不混算,§2.1) */
function approx(row: UsageOut | null): string {
  return row && row.estimated_tokens > 0 ? '≈' : ''
}

/** period=all 时 since 是 null(不是 0),别当日期用;generated_at 是服务端 UTC 时刻 */
const rangeLabel = computed(() => {
  const s = summary.value ?? mine.value
  if (!s) return ''
  const to = fmtDateTime(s.generated_at)
  return s.since ? `统计区间 ${fmtDateTime(s.since)} ~ ${to}` : `统计区间 全部(截至 ${to})`
})

function memberLabel(m: MemberUsageOut): string {
  return m.nickname ? `${m.nickname}(${m.username})` : m.username
}
</script>

<template>
  <div class="page-pad">
    <div class="tu-wrap" data-tour="tour-usage" v-loading="loading">
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
            <div class="tu-num">{{ approx(summary.space_total) }}{{ n(summary.space_total.total_tokens) }}</div>
            <div class="tu-cap">空间合计 tokens<span v-if="summary.space_total.estimated_tokens > 0">(含估算 {{ n(summary.space_total.estimated_tokens) }})</span></div>
          </div>
          <div class="tu-total-item">
            <div class="tu-num">{{ n(summary.space_total.prompt_tokens) }}</div>
            <div class="tu-cap">输入</div>
          </div>
          <div class="tu-total-item">
            <div class="tu-num">{{ n(summary.space_total.completion_tokens) }}</div>
            <div class="tu-cap">输出</div>
          </div>
          <div class="tu-generated">{{ rangeLabel }}</div>
        </div>

        <div class="card tu-table">
          <el-table :data="sortedMembers" style="width: 100%">
            <el-table-column label="成员" min-width="170">
              <template #default="{ row }">
                <span class="tu-strong">{{ memberLabel(row as MemberUsageOut) }}</span>
                <span class="tu-role">{{ roleLabel(row.role) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="对话调用" width="100" align="right">
              <template #default="{ row }">{{ n(row.chat_calls) }}</template>
            </el-table-column>
            <el-table-column label="输入 token" width="130" align="right">
              <template #default="{ row }">{{ n(row.prompt_tokens) }}</template>
            </el-table-column>
            <el-table-column label="输出 token" width="130" align="right">
              <template #default="{ row }">{{ n(row.completion_tokens) }}</template>
            </el-table-column>
            <el-table-column label="合计" width="140" align="right">
              <template #default="{ row }">
                <span class="tu-strong">{{ approx(row as MemberUsageOut) }}{{ n(row.total_tokens) }}</span>
                <span v-if="(row as MemberUsageOut).estimated_tokens > 0" class="tu-est">估算 {{ n(row.estimated_tokens) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="向量化调用" width="110" align="right">
              <template #default="{ row }">{{ n(row.embedding_calls) }}</template>
            </el-table-column>
          </el-table>
          <div v-if="!sortedMembers.length && !loading" class="tu-empty">该时间窗内还没有用量记录(用量自计量上线起统计,历史不回填)</div>
          <!-- 成员行之和 ≤ 空间合计是设计如此:差值归"已离成员/无归属" (§2.1) -->
          <div v-if="otherTokens > 0" class="tu-other">其他/历史归属 ≈ {{ n(otherTokens) }}(已退出成员与无归属记录,不计入上方成员行)</div>
        </div>
      </template>
      <div v-else-if="!canViewSummary || summaryForbidden" class="tu-note">
        {{ canViewSummary ? '暂无权限查看按成员汇总表(需要空间管理员权限),以下展示你本人的用量。' : '按成员汇总表仅空间 Admin 及以上可查看,以下展示你本人的用量。' }}
      </div>

      <!-- 我的用量(所有成员;响应嵌套在 usage 里,没有 members 字段) -->
      <div v-if="mine" class="card tu-mine">
        <div class="tu-mine-head">
          <div class="tu-title">我的用量</div>
          <div class="tu-generated tu-generated-inline">{{ rangeLabel }}</div>
        </div>
        <div class="tu-mine-grid">
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ approx(mine.usage) }}{{ n(mine.usage.total_tokens) }}</span><span class="tu-cap">合计 tokens<span v-if="mine.usage.estimated_tokens > 0">(含估算 {{ n(mine.usage.estimated_tokens) }})</span></span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.usage.prompt_tokens) }}</span><span class="tu-cap">输入</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.usage.completion_tokens) }}</span><span class="tu-cap">输出</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.usage.chat_calls) }}</span><span class="tu-cap">对话调用次数</span></div>
          <div class="tu-mine-item"><span class="tu-num tu-num-sm">{{ n(mine.usage.embedding_calls) }}</span><span class="tu-cap">向量化调用</span></div>
        </div>
      </div>

      <!-- §2.3 口径提示(后端要求原文照抄、必须放) -->
      <div v-if="hasEstimated || summary || mine" class="tu-note">
        统计含对话与向量化两类用量。个别供应商不回传精确 token 数,此时后端按「1 汉字≈0.75 token、
        1 字母≈0.25 token」估算并单列(标 ≈),与厂商回传的精确值不混算。
        用量自 2026-09-21(计量上线)起统计,历史不回填。
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
.tu-generated-inline {
  position: static;
}
.tu-est {
  margin-left: 6px;
  font-size: 11px;
  color: #9ca3af;
}
.tu-other {
  padding: 10px 16px;
  font-size: 12px;
  color: #9ca3af;
  border-top: 1px solid #f3f4f6;
}
.tu-mine-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  position: relative;
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
