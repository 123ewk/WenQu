<script setup lang="ts">
import { Plus, RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { apiCreateApiKey, apiListApiKeys, apiRevokeApiKey } from '@/api/apiKeys'
import { ApiError, errMessage } from '@/api/http'
import { apiListKbs } from '@/api/knowledge'
import { apiListPermissionGroups } from '@/api/permissionGroups'
import { apiListMembers } from '@/api/spaces'
import type {
  ApiKeyCreatedOut,
  ApiKeyGroupRef,
  ApiKeyOut,
  CreateApiKeyRequest,
  MemberOut,
  PermissionGroupOut,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDate, fmtRelative } from '@/utils/format'
import { isAtLeast, ROLE, roleLabel } from '@/utils/roles'

/**
 * API Key(OPT-6 签发/列表/吊销;OPT-26 起以成员身份行事;OPT-41 起**权限看档位**)。
 * 端点全挂在空间下:GET 列表(含已吊销,Viewer+)/ POST 创建(Admin+,须给 group_id + owner_user_id)
 * / DELETE 吊销(Admin+,幂等)。
 *
 * OPT-41 的两条口径,页面上必须体现出来:
 * 1. **Key 只是凭据,权限的唯一真相在档位** —— 能力与知识库范围一律读内联的 `group` 快照,
 *    要改权限去「权限档位」页;改一次档位即对其下全部已签发 Key 即时生效,不用重发明文。
 *    一把档位可以签多把 Key,轮换互不影响;
 * 2. `owner_user_id`(旧名 member_user_id)**只决定身份与调用归因,不决定权限**:
 *    此人被移出空间或注销 → Key 在认证阶段即失效(用 `owner_username === null` 判);
 *    此人被降级 → 高权限路由由服务层实时拦下。
 * OPT-31 的 `is_active` / 「设为正在使用」已随迁移 0018 删列消失,"谁当前用哪一档"
 * 改由档位页的 `PUT /permission-groups/{id}/current` 表达。
 * 完整明文只在创建响应出现一次,列表永远只有 key_hint。
 */
const auth = useAuthStore()
const router = useRouter()

const items = ref<ApiKeyOut[]>([])
const kbs = ref<{ id: string; name: string }[]>([])
const members = ref<MemberOut[]>([])
const groups = ref<PermissionGroupOut[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

/** 签发/吊销门槛:Admin+;列表仍 Viewer+ 可看 */
const canManage = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

const CAPABILITY_LABELS: Record<string, string> = {
  chat: '对话检索',
  documents: '文档管理',
}

/** 未映射的能力值原样展示,后端新增能力时不至于渲染成空白 */
function capabilityLabel(value: string): string {
  return CAPABILITY_LABELS[value] ?? value
}

/**
 * 范围口径与档位页保持一致(契约 §1 的 fail-closed 表):
 * all_kbs=true → 全部(实时展开);false + 有勾 → 指定 n 个;false + 空 → **一个都不给**。
 * 旧口径"空数组 = 不限制"已作废,不要把空列表渲染成"全部"。
 */
function kbScopeLabel(group: ApiKeyGroupRef): string {
  if (group.all_kbs) return '全部知识库(实时展开)'
  if (!group.kb_ids.length) return '不含任何知识库'
  const names = group.kb_ids.map((id) => kbs.value.find((k) => k.id === id)?.name)
  if (names.every(Boolean)) return names.join('、')
  const known = names.filter(Boolean).length
  return known ? `${names.filter(Boolean).join('、')} 等 ${group.kb_ids.length} 个` : `${group.kb_ids.length} 个知识库`
}

/* ───── 过滤与排序:两态互斥(未吊销/已吊销)+ 按归因成员分组 + 成员名搜索 ───── */

type AkFilter = 'all' | 'alive' | 'revoked'

const FILTER_OPTIONS: Array<{ value: AkFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'alive', label: '未吊销' },
  { value: 'revoked', label: '已吊销' },
]

const filter = ref<AkFilter>('all')
const ownerQuery = ref('')

const statusCounts = computed(() => ({
  alive: items.value.filter((k) => !k.revoked_at).length,
  revoked: items.value.filter((k) => !!k.revoked_at).length,
}))

const filteredKeys = computed(() => {
  let list = items.value
  if (filter.value === 'alive') list = list.filter((k) => !k.revoked_at)
  if (filter.value === 'revoked') list = list.filter((k) => !!k.revoked_at)
  const q = ownerQuery.value.trim().toLowerCase()
  if (q) {
    list = list.filter((k) => `${k.owner_username ?? ''} ${k.owner_nickname ?? ''}`.toLowerCase().includes(q))
  }
  // 按归因成员聚在一起:成员名升序(已失效沉底),同成员内按创建时间新→旧
  return [...list].sort((a, b) => {
    const ma = a.owner_username ?? ''
    const mb = b.owner_username ?? ''
    if (ma !== mb) {
      if (!ma) return 1
      if (!mb) return -1
      return ma < mb ? -1 : 1
    }
    return (b.created_at ?? '').localeCompare(a.created_at ?? '')
  })
})

/**
 * 归因成员已失效(注销/被移出,后端把 owner_username 置 null)或已吊销:整行置灰。
 * 注意别用 owner_user_id 判 —— 被移出时 id 仍在,只有 username/nickname 变 null。
 */
function isDeadKey(key: ApiKeyOut): boolean {
  return !!key.revoked_at || !key.owner_username
}

function rowClass({ row }: { row: ApiKeyOut }): string {
  return isDeadKey(row) ? 'ak-row-dead' : ''
}

/** 列「归因成员」:昵称(用户名);owner_username 为 null 显示"已注销" */
function ownerLabel(key: ApiKeyOut): string {
  if (!key.owner_username) return '已注销'
  return key.owner_nickname ? `${key.owner_nickname}(${key.owner_username})` : key.owner_username
}

function goPermissionGroups() {
  router.push({ name: 'permission-groups' })
}

/* ───── 数据加载 ───── */

watch(
  () => auth.currentSpaceId,
  async () => {
    filter.value = 'all'
    ownerQuery.value = ''
    await Promise.all([load(), loadKbs(), loadMembers(), loadGroups()])
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
  // 仅用于把档位的 kb_ids 显示成库名;失败不影响列表本身
  try {
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
    // 归因成员下拉数据源;列表本身 Viewer+ 可看,故仅 Admin+ 才拉
    members.value = await apiListMembers(spaceId)
  } catch {
    members.value = []
  }
}

async function loadGroups() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canManage.value) {
    groups.value = []
    return
  }
  try {
    // 创建弹窗的档位下拉 + 权限预览;档位是权限源,读门槛 Viewer+
    groups.value = await apiListPermissionGroups(spaceId)
  } catch {
    groups.value = []
  }
}

/* ───── 新建(两步:表单 → 一次性展示) ───── */

const createOpen = ref(false)
const creating = ref(false)
const created = ref<ApiKeyCreatedOut | null>(null)
const createError = ref('')
const createForm = reactive({
  name: '',
  description: '',
  /** OPT-41 必填:权限档位 id —— 决定这把 Key 能做什么、能看哪些库 */
  groupId: '',
  /** OPT-26 必填:归因成员(只决定身份与用量归因,不决定权限) */
  ownerUserId: '',
})

/** 选中档位后的只读预览:让人在签发前看清"这把 Key 到底能做什么" */
const selectedGroup = computed(() => groups.value.find((g) => g.id === createForm.groupId) ?? null)

const canSubmit = computed(
  () => createForm.name.trim().length > 0 && !!createForm.groupId && !!createForm.ownerUserId && !creating.value,
)

function openCreate() {
  createForm.name = ''
  createForm.description = ''
  createForm.groupId = ''
  // 默认归因到当前用户(必为本空间成员,Admin+ 才能进到这里)
  createForm.ownerUserId = auth.user?.id ?? ''
  createError.value = ''
  created.value = null
  createOpen.value = true
  // 档位可能刚在档位页建好,重开弹窗时补一次
  void loadGroups()
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
      group_id: createForm.groupId,
      owner_user_id: createForm.ownerUserId,
    }
    created.value = await apiCreateApiKey(spaceId, body)
    await load()
  } catch (e) {
    // 档位/成员不属于本空间 → 404;缺字段 → 422。后端 message 已是中文成品,直显
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
    `吊销 Key「${key.name}」?吊销后使用该 Key 的所有程序将立即收到 401 并失去访问权限,此操作不可恢复。` +
      '如需继续使用,请在同一档位下创建新 Key(不影响该档位的其他 Key 与成员)。',
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
</script>

<template>
  <div class="page-pad">
    <div class="ak-wrap">
      <div class="page-head-row">
        <div>
          <div class="page-title">API Key</div>
          <div class="page-sub">程序化接入凭据;能做什么、能看哪些库由所属<b>权限档位</b>决定</div>
        </div>
        <el-button v-if="canManage" type="primary" data-testid="key-create-open" @click="openCreate">
          <el-icon class="btn-ic"><Plus /></el-icon>新建 Key
        </el-button>
      </div>

      <div class="ak-note">
        <el-icon class="ak-note-ic"><WarningFilled /></el-icon>
        <span>
          Key 用于程序化接入,调用时以 <span class="mono">X-API-Key</span> 请求头携带(不是 Bearer)。
          完整 Key 只在创建时展示一次,一旦泄露请立即吊销并在同一档位下新建。
          <b>Key 本身不带权限</b>:能力与知识库范围都在<b>权限档位</b>上,要改权限去
          <el-link type="primary" class="ak-link" @click="goPermissionGroups">权限档位</el-link>
          页 —— 改一次档位,其下全部已签发的 Key <b>即时生效</b>,不用重发明文;一把档位可以签多把 Key,轮换互不影响。
          每把 Key 另有一名<b>归因成员</b>(只决定身份与用量归因):此人被移出空间或注销,Key 立即失效;签发人离开不影响 Key。
        </span>
      </div>

      <!-- 过滤与搜索:两态互斥 + 按归因成员名模糊搜索 -->
      <div v-if="!loadError && items.length" class="ak-toolbar">
        <el-radio-group v-model="filter">
          <el-radio-button v-for="opt in FILTER_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
            <span v-if="opt.value !== 'all'" class="ak-tab-count">{{ statusCounts[opt.value as 'alive' | 'revoked'] }}</span>
          </el-radio-button>
        </el-radio-group>
        <el-input
          v-model="ownerQuery"
          class="ak-search"
          placeholder="搜索归因成员"
          clearable
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
          <path d="M48 40l22 22m0 0h-8m8 0v-8" stroke="var(--el-color-primary)" />
          <circle cx="34" cy="26" r="2" fill="#9CA3AF" stroke="none" />
        </svg>
        <div class="state-title">还没有 API Key</div>
        <div class="state-desc">先在「权限档位」页建一档,再在档下签发 Key,程序即可调用检索与对话接口</div>
        <el-button v-if="canManage" type="primary" class="state-btn" @click="openCreate">
          <el-icon class="btn-ic"><Plus /></el-icon>新建 Key
        </el-button>
        <div v-else class="state-desc role-hint">当前角色无权创建 API Key(创建/吊销需 Admin 及以上,列表 Viewer+ 可看)</div>
      </div>

      <!-- 列表 -->
      <div v-else class="card table-card" v-loading="loading">
        <el-table :data="filteredKeys" :row-class-name="rowClass" style="width: 100%">
          <el-table-column label="名称" min-width="180">
            <template #default="{ row }">
              <div class="ak-name">{{ row.name }}</div>
              <div v-if="row.description" class="ak-desc truncate">{{ row.description }}</div>
            </template>
          </el-table-column>
          <el-table-column label="Key" width="180">
            <template #default="{ row }">
              <span class="mono ak-hint">{{ row.key_hint }}</span>
            </template>
          </el-table-column>
          <el-table-column label="所属档位" min-width="150">
            <template #default="{ row }">
              <el-link type="primary" class="ak-group" @click="goPermissionGroups">{{ row.group.name }}</el-link>
            </template>
          </el-table-column>
          <el-table-column label="归因成员" min-width="170">
            <template #default="{ row }">
              <span v-if="!row.owner_username" class="ak-revoked">已注销</span>
              <span v-else>
                <span class="ak-member">{{ ownerLabel(row as ApiKeyOut) }}</span>
                <span v-if="row.owner_role != null" class="ak-member-role">{{ roleLabel(row.owner_role) }}</span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="签发人" width="120">
            <template #default="{ row }">
              <span class="cell-dim">{{ row.created_by_name || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="能力(来自档位)" min-width="170">
            <template #default="{ row }">
              <span v-if="!row.group.capabilities.length" class="ak-cap-gray">无能力</span>
              <span v-for="cap in row.group.capabilities" :key="cap" class="ak-cap">{{ capabilityLabel(cap) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="允许的知识库(来自档位)" min-width="180">
            <template #default="{ row }">
              <span v-if="!row.group.all_kbs && !row.group.kb_ids.length" class="ak-cap-gray">不含任何知识库</span>
              <span v-else class="ak-scope">{{ kbScopeLabel(row.group as ApiKeyGroupRef) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="110">
            <template #default="{ row }">
              <span class="cell-dim">{{ fmtDate(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="最近使用" width="110">
            <template #default="{ row }">
              <span v-if="!row.last_used_at" class="cell-dim">从未使用</span>
              <span v-else class="cell-dim">{{ fmtRelative(row.last_used_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" align="right">
            <template #default="{ row }">
              <span v-if="row.revoked_at" class="ak-revoked">已吊销</span>
              <el-button v-else-if="canManage" link type="danger" @click="onRevoke(row as ApiKeyOut)">吊销</el-button>
              <span v-else class="cell-dim">—</span>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="items.length" class="ak-foot">
          共 {{ items.length }} 个({{ statusCounts.alive }} 个可用 · {{ statusCounts.revoked }} 个已吊销)<span
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
      <!-- 步骤 1:表单(Key 不带权限,只有 名称/描述/档位/归因成员 四项) -->
      <template v-if="!created">
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="名称" required>
            <el-input
              v-model="createForm.name"
              maxlength="64"
              placeholder="例如:ci-pipeline"
              data-testid="key-name"
            />
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="createForm.description" maxlength="512" placeholder="用途说明(选填)" />
          </el-form-item>
          <el-form-item label="权限档位" required>
            <el-select
              v-model="createForm.groupId"
              class="ak-select"
              placeholder="选择本空间的现存档位"
              data-testid="key-group"
            >
              <el-option v-for="g in groups" :key="g.id" :label="g.name" :value="g.id">
                <div class="ak-opt-row">
                  <span>{{ g.name }}</span>
                  <span class="ak-opt-sub">
                    {{ g.capabilities.length ? g.capabilities.map(capabilityLabel).join('、') : '无能力' }}
                  </span>
                </div>
              </el-option>
            </el-select>
            <div v-if="!groups.length" class="ak-err">
              本空间还没有权限档位 —— Key 必须先挂在一档上才能签发,请先去
              <el-link type="primary" class="ak-link" @click="goPermissionGroups">权限档位</el-link> 建档
            </div>
            <!-- 只读预览:签发前让人看清这把 Key 到底能做什么、能看哪些库 -->
            <div v-else-if="selectedGroup" class="ak-preview">
              <div class="ak-preview-title">{{ selectedGroup.name }} 的权限(改权限请去档位页)</div>
              <div class="ak-preview-row">
                <span class="ak-preview-label">能力</span>
                <span v-if="!selectedGroup.capabilities.length" class="ak-cap-gray">无能力(什么都不能做)</span>
                <span v-for="cap in selectedGroup.capabilities" :key="cap" class="ak-cap">{{ capabilityLabel(cap) }}</span>
              </div>
              <div class="ak-preview-row">
                <span class="ak-preview-label">知识库</span>
                <span v-if="!selectedGroup.all_kbs && !selectedGroup.kb_ids.length" class="ak-cap-gray">不含任何知识库</span>
                <span v-else class="ak-scope">{{ kbScopeLabel(selectedGroup) }}</span>
              </div>
            </div>
          </el-form-item>
          <el-form-item label="归因成员" required>
            <el-select
              v-model="createForm.ownerUserId"
              class="ak-select"
              placeholder="选择该空间的现存成员"
              data-testid="key-owner"
            >
              <el-option
                v-for="m in members"
                :key="m.user_id"
                :label="`${m.nickname}(${m.username}) · ${roleLabel(m.role)}`"
                :value="m.user_id"
              />
            </el-select>
            <div class="ak-hint-line">
              Key 以该成员的身份行事(调用归因、消息归属都落到 TA 头上):此人被移出空间或注销后 Key 立即失效;
              <b>TA 的角色不决定这把 Key 的权限范围</b>,范围只看档位。
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
          <div class="mono ak-keybox-value" data-testid="key-secret">{{ created.plaintext }}</div>
          <button class="ak-copy" data-testid="key-copy" @click="copyPlaintext">复制</button>
        </div>
        <div class="ak-warn">
          <el-icon class="ak-warn-ic"><WarningFilled /></el-icon>
          关闭后无法再次查看完整 Key,请立即复制并妥善保存。它的权限来自档位「{{ created.group.name }}」,改权限去档位页即可,无需重发这把 Key。
        </div>
      </template>

      <template #footer>
        <template v-if="!created">
          <el-button @click="createOpen = false">取消</el-button>
          <el-button
            type="primary"
            :loading="creating"
            :disabled="!canSubmit"
            data-testid="key-submit"
            @click="onCreate"
          >
            创建 Key
          </el-button>
        </template>
        <el-button v-else type="primary" data-testid="key-created-close" @click="onCreatedClose">
          我已保存
        </el-button>
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
.ak-link {
  vertical-align: baseline;
}

.table-card {
  overflow: hidden;
  min-height: 200px;
}
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
.ak-group {
  font-size: 13px;
}
.ak-cap {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  margin-right: 4px;
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
  font-size: 11px;
  color: var(--el-color-primary);
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
/* 已吊销 / 归因成员已注销:整行置灰 */
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

.ak-select {
  width: 100%;
}
.ak-opt-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.ak-opt-sub {
  font-size: 11px;
  color: #9ca3af;
}
.ak-preview {
  width: 100%;
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f7f8fa;
}
.ak-preview-title {
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 8px;
}
.ak-preview-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  line-height: 1.6;
}
.ak-preview-row + .ak-preview-row {
  margin-top: 4px;
}
.ak-preview-label {
  flex-shrink: 0;
  width: 52px;
  font-size: 12px;
  color: #9ca3af;
}
.ak-hint-line {
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
