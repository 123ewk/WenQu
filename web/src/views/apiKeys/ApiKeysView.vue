<script setup lang="ts">
import { Plus, RefreshRight, Search, WarningFilled } from '@element-plus/icons-vue'

import { apiActivateApiKey, apiCreateApiKey, apiListApiKeys, apiRevokeApiKey } from '@/api/apiKeys'
import { ApiError, errMessage } from '@/api/http'
import { apiListKbs } from '@/api/knowledge'
import { apiListMembers } from '@/api/spaces'
import type { ApiCapability, ApiKeyCreatedOut, ApiKeyOut, CreateApiKeyRequest, MemberOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDate, fmtRelative } from '@/utils/format'
import { isAtLeast, ROLE, roleLabel } from '@/utils/roles'

/**
 * API Key(原型 07,OPT-6 + OPT-26 + OPT-31)。端点全挂在空间下:
 * GET 列表(含已吊销,Viewer+)/ POST 创建(Admin+,须绑定成员)/ DELETE 吊销(Admin+,幂等)
 * / PUT activate(Admin+,设为「正在使用」)。
 * OPT-26 语义:Key 以**绑定成员**身份行事 —— 绑定成员被移出/注销 → Key 立即 401;
 * 签发者被移出不影响 Key;成员被降级 → Key 权限实时收敛。
 * OPT-31 语义:`is_active`「正在使用」是 Admin+ 的分配标记,同成员至多一把;
 * **纯管理语义** —— 未标记的未吊销 Key 仍可正常调用(成员换着用,后端不拦)。
 * 完整明文只在创建响应出现,列表永远只有 key_hint —— 见 m3-api-keys-接口说明.md。
 */
const auth = useAuthStore()

const items = ref<ApiKeyOut[]>([])
const kbs = ref<{ id: string; name: string }[]>([])
const members = ref<MemberOut[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

/** 创建/吊销/设为正在使用门槛(OPT-26/31):Admin+;列表仍 Viewer+ 可看 */
const canManage = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

const CAPABILITY_LABELS: Record<string, string> = {
  chat: '对话检索',
  documents: '文档管理',
}

/** 两个能力与原型页 07 的勾选项一一对应(chat 同时覆盖 /ask 与 /search) */
const CAPABILITY_OPTIONS: Array<{ value: ApiCapability; desc: string }> = [
  { value: 'chat', desc: '调用流式问答与检索接口' },
  { value: 'documents', desc: '上传、删除文档与触发重新解析' },
]

/** el-checkbox-group 的值为字符串数组,恰好与契约的 list[str] 一致 */
const createForm = reactive({
  name: '',
  description: '',
  /** OPT-26 必填:绑定的空间成员 user_id,默认选中当前用户 */
  memberUserId: '',
  capabilities: ['chat'] as string[],
  kbIds: [] as string[],
})

/**
 * 「全部」与条目双向联动(原型 07 toggleAllKeyKb/syncKbAll 口径):
 * 全勾 = 全部;提交时全勾折算成空数组(契约:空 = 不限库)。
 * ⚠️ 绝不能让"零勾选"落到 kb_ids=[] —— 那在契约里等于"全部",会出现"什么都不选=全放行"的反向语义,
 * 故 canSubmit 要求至少勾一个。
 */
const allKbs = computed(() => kbs.value.length > 0 && createForm.kbIds.length === kbs.value.length)
const someKbs = computed(() => createForm.kbIds.length > 0 && !allKbs.value)

function toggleAllKbs(value: boolean) {
  createForm.kbIds = value ? kbs.value.map((k) => k.id) : []
}

/** 未映射的能力值原样展示,不隐藏(后端新增能力时不至于渲染成空白) */
function capabilityLabel(value: string): string {
  return CAPABILITY_LABELS[value] ?? value
}

/** kb_ids 为空 = 全部知识库;否则解析名称,取不到的退化为数量 */
function kbScopeLabel(key: ApiKeyOut): string {
  if (!key.kb_ids.length) return '全部知识库'
  const names = key.kb_ids.map((id) => kbs.value.find((k) => k.id === id)?.name)
  if (names.every(Boolean)) return names.join('、')
  const known = names.filter(Boolean).length
  return known ? `${names.filter(Boolean).join('、')} 等 ${key.kb_ids.length} 个` : `${key.kb_ids.length} 个知识库`
}

/* ───── OPT-31 过滤与排序:四档互斥 + 按成员分组 + 成员名搜索 ───── */

type AkFilter = 'all' | 'active' | 'unused' | 'revoked'
type AkStatus = Exclude<AkFilter, 'all'>

const FILTER_OPTIONS: Array<{ value: AkFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '正在使用' },
  { value: 'unused', label: '未使用' },
  { value: 'revoked', label: '已吊销' },
]

const filter = ref<AkFilter>('all')
const memberQuery = ref('')

/** 三态互斥:已吊销优先判定(吊销 Key 即便残留 is_active 也归"已吊销"档) */
function keyStatus(key: ApiKeyOut): AkStatus {
  if (key.revoked_at) return 'revoked'
  return key.is_active ? 'active' : 'unused'
}

const statusCounts = computed(() => {
  const counts: Record<AkStatus, number> = { active: 0, unused: 0, revoked: 0 }
  for (const key of items.value) counts[keyStatus(key)] += 1
  return counts
})

const filteredKeys = computed(() => {
  let list = items.value
  if (filter.value !== 'all') list = list.filter((k) => keyStatus(k) === filter.value)
  const q = memberQuery.value.trim().toLowerCase()
  if (q) {
    list = list.filter((k) =>
      `${k.member_username ?? ''} ${k.member_nickname ?? ''}`.toLowerCase().includes(q),
    )
  }
  // 按成员聚在一起:成员名升序(已注销沉底),同成员内生效者优先、再按创建时间新→旧
  return [...list].sort((a, b) => {
    const ma = a.member_username ?? ''
    const mb = b.member_username ?? ''
    if (ma !== mb) {
      if (!ma) return 1
      if (!mb) return -1
      return ma < mb ? -1 : 1
    }
    if (a.is_active !== b.is_active) return a.is_active ? -1 : 1
    return (b.created_at ?? '').localeCompare(a.created_at ?? '')
  })
})

/**
 * 绑定成员已失效(注销/被移出,实测后端把 member_username 置 null)或已吊销:整行置灰。
 * 注意别用 member_user_id 判 —— 被移出时 id 仍在,只有 username/nickname 变 null。
 */
function isDeadKey(key: ApiKeyOut): boolean {
  return !!key.revoked_at || !key.member_username
}

function rowClass({ row }: { row: ApiKeyOut }): string {
  return isDeadKey(row) ? 'ak-row-dead' : ''
}

/** 列"绑定成员":昵称(用户名);member_username 为 null(注销/被移出)显示"已注销" */
function memberLabel(key: ApiKeyOut): string {
  if (!key.member_username) return '已注销'
  return key.member_nickname ? `${key.member_nickname}(${key.member_username})` : key.member_username
}

/* ───── 数据加载 ───── */

watch(
  () => auth.currentSpaceId,
  async () => {
    filter.value = 'all'
    memberQuery.value = ''
    await Promise.all([load(), loadKbs(), loadMembers()])
  },
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
    items.value = await apiListApiKeys(spaceId)
  } catch (e) {
    loadError.value = errMessage(e)
    items.value = []
  } finally {
    loading.value = false
  }
}

async function loadKbs() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    kbs.value = []
    return
  }
  try {
    // 仅用于把 kb_ids 显示成名称;失败不影响列表本身
    kbs.value = await apiListKbs(spaceId)
  } catch {
    kbs.value = []
  }
}

async function loadMembers() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canManage.value) {
    members.value = []
    return
  }
  try {
    // 绑定成员下拉数据源;列表本身 Viewer+ 可看,故仅 Admin+ 才拉
    members.value = await apiListMembers(spaceId)
  } catch {
    members.value = []
  }
}

/* ───── 新建(两步:表单 → 一次性展示) ───── */

const createOpen = ref(false)
const creating = ref(false)
const created = ref<ApiKeyCreatedOut | null>(null)
const createError = ref('')

const canSubmit = computed(
  () =>
    createForm.name.trim().length > 0 &&
    !!createForm.memberUserId &&
    // 有库时必须至少勾一个:零勾选折算成 kb_ids=[] 会被契约解释成「全部」(反向语义),须挡在提交前
    (kbs.value.length === 0 || createForm.kbIds.length > 0) &&
    !creating.value,
)

function openCreate() {
  createForm.name = ''
  createForm.description = ''
  // 默认选中当前用户(必为本空间成员,Admin+ 才能进到这里)
  createForm.memberUserId = auth.user?.id ?? ''
  createForm.capabilities = ['chat']
  createForm.kbIds = kbs.value.map((k) => k.id)
  createError.value = ''
  created.value = null
  createOpen.value = true
}

async function onCreate() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canSubmit.value) return
  creating.value = true
  createError.value = ''
  try {
    const body: CreateApiKeyRequest = {
      name: createForm.name.trim(),
      description: createForm.description.trim(),
      member_user_id: createForm.memberUserId,
      capabilities: createForm.capabilities,
      // 全选折算成空数组(契约语义:不限制);部分选中提交其 id;零勾选已被 canSubmit 挡住
      kb_ids: allKbs.value ? [] : createForm.kbIds,
    }
    created.value = await apiCreateApiKey(spaceId, body)
    await load()
  } catch (e) {
    // 未知能力名是 422,后端 message 已列出允许值,直接展示即可
    createError.value = e instanceof ApiError ? e.message : errMessage(e)
  } finally {
    creating.value = false
  }
}

function onCreatedClose() {
  createOpen.value = false
  created.value = null
}

async function copyPlaintext() {
  const text = created.value?.plaintext
  if (!text) return
  try {
    await navigator.clipboard?.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败,请手动选择文本')
  }
}

/* ───── 吊销 ───── */

async function onRevoke(key: ApiKeyOut) {
  const confirmed = await ElMessageBox.confirm(
    `吊销 Key「${key.name}」?吊销后使用该 Key 的所有程序将立即收到 401 并失去访问权限,此操作不可恢复。如需继续使用,请创建新 Key。`,
    '吊销 API Key',
    { type: 'warning', confirmButtonText: '确认吊销', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  try {
    await apiRevokeApiKey(spaceId, key.id)
    ElMessage.success('Key 已吊销')
    await load()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'FORBIDDEN') ElMessage.warning('当前角色无权吊销 API Key(需 Admin 及以上)')
    else if (e instanceof ApiError && e.code === 'NOT_FOUND') ElMessage.warning('该 Key 已不存在')
    else ElMessage.error(errMessage(e))
  }
}

/* ───── 设为「正在使用」(OPT-31,Admin+ 分配,同成员互斥) ───── */

const activatingId = ref('')

async function onActivate(key: ApiKeyOut) {
  const confirmed = await ElMessageBox.confirm(
    `将 Key「${key.name}」设为成员「${memberLabel(key)}」正在使用的 Key?` +
      '该成员当前的生效 Key 会自动变为未使用。「正在使用」只是管理标识,其余未吊销 Key 仍可正常调用。',
    '设为正在使用',
    { type: 'info', confirmButtonText: '确认设置', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  activatingId.value = key.id
  try {
    await apiActivateApiKey(spaceId, key.id)
    ElMessage.success('已设为正在使用')
    await load()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'FORBIDDEN') ElMessage.warning('当前角色无权分配生效 Key(需 Admin 及以上)')
    else if (e instanceof ApiError && e.code === 'NOT_FOUND') ElMessage.warning('该 Key 已不存在')
    else if (e instanceof ApiError && e.code === 'VALIDATION_ERROR') ElMessage.warning(errMessage(e))
    else ElMessage.error(errMessage(e))
  } finally {
    activatingId.value = ''
  }
}
</script>

<template>
  <div class="page-pad">
    <div class="ak-wrap">
      <div class="page-head-row">
        <div>
          <div class="page-title">API Key</div>
          <div class="page-sub">供程序化接入,可限定能力与知识库范围</div>
        </div>
        <el-button v-if="canManage" type="primary" @click="openCreate">
          <el-icon class="btn-ic"><Plus /></el-icon>新建 Key
        </el-button>
      </div>

      <div class="ak-note">
        <el-icon class="ak-note-ic"><WarningFilled /></el-icon>
        <span>
          Key 用于程序化接入,请妥善保管。完整 Key 只在创建时展示一次;一旦泄露请立即吊销并新建。
          调用时以 <span class="mono">X-API-Key</span> 请求头携带(不是 Bearer)。
          每个 Key 绑定一名空间成员并以其身份行事:<b>绑定成员</b>被移出空间或注销,Key 立即失效;签发人离开不影响 Key。
          每位成员由管理员标一把<b>正在使用</b>的 Key 作辨识;其余未吊销 Key 仍可正常调用(可换着用)。
        </span>
      </div>

      <!-- 过滤与搜索(OPT-31):四档互斥 + 按成员名模糊搜索 -->
      <div v-if="!loadError && items.length" class="ak-toolbar">
        <el-radio-group v-model="filter">
          <el-radio-button v-for="opt in FILTER_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
            <span v-if="opt.value !== 'all'" class="ak-tab-count">{{ statusCounts[opt.value as AkStatus] }}</span>
          </el-radio-button>
        </el-radio-group>
        <el-input
          v-model="memberQuery"
          class="ak-search"
          placeholder="搜索成员名字"
          clearable
          :prefix-icon="Search"
        />
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
          <circle cx="38" cy="30" r="14" />
          <path d="M48 40l22 22m0 0h-8m8 0v-8" stroke="#4F6EF2" />
          <circle cx="34" cy="26" r="2" fill="#9CA3AF" stroke="none" />
        </svg>
        <div class="state-title">还没有 API Key</div>
        <div class="state-desc">创建 Key 后,即可在程序中调用检索与对话接口</div>
        <el-button v-if="canManage" type="primary" class="state-btn" @click="openCreate">
          <el-icon class="btn-ic"><Plus /></el-icon>新建 Key
        </el-button>
        <div v-else class="state-desc role-hint">当前角色无权创建 API Key(创建/吊销需 Admin 及以上,列表 Viewer+ 可看)</div>
      </div>

      <!-- 列表 -->
      <div v-else class="card table-card" v-loading="loading">
        <el-table :data="filteredKeys" :row-class-name="rowClass" style="width: 100%">
          <el-table-column label="名称" min-width="200">
            <template #default="{ row }">
              <div class="ak-name">
                {{ row.name }}
                <span v-if="!row.revoked_at && row.is_active" class="ak-badge">正在使用</span>
              </div>
              <div v-if="row.description" class="ak-desc truncate">{{ row.description }}</div>
            </template>
          </el-table-column>
          <el-table-column label="Key" width="200">
            <template #default="{ row }">
              <span class="mono ak-hint">{{ row.key_hint }}</span>
            </template>
          </el-table-column>
          <el-table-column label="绑定成员" min-width="170">
            <template #default="{ row }">
              <span v-if="!row.member_username" class="ak-revoked">已注销</span>
              <span v-else>
                <span class="ak-member">{{ memberLabel(row as ApiKeyOut) }}</span>
                <span v-if="row.member_role != null" class="ak-member-role">{{ roleLabel(row.member_role) }}</span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="签发人" width="130">
            <template #default="{ row }">
              <span class="cell-dim">{{ row.created_by_name || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="能力" min-width="180">
            <template #default="{ row }">
              <span v-if="!row.capabilities.length" class="ak-cap-gray">无能力</span>
              <span v-for="cap in row.capabilities" :key="cap" class="ak-cap">{{ capabilityLabel(cap) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="允许的知识库" min-width="180">
            <template #default="{ row }">
              <span class="ak-scope">{{ kbScopeLabel(row as ApiKeyOut) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="120">
            <template #default="{ row }">
              <span class="cell-dim">{{ fmtDate(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="最近使用" width="120">
            <template #default="{ row }">
              <span v-if="!row.last_used_at" class="cell-dim">从未使用</span>
              <span v-else class="cell-dim">{{ fmtRelative(row.last_used_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150" align="right">
            <template #default="{ row }">
              <span v-if="row.revoked_at" class="ak-revoked">已吊销</span>
              <template v-else-if="canManage">
                <span v-if="row.is_active" class="cell-dim">使用中</span>
                <el-button
                  v-else
                  link
                  type="primary"
                  :loading="activatingId === row.id"
                  @click="onActivate(row as ApiKeyOut)"
                >
                  设为正在使用
                </el-button>
                <el-button link type="danger" @click="onRevoke(row as ApiKeyOut)">吊销</el-button>
              </template>
              <span v-else class="cell-dim">—</span>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="items.length" class="ak-foot">
          共 {{ items.length }} 个({{ statusCounts.active }} 个正在使用 ·
          {{ statusCounts.unused }} 个未使用 · {{ statusCounts.revoked }} 个已吊销)<span
            v-if="filteredKeys.length !== items.length"
          >
            · 当前筛选显示 {{ filteredKeys.length }} 个</span
          >
        </div>
      </div>
    </div>

    <!-- 新建 Key:两步弹窗(表单 → 一次性展示) -->
    <el-dialog
      v-model="createOpen"
      :title="created ? 'Key 已创建' : '新建 API Key'"
      width="480px"
      :close-on-click-modal="!!created"
      append-to-body
      @closed="created = null"
    >
      <!-- 步骤 1:表单 -->
      <template v-if="!created">
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="名称" required>
            <el-input v-model="createForm.name" maxlength="64" placeholder="例如:ci-pipeline" />
          </el-form-item>
          <el-form-item label="绑定成员" required>
            <el-select v-model="createForm.memberUserId" class="ak-member-select" placeholder="选择该空间的现存成员">
              <el-option
                v-for="m in members"
                :key="m.user_id"
                :label="`${m.nickname}(${m.username}) · ${roleLabel(m.role)}`"
                :value="m.user_id"
              />
            </el-select>
            <div class="ak-member-hint">Key 将以该成员的身份行事:成员被移出空间或注销,Key 立即失效</div>
          </el-form-item>
          <el-form-item label="描述">
            <el-input
              v-model="createForm.description"
              maxlength="512"
              placeholder="用途说明(选填)"
            />
          </el-form-item>
          <el-form-item label="能力">
            <el-checkbox-group v-model="createForm.capabilities" class="ak-caps">
              <label v-for="opt in CAPABILITY_OPTIONS" :key="opt.value" class="ak-cap-opt">
                <el-checkbox :value="opt.value" />
                <span>
                  <span class="ak-cap-title">{{ capabilityLabel(opt.value) }}</span>
                  <span class="ak-cap-sub">{{ opt.desc }}</span>
                </span>
              </label>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item class="ak-scope-item">
            <template #label>
              <div class="ak-scope-head">
                <span>允许的知识库</span>
                <el-checkbox
                  :model-value="allKbs"
                  :indeterminate="someKbs"
                  @update:model-value="(v: any) => toggleAllKbs(Boolean(v))"
                >
                  全部
                </el-checkbox>
              </div>
            </template>
            <el-checkbox-group v-model="createForm.kbIds" class="ak-kb-grid">
              <label v-for="kb in kbs" :key="kb.id" class="ak-kb-opt">
                <el-checkbox :value="kb.id" />
                <span class="truncate">{{ kb.name }}</span>
              </label>
              <div v-if="!kbs.length" class="cell-dim">当前空间还没有知识库</div>
            </el-checkbox-group>
            <div v-if="kbs.length && !createForm.kbIds.length" class="ak-err ak-scope-err">
              至少选择一个知识库(全不选会被视为"全部知识库",与预期相反)
            </div>
          </el-form-item>
        </el-form>
        <div v-if="createError" class="ak-err">{{ createError }}</div>
      </template>

      <!-- 步骤 2:一次性展示 -->
      <template v-else>
        <div class="ak-created-sub">请立即复制保存,这是唯一一次完整展示。</div>
        <div class="ak-keybox">
          <div class="ak-keybox-label mono">X-API-Key</div>
          <div class="mono ak-keybox-value">{{ created.plaintext }}</div>
          <button class="ak-copy" @click="copyPlaintext">复制</button>
        </div>
        <div class="ak-warn">
          <el-icon class="ak-warn-ic"><WarningFilled /></el-icon>
          关闭后无法再次查看完整 Key,请立即复制并妥善保存。
        </div>
      </template>

      <template #footer>
        <template v-if="!created">
          <el-button @click="createOpen = false">取消</el-button>
          <el-button type="primary" :loading="creating" :disabled="!canSubmit" @click="onCreate">
            创建 Key
          </el-button>
        </template>
        <el-button v-else type="primary" @click="onCreatedClose">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.btn-ic {
  margin-right: 4px;
}
.ak-wrap {
  max-width: 1240px;
}
.role-hint {
  margin-top: 12px;
}

.ak-note {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 16px;
  margin-bottom: 20px;
  border-radius: 8px;
  background: #f7f8fa;
  font-size: 13px;
  line-height: 1.6;
  color: #6b7280;
}
.ak-note-ic {
  margin-top: 2px;
  color: #9ca3af;
  flex-shrink: 0;
}

.table-card {
  overflow: hidden;
  min-height: 200px;
}
/* OPT-31 过滤工具条 */
.ak-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.ak-search {
  width: 220px;
}
.ak-tab-count {
  margin-left: 4px;
  font-size: 11px;
  opacity: 0.7;
}
.ak-badge {
  display: inline-flex;
  align-items: center;
  height: 18px;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 6px;
  background: rgba(16, 185, 129, 0.1);
  font-size: 11px;
  font-weight: 400;
  color: #059669;
  white-space: nowrap;
  vertical-align: 2px;
}
.ak-name {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.ak-desc {
  margin-top: 2px;
  font-size: 11px;
  color: #9ca3af;
}
.ak-hint {
  display: inline-block;
  padding: 2px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  background: #f7f8fa;
  font-size: 13px;
  color: #374151;
}
.ak-cap {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  margin-right: 4px;
  border-radius: 6px;
  background: #eef1fe;
  font-size: 11px;
  color: #4f6ef2;
  white-space: nowrap;
}
.ak-cap-gray {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  font-size: 11px;
  color: #9ca3af;
}
.ak-scope {
  font-size: 13px;
  color: #374151;
}
.cell-dim {
  font-size: 13px;
  color: #6b7280;
}
.ak-member {
  font-size: 13px;
  color: #374151;
}
.ak-member-role {
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
.ak-revoked {
  font-size: 12px;
  color: #9ca3af;
}
/* 已吊销 / 绑定成员已注销:整行置灰 */
.table-card :deep(.ak-row-dead) {
  opacity: 0.5;
}
.ak-foot {
  height: 48px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  border-top: 1px solid #e5e7eb;
  font-size: 12px;
  color: #9ca3af;
}

/*
 * Element Plus 给 .el-checkbox-group 设了 font-size:0 / line-height:0(消除勾选项之间的空白间隙),
 * 我们往组里放了自定义文案,若不重置就会继承这两个 0 —— line-height 计算值是长度会一并继承为 0,
 * 行盒高度塌成 0,多行文字便叠在同一位置。scoped 属性选择器优先级高于 .el-checkbox-group,可覆盖。
 */
.ak-caps,
.ak-kb-grid {
  font-size: 14px;
  line-height: 1.5;
}
.ak-caps {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ak-cap-opt {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  cursor: pointer;
}
.ak-cap-opt:hover {
  background: #f7f8fa;
}
.ak-cap-title {
  display: block;
  font-size: 13px;
  color: #111827;
}
.ak-cap-sub {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: #9ca3af;
}
.ak-scope-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}
/* label-top 模式下 label 默认按内容收缩,撑满才能让「全部」靠右(与原型一致) */
.ak-scope-item :deep(.el-form-item__label) {
  width: 100%;
}
.ak-kb-grid {
  width: 100%;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  max-height: 148px;
  overflow-y: auto;
}.ak-kb-opt {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 10px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  font-size: 13px;
  color: #374151;
  cursor: pointer;
}
.ak-kb-opt:hover {
  background: #f7f8fa;
}

.ak-member-select {
  width: 100%;
}
.ak-member-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: #9ca3af;
}

.ak-created-sub {
  font-size: 13px;
  color: #6b7280;
}
.ak-keybox {
  position: relative;
  margin-top: 16px;
  padding: 14px 16px;
  border-radius: 8px;
  background: #111827;
}
.ak-keybox-label {
  font-size: 11px;
  color: #9ca3af;
  margin-bottom: 6px;
}
.ak-keybox-value {
  font-size: 13px;
  line-height: 1.6;
  color: #e5e7eb;
  word-break: break-all;
}
.ak-copy {
  position: absolute;
  top: 36px;
  right: 10px;
  height: 28px;
  padding: 0 8px;
  border: none;
  border-radius: 6px;
  background: #1f2937;
  color: #e5e7eb;
  font-size: 12px;
  cursor: pointer;
}
.ak-copy:hover {
  background: #374151;
}
.ak-warn {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 16px;
  padding: 10px 12px;
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 8px;
  background: rgba(239, 68, 68, 0.05);
  font-size: 13px;
  line-height: 1.6;
  color: #ef4444;
}
.ak-warn-ic {
  margin-top: 3px;
  flex-shrink: 0;
}
.ak-err {
  margin-top: 4px;
  font-size: 13px;
  color: #ef4444;
}
</style>
