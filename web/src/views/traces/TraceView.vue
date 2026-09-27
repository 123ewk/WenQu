<script setup lang="ts">
/**
 * OPT-49 v2 调用追踪页:问答链路 trace 的产品内查看 + 空间级 Langfuse 接入配置。
 * 查看口径(后端强制):成员只能看自己的 trace(伪造 user_id 参数也被覆盖);
 * Admin+ 可看全空间,也可切"仅自己"。配置卡仅空间 Admin+(role≥30)可见,
 * 未接入时 Admin 就地配置,成员看到引导提示。
 */
import {
  apiGetSpaceLangfuseConfig,
  apiGetSpaceLangfuseTrace,
  apiListSpaceLangfuseTraces,
  apiPutSpaceLangfuseConfig,
} from '@/api/langfuseConfig'
import { errMessage } from '@/api/http'
import type { LangfuseConfigOut, LangfuseTraceDetailOut, LangfuseTraceItem } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { isAtLeast, ROLE } from '@/utils/roles'

const auth = useAuthStore()
const canManage = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

/* ───── trace 列表 ───── */

const traces = ref<LangfuseTraceItem[]>([])
const configured = ref(true)
const loading = ref(false)
const loadError = ref<string | null>(null)
/** Admin+ 视角:全空间 / 仅自己(成员恒看自己,与该值无关) */
const scope = ref<'all' | 'self'>('all')

async function loadTraces() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    traces.value = []
    configured.value = true
    return
  }
  loading.value = true
  loadError.value = null
  try {
    const userId = canManage.value && scope.value === 'self' ? auth.user?.id : undefined
    const data = await apiListSpaceLangfuseTraces(spaceId, userId ? { user_id: userId } : undefined)
    traces.value = data.items
    configured.value = data.configured
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

function fmtTime(ts: string | null): string {
  return ts ? ts.replace('T', ' ').slice(0, 19) : '—'
}
function fmtLatency(s: number | null): string {
  return s == null ? '—' : `${s.toFixed(2)}s`
}
function questionOf(item: LangfuseTraceItem): string {
  const q = (item.input as { question?: unknown } | null)?.question
  if (typeof q !== 'string' || !q) return '—'
  return q.length > 40 ? `${q.slice(0, 40)}…` : q
}
function userOf(item: LangfuseTraceItem): string {
  const u = item.metadata?.user_id
  return typeof u === 'string' ? u.slice(0, 8) : '—'
}

/* ───── trace 详情 ───── */

const detail = ref<LangfuseTraceDetailOut | null>(null)
const detailOpen = ref(false)
const detailLoading = ref(false)

async function openDetail(row: LangfuseTraceItem) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await apiGetSpaceLangfuseTrace(spaceId, row.id)
  } catch (e) {
    detailOpen.value = false
    ElMessage.error(errMessage(e))
  } finally {
    detailLoading.value = false
  }
}

function pretty(v: unknown): string {
  return JSON.stringify(v ?? null, null, 2)
}

/* ───── 接入配置(空间 Admin+ 专属) ───── */

const lfConfig = ref<LangfuseConfigOut | null>(null)
const lfHost = ref('')
const lfPublicKey = ref('')
const lfSecretInput = ref('')
const lfClearSecret = ref(false)
const lfSaving = ref(false)

async function loadConfig() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    lfConfig.value = null
    return
  }
  try {
    lfConfig.value = await apiGetSpaceLangfuseConfig(spaceId)
    lfHost.value = lfConfig.value.host
    lfPublicKey.value = lfConfig.value.public_key
    lfSecretInput.value = ''
    lfClearSecret.value = false
  } catch (e) {
    lfConfig.value = null
    ElMessage.error(errMessage(e))
  }
}

async function onSaveLangfuse() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !lfConfig.value || lfSaving.value) return
  // 密钥三态:勾了清除 → 空串;输入了新值 → 替换;否则 → null(不改动)
  const secret = lfClearSecret.value ? '' : lfSecretInput.value || null
  try {
    lfSaving.value = true
    lfConfig.value = await apiPutSpaceLangfuseConfig(spaceId, {
      host: lfHost.value,
      public_key: lfPublicKey.value,
      secret_key: secret,
      enabled: lfConfig.value.enabled,
    })
    lfSecretInput.value = ''
    lfClearSecret.value = false
    ElMessage.success('Langfuse 接入配置已保存')
  } catch (e) {
    ElMessage.error(errMessage(e))
  } finally {
    lfSaving.value = false
  }
}

async function onToggleLangfuse(): Promise<boolean> {
  const spaceId = auth.currentSpaceId
  const current = lfConfig.value
  if (!spaceId || !current || lfSaving.value) return false
  const opening = !current.enabled
  if (opening && !current.key_hint) {
    ElMessage.warning('先保存 Secret Key,再开启上送')
    return false
  }
  try {
    await ElMessageBox.confirm(
      opening
        ? '开启后,本空间所有问答的 trace(含问题与答案原文)将上送到该 Langfuse 项目。确认开启?'
        : '关闭后停止上送 trace(已存数据保留在 Langfuse)。确认关闭?',
      opening ? '开启 Langfuse 上送' : '关闭 Langfuse 上送',
      { confirmButtonText: opening ? '确认开启' : '确认关闭', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return false
  }
  try {
    lfSaving.value = true
    lfConfig.value = await apiPutSpaceLangfuseConfig(spaceId, {
      host: lfHost.value,
      public_key: lfPublicKey.value,
      secret_key: null, // 开关切换不动密钥
      enabled: opening,
    })
    loadTraces() // 开关决定列表页"未接入"提示与数据,拨完即刷新
    return true
  } catch (e) {
    ElMessage.error(errMessage(e))
    return false
  } finally {
    lfSaving.value = false
  }
}

watch(
  () => auth.currentSpaceId,
  () => {
    scope.value = 'all'
    loadTraces()
    if (canManage.value) loadConfig()
  },
  { immediate: true },
)
</script>

<template>
  <div class="page-pad">
    <div class="tf-wrap">
      <div class="page-head-row">
        <div>
          <div class="page-title">调用追踪</div>
          <div class="page-sub">问答链路 trace(问题/答案/检索上下文)上送到本空间接入的 Langfuse · 成员看自己的,Admin 及以上可看全空间</div>
        </div>
        <el-radio-group v-if="canManage && configured" v-model="scope" size="small" @change="loadTraces">
          <el-radio-button value="all">全空间</el-radio-button>
          <el-radio-button value="self">仅自己</el-radio-button>
        </el-radio-group>
      </div>

      <!-- trace 列表(未接入时给引导提示) -->
      <div class="card tf-list" data-testid="trace-list">
        <template v-if="configured">
          <div v-if="loadError" class="tf-note tf-err">
            加载失败:{{ loadError }}
            <el-button size="small" @click="loadTraces">重试</el-button>
          </div>
          <el-table
            v-else
            v-loading="loading"
            :data="traces"
            empty-text="暂无 trace(开启上送后,新的问答会出现在这里)"
            @row-click="(r: LangfuseTraceItem) => openDetail(r)"
          >
            <el-table-column label="时间" width="170">
              <template #default="{ row }">{{ fmtTime(row.timestamp) }}</template>
            </el-table-column>
            <el-table-column label="提问" min-width="240">
              <template #default="{ row }">{{ questionOf(row as LangfuseTraceItem) }}</template>
            </el-table-column>
            <el-table-column v-if="canManage && scope === 'all'" label="成员" width="110">
              <template #default="{ row }"><span class="mono">{{ userOf(row as LangfuseTraceItem) }}</span></template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{ fmtLatency(row.latency) }}</template>
            </el-table-column>
            <el-table-column width="80" align="right">
              <template #default="{ row }">
                <el-button link type="primary" @click.stop="openDetail(row as LangfuseTraceItem)">详情</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <div v-else class="tf-note">
          <template v-if="canManage">本空间尚未接入调用追踪,请在下方配置 Langfuse 并开启上送</template>
          <template v-else>本空间尚未接入调用追踪,如需使用请联系空间管理员配置</template>
        </div>
      </div>

      <!-- 接入配置(空间 Admin+ 专属) -->
      <div v-if="canManage && lfConfig" class="card tf-lf" data-testid="langfuse-card">
        <div class="tf-title">Langfuse 接入配置</div>
        <div class="tf-sub">本空间问答 trace 的上送目标(建议每空间一个 Langfuse 项目);未开启时问答零开销</div>

        <div class="tf-ops">
          <el-input v-model="lfHost" class="tf-input" placeholder="Langfuse 地址,如 https://langfuse.internal.example">
            <template #prepend>地址</template>
          </el-input>
          <el-input v-model="lfPublicKey" class="tf-input" placeholder="Public Key(pk-lf-...)">
            <template #prepend>公钥</template>
          </el-input>
          <el-input
            v-model="lfSecretInput"
            type="password"
            show-password
            class="tf-input"
            :placeholder="lfConfig.key_hint ? `已配置 ${lfConfig.key_hint} · 留空 = 不改动` : 'Secret Key(sk-lf-...)'"
          >
            <template #prepend>私钥</template>
          </el-input>
        </div>
        <div v-if="lfConfig.key_hint" class="tf-clear">
          <el-checkbox v-model="lfClearSecret">清除已存密钥</el-checkbox>
        </div>

        <div class="tf-ops tf-switch-row">
          <div class="tf-switch-text">
            <span>上送开关</span>
            <span class="tf-hint">
              {{ lfConfig.enabled ? '已开启' : '已关闭' }}
              <template v-if="lfConfig.updated_at && lfConfig.updated_by_hint">
                · 由 {{ lfConfig.updated_by_hint }} 于 {{ fmtTime(lfConfig.updated_at) }} 变更
              </template>
            </span>
          </div>
          <el-switch
            :model-value="lfConfig.enabled"
            :before-change="onToggleLangfuse"
            :loading="lfSaving"
            data-testid="langfuse-switch"
          />
        </div>

        <div class="tf-save">
          <el-button type="primary" :loading="lfSaving" @click="onSaveLangfuse">保存配置</el-button>
          <a class="tf-link" :href="lfHost || 'https://cloud.langfuse.com'" target="_blank" rel="noopener">
            打开 Langfuse 控制台,在项目 Settings → API Keys 创建密钥
          </a>
        </div>
      </div>
    </div>

    <!-- trace 详情:输入/输出/元数据(Langfuse 单条对象透传,按埋点字段分区展示) -->
    <el-dialog v-model="detailOpen" :title="`trace 详情${detail ? ` · ${detail.id}` : ''}`" width="720px">
      <div v-loading="detailLoading" class="tf-detail">
        <template v-if="detail">
          <div class="tf-d-sec">
            <div class="tf-d-label">输入</div>
            <pre class="tf-d-pre">{{ pretty(detail.data.input) }}</pre>
          </div>
          <div class="tf-d-sec">
            <div class="tf-d-label">输出</div>
            <pre class="tf-d-pre">{{ pretty(detail.data.output) }}</pre>
          </div>
          <div class="tf-d-sec">
            <div class="tf-d-label">元数据</div>
            <pre class="tf-d-pre">{{ pretty(detail.data.metadata) }}</pre>
          </div>
        </template>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.tf-wrap {
  max-width: 980px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}
.tf-note {
  padding: 24px 16px;
  text-align: center;
  font-size: 13px;
  line-height: 1.6;
  color: #6b7280;
}
.tf-err {
  color: #ef4444;
}
.tf-list :deep(.el-table__row) {
  cursor: pointer;
}

.tf-lf {
  padding: 20px;
}
.tf-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.tf-sub {
  margin-top: 3px;
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.5;
}
.tf-ops {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tf-input {
  max-width: 560px;
}
.tf-clear {
  margin-top: 6px;
}
.tf-switch-row {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  max-width: 560px;
}
.tf-switch-text {
  display: flex;
  flex-direction: column;
}
.tf-hint {
  font-size: 12px;
  color: #9ca3af;
}
.tf-save {
  margin-top: 14px;
  display: flex;
  align-items: center;
  gap: 16px;
}
.tf-link {
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  color: #4f6ef2;
  text-decoration: none;
}
.tf-link:hover {
  text-decoration: underline;
}

.tf-detail {
  min-height: 120px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.tf-d-label {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin-bottom: 6px;
}
.tf-d-pre {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #f7f8fa;
  font-size: 12px;
  line-height: 1.6;
  color: #111827;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
</style>
