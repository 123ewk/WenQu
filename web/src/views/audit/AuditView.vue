<script setup lang="ts">
import { RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { ApiError, errMessage } from '@/api/http'
import { apiListAuditLogs, apiListMembers } from '@/api/spaces'
import type { AuditLogOut, MemberOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDateTime } from '@/utils/format'

/**
 * 审计日志(原型 08,空间级):仅当前空间 Admin 及以上可见(路由守卫 + 侧栏显隐)。
 * 筛选:动作类型(下拉,按前缀分组)/ 操作人(成员下拉 → actor_id;自由文本昵称搜索
 * 契约不支持,见缺口清单)/ 时间范围(since/until 闭区间,until 取次日零点含整天)。
 * 结果列直接用 result 枚举渲染圆点;操作人显示 actor_name(账号已删除时兜底)。
 * 导出属后端明确不做(L3),不渲染入口。
 */
const PAGE_SIZE = 20

const auth = useAuthStore()

const items = ref<AuditLogOut[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref<string | null>(null)
const members = ref<MemberOut[]>([])

/** 筛选条件;变化即回第一页重查 */
const filterAction = ref<string | null>(null)
const filterActor = ref<string | null>(null)
const filterRange = ref<[string, string] | null>(null)

const memberMap = computed(() => {
  const map = new Map<string, MemberOut>()
  for (const m of members.value) map.set(m.user_id, m)
  return map
})

/** 动作码 → 中文标签;与契约 AuditAction(23 值)保持同步,未知码原样展示 */
const ACTION_LABELS: Record<string, string> = {
  'auth.register': '注册',
  'auth.login_success': '登录',
  'auth.login_failed': '登录失败',
  'auth.logout': '登出',
  'user.password_changed': '修改密码',
  'user.updated': '修改资料',
  'user.avatar_uploaded': '上传头像',
  'user.avatar_deleted': '删除头像',
  'space.created': '创建空间',
  'space.updated': '空间设置变更',
  'space.deleted': '删除空间',
  'space.member_added': '添加成员',
  'space.member_removed': '移除成员',
  'space.member_role_changed': '成员角色变更',
  'kb.created': '知识库创建',
  'kb.updated': '知识库变更',
  'kb.deleted': '知识库删除',
  'document.uploaded': '文档上传',
  'document.deleted': '文档删除',
  'document.reparsed': '重新解析',
  'api_key.created': '创建 API Key',
  'api_key.revoked': '吊销 API Key',
  'access.denied': '越权访问被拒',
}

/** 动作下拉按前缀分组(对应原型的 登录/文档/知识库/成员/API Key 分组意图) */
const ACTION_GROUPS = computed(() => {
  const GROUP_NAMES: Array<[string, string]> = [
    ['auth.', '认证'],
    ['user.', '用户'],
    ['space.', '空间与成员'],
    ['kb.', '知识库'],
    ['document.', '文档'],
    ['api_key.', 'API Key'],
  ]
  const groups = GROUP_NAMES
    .map(([prefix, label]) => ({
      label,
      options: Object.entries(ACTION_LABELS).filter(([code]) => code.startsWith(prefix)),
    }))
    .filter((g) => g.options.length)
  const rest = Object.entries(ACTION_LABELS).filter(
    ([code]) => !GROUP_NAMES.some(([p]) => code.startsWith(p)),
  )
  if (rest.length) groups.push({ label: '其他', options: rest })
  return groups
})

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

function actorName(log: AuditLogOut): string {
  if (log.actor_name) return log.actor_name
  if (!log.actor_id) return '系统'
  return memberMap.value.get(log.actor_id)?.nickname ?? `${log.actor_id.slice(0, 8)}(已注销)`
}

function actorChar(log: AuditLogOut): string {
  const name = actorName(log)
  return name === '系统' ? 'S' : name.slice(0, 1)
}

function detailText(log: AuditLogOut): string {
  const keys = Object.keys(log.detail ?? {})
  if (!keys.length) return ''
  const text = Object.entries(log.detail)
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(' · ')
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

/** 日期(本地)→ UTC ISO;until 取次日零点(闭区间,含结束日整天) */
function rangeToIso(): { since?: string; until?: string } {
  const range = filterRange.value
  if (!range) return {}
  const [s, e] = range
  const parse = (str: string) => {
    const [y, m, d] = str.split('-').map(Number)
    return new Date(y, m - 1, d)
  }
  const next = parse(e)
  next.setDate(next.getDate() + 1)
  return { since: parse(s).toISOString(), until: next.toISOString() }
}

const hasFilter = computed(() => !!filterAction.value || !!filterActor.value || !!filterRange.value)

watch(
  () => auth.currentSpaceId,
  async () => {
    page.value = 1
    await Promise.all([loadMembers(), loadLogs()])
  },
  { immediate: true },
)

async function loadMembers() {
  const id = auth.currentSpaceId
  if (!id) return
  try {
    members.value = await apiListMembers(id)
  } catch {
    members.value = [] // 成员下拉失败不影响日志列表本身
  }
}

async function loadLogs() {
  const id = auth.currentSpaceId
  if (!id) return
  loading.value = true
  error.value = null
  try {
    const data = await apiListAuditLogs(id, PAGE_SIZE, (page.value - 1) * PAGE_SIZE, {
      action: filterAction.value || undefined,
      actor_id: filterActor.value || undefined,
      ...rangeToIso(),
    })
    items.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : errMessage(e)
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  page.value = 1
  loadLogs()
}

function resetFilter() {
  filterAction.value = null
  filterActor.value = null
  filterRange.value = null
  onFilterChange()
}

async function onPageChange(p: number) {
  page.value = p
  await loadLogs()
}

function onRetry() {
  page.value = 1
  loadLogs()
  loadMembers()
}
</script>

<template>
  <div class="page-pad">
    <div class="audit-wrap">
      <!-- 错误态 -->
      <div v-if="error && !loading" class="state-box">
        <el-icon class="state-ic danger"><WarningFilled /></el-icon>
        <div class="state-title">加载失败</div>
        <div class="state-desc">{{ error }}</div>
        <el-button class="state-btn" @click="onRetry">
          <el-icon class="btn-ic"><RefreshRight /></el-icon>重试
        </el-button>
      </div>

      <!-- 正常/空 态 -->
      <div v-else class="card table-card" v-loading="loading">
        <!-- 筛选条(时间范围/动作类型/操作人;导出属 L3 不渲染) -->
        <div class="flt-bar">
          <el-date-picker
            v-model="filterRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            :clearable="true"
            style="width: 240px"
            @change="onFilterChange"
          />
          <el-select
            v-model="filterAction"
            placeholder="全部动作"
            clearable
            filterable
            style="width: 170px"
            @change="onFilterChange"
          >
            <el-option-group v-for="g in ACTION_GROUPS" :key="g.label" :label="g.label">
              <el-option v-for="opt in g.options" :key="opt[0]" :label="opt[1]" :value="opt[0]" />
            </el-option-group>
          </el-select>
          <el-select
            v-model="filterActor"
            placeholder="全部操作人"
            clearable
            filterable
            style="width: 150px"
            @change="onFilterChange"
          >
            <el-option
              v-for="m in members"
              :key="m.user_id"
              :label="m.nickname"
              :value="m.user_id"
            />
          </el-select>
          <el-button v-if="hasFilter" text @click="resetFilter">清除筛选</el-button>
          <span class="flt-total">共 {{ total.toLocaleString() }} 条记录</span>
        </div>

        <template v-if="items.length">
          <el-table :data="items" style="width: 100%">
            <el-table-column label="时间" width="150">
              <template #default="{ row }">
                <span class="mono cell-dim">{{ fmtDateTime(row.created_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作人" width="140">
              <template #default="{ row }">
                <div class="actor-cell">
                  <span class="actor-avatar" :class="{ sys: !row.actor_id }">{{ actorChar(row as AuditLogOut) }}</span>
                  <span class="actor-name" :title="row.actor_id ?? ''">{{ actorName(row as AuditLogOut) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="动作类型" width="140">
              <template #default="{ row }">
                <span class="act-badge">{{ actionLabel(row.action) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="对象" min-width="280">
              <template #default="{ row }">
                <span class="obj-target">{{ row.target || '—' }}</span>
                <span v-if="detailText(row as AuditLogOut)" class="obj-detail">{{ detailText(row as AuditLogOut) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <span class="result-cell" :class="(row as AuditLogOut).result === 'denied' ? 'is-denied' : 'is-success'">
                  <span class="result-dot"></span>{{ (row as AuditLogOut).result === 'denied' ? '拒绝' : '成功' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="IP" width="130">
              <template #default="{ row }">
                <span class="mono cell-dim">{{ row.ip }}</span>
              </template>
            </el-table-column>
          </el-table>

          <div class="pager">
            <span class="pager-total">共 {{ total.toLocaleString() }} 条记录</span>
            <el-pagination
              layout="prev, pager, next"
              :total="total"
              :page-size="PAGE_SIZE"
              :current-page="page"
              @current-change="onPageChange"
            />
          </div>
        </template>

        <el-empty
          v-else-if="!loading"
          :description="hasFilter ? '没有符合筛选条件的记录' : '暂无审计记录'"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.audit-wrap {
  max-width: 1240px;
}
.btn-ic {
  margin-right: 4px;
}

.table-card {
  overflow: hidden;
  min-height: 200px;
}
.flt-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid #e5e7eb;
}
.flt-total {
  margin-left: auto;
  font-size: 13px;
  color: #9ca3af;
}
.result-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.result-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.result-cell.is-success {
  color: #374151;
}
.result-cell.is-success .result-dot {
  background: #10b981;
}
.result-cell.is-denied {
  color: #ef4444;
}
.result-cell.is-denied .result-dot {
  background: #ef4444;
}
.cell-dim {
  font-size: 13px;
  color: #6b7280;
}
.actor-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.actor-avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #eef1fe;
  color: #4f6ef2;
  font-size: 10px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.actor-avatar.sys {
  background: #111827;
  color: #fff;
}
.actor-name {
  font-size: 13px;
  color: #374151;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.act-badge {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  font-size: 11px;
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  color: #6b7280;
  line-height: 1;
  white-space: nowrap;
}
.obj-target {
  font-size: 13px;
  color: #6b7280;
}
.obj-detail {
  display: block;
  font-size: 11px;
  color: #9ca3af;
  margin-top: 1px;
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

.state-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 96px 0;
}
.state-ic {
  font-size: 44px;
  width: 48px;
  height: 48px;
  border-radius: 50%;
  padding: 12px;
  box-sizing: border-box;
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}
.state-title {
  margin-top: 16px;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.state-desc {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
.state-btn {
  margin-top: 20px;
}
</style>
