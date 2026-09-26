<script setup lang="ts">
import { Plus, RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { ApiError, errMessage } from '@/api/http'
import { apiListKbs } from '@/api/knowledge'
import {
  apiAssignGroupMember,
  apiCreatePermissionGroup,
  apiDeletePermissionGroup,
  apiGetSpaceToggle,
  apiListPermissionGroups,
  apiPurgeGroupRevokedKeys,
  apiPutSpaceToggle,
  apiSetCurrentGroup,
  apiUnassignGroupMember,
  apiUpdatePermissionGroup,
  groupDeleteBlockedDetails,
} from '@/api/permissionGroups'
import { apiListMembers } from '@/api/spaces'
import type {
  GroupMemberOut,
  MemberOut,
  PermissionGroupOut,
  SpaceToggleStatusOut,
  UpsertPermissionGroupRequest,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDate, fmtDateTime } from '@/utils/format'
import { isAtLeast, ROLE, roleLabel } from '@/utils/roles'

/**
 * 权限档位(OPT-41)—— 一档 = "能做什么能力 + 能看哪些知识库",人和 API Key 共用同一份真相。
 *
 * 端点全挂在空间下且**只认 JWT**(带 API Key 打过来是 401 AUTH_REQUIRED,与未登录同构 ——
 * 改权限不能由凭据自己发起):列表/详情 Viewer+,建档·改档·删档·分配·解除·换档一律 Admin+。
 *
 * 三条必须落到 UI 上的口径:
 * 1. 权限的唯一真相在这里 —— 改一次档位,其下全部已签发 Key 与新范围即时生效(不用重发明文);
 * 2. fail-closed —— 能力为空 = 什么都不能做;`all_kbs=false` 且没勾库 = 一个库都不给。
 *    "放开全部库"必须显式勾选,漏填不能变成放开整个空间;
 * 3. 换档是权限开关(取代 OPT-31 的「正在使用」台账标记)—— 每人每空间至多一档 current,
 *    解除当前档会让此人立即没有生效档位(后端不自动换一档),灰度开启后问答直接 403。
 */
const auth = useAuthStore()

const groups = ref<PermissionGroupOut[]>([])
const kbs = ref<{ id: string; name: string }[]>([])
/** 灰度开关开启后,自己没挂档位时读库列表会 403 —— 这时不是"空间没有库",不能说成没有库 */
const kbsDeniedByGroup = ref(false)
const members = ref<MemberOut[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)
/** 抽屉打开的档位 id;声明在 watch 之前,immediate 回调才不会踩到 TDZ */
const drawerId = ref<string | null>(null)

/** 读 Viewer+ 可进本页;写 Admin+ —— 允许成员自己挂档 = 允许自己给自己提权 */
const canManage = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

/* ───── OPT-48 v2:空间级权限挡位功能开关(本空间 Admin+ 专属) ───── */

const spaceToggle = ref<SpaceToggleStatusOut | null>(null)
const toggleSaving = ref(false)

async function loadToggle() {
  if (!auth.currentSpaceId) return
  try {
    spaceToggle.value = await apiGetSpaceToggle(auth.currentSpaceId)
  } catch {
    // 无权(非 Admin+)/加载失败都不影响档位列表本身,卡片不渲染即可
    spaceToggle.value = null
  }
}

/** 拨动前的二次确认;自锁不拦(拍板 3),但打开时明确提示"你还没给自己配挡"的后果 */
async function confirmToggle(): Promise<boolean> {
  const current = spaceToggle.value
  if (!current || toggleSaving.value || !auth.currentSpaceId) return false
  const opening = !current.enabled
  const impact = current.impact
  const message = opening
    ? `打开后,本空间的核心功能(问答/检索/文档)需要"生效挡位",没有的人会立即被拦` +
      `(本空间 ${impact.members_total} 条成员关系中有 ${impact.members_without_active_group} 条没有生效档)。` +
      (impact.actor_has_active_group
        ? ''
        : '你还没给自己配挡,打开后你也将无法使用核心功能(管理功能不受影响,可先给自己分配一档并设为当前)。') +
      '确认打开?'
    : '关闭后,本空间恢复只看成员角色;给成员分配/换当前挡位的操作会不可用。确认关闭?'
  try {
    await ElMessageBox.confirm(message, opening ? '打开权限挡位功能开关' : '关闭权限挡位功能开关', {
      confirmButtonText: opening ? '确认打开' : '确认关闭',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return false
  }
  try {
    toggleSaving.value = true
    spaceToggle.value = await apiPutSpaceToggle(auth.currentSpaceId, { enabled: opening })
    ElMessage.success(opening ? '已打开:本进程已生效,其它实例进程最多 60 秒后跟上' : '已关闭:本空间恢复只看角色')
    return true
  } catch (e) {
    ElMessage.error(errMessage(e))
    return false
  } finally {
    toggleSaving.value = false
  }
}

const CAPABILITY_LABELS: Record<string, string> = {
  chat: '对话检索',
  documents: '文档管理',
}
const CAPABILITY_OPTIONS: Array<{ value: string; desc: string }> = [
  { value: 'chat', desc: '调用流式问答与检索接口(含断线续流)' },
  { value: 'documents', desc: '上传、删除文档与触发重新解析' },
]

/** 未映射的能力值原样展示,后端新增能力时不至于渲染成空白 */
function capabilityLabel(value: string): string {
  return CAPABILITY_LABELS[value] ?? value
}

/** 范围列三种状态必须分得清:全部(实时展开) / 指定 n 个 / 一个都不给 */
function scopeLabel(group: PermissionGroupOut): string {
  if (group.all_kbs) return '全部知识库(含今后新建,实时展开)'
  if (!group.kb_ids.length) return '不含任何知识库'
  const names = group.kb_ids.map((id) => kbs.value.find((k) => k.id === id)?.name)
  if (names.every(Boolean)) return names.join('、')
  const known = names.filter(Boolean).length
  return known
    ? `${names.filter(Boolean).join('、')} 等 ${group.kb_ids.length} 个`
    : `${group.kb_ids.length} 个知识库`
}

function memberLine(m: GroupMemberOut): string {
  if (!m.username) return '已注销账号'
  return m.nickname ? `${m.nickname}(${m.username})` : m.username
}

/* ───── 数据加载 ───── */

watch(
  () => auth.currentSpaceId,
  async () => {
    drawerId.value = null
    await Promise.all([load(), loadKbs(), loadMembers(), loadToggle()])
  },
  { immediate: true },
)

async function load() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    groups.value = []
    return
  }
  loading.value = true
  loadError.value = null
  try {
    groups.value = await apiListPermissionGroups(spaceId)
  } catch (e) {
    loadError.value = errMessage(e)
    groups.value = []
  } finally {
    loading.value = false
  }
}

async function loadKbs() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    kbs.value = []
    kbsDeniedByGroup.value = false
    return
  }
  // 仅用于把 kb_ids 显示成库名;失败不影响档位列表本身
  try {
    kbs.value = await apiListKbs(spaceId)
    kbsDeniedByGroup.value = false
  } catch (e) {
    kbs.value = []
    kbsDeniedByGroup.value = e instanceof ApiError && e.code === 'PERMISSION_GROUP_REQUIRED'
  }
}

async function loadMembers() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canManage.value) {
    members.value = []
    return
  }
  try {
    members.value = await apiListMembers(spaceId)
  } catch {
    members.value = []
  }
}

/** 六个写端点里除删除外都返回更新后的整档,直接原位替换可省一次重拉 */
function replaceGroup(updated: PermissionGroupOut) {
  groups.value = groups.value.map((g) => (g.id === updated.id ? updated : g))
}

/* ───── 建档 / 改档(同一弹窗;PATCH 是整组替换,不做半字段补丁) ───── */

const formOpen = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  name: '',
  description: '',
  capabilities: [] as string[],
  allKbs: false,
  kbIds: [] as string[],
})

const someKbs = computed(() => form.kbIds.length > 0 && !form.allKbs)
/** 零库合法(等于一个都不给),但要拦掉"漏填=不限制"的误解 */
const emptyScope = computed(() => !form.allKbs && form.kbIds.length === 0)
const canSave = computed(() => form.name.trim().length > 0 && !saving.value)

function openCreate() {
  editingId.value = null
  form.name = ''
  form.description = ''
  // 默认不勾能力:档位是权限源,给一个"什么都不让做"的起点比默认放开更安全
  form.capabilities = []
  form.allKbs = false
  form.kbIds = []
  formError.value = ''
  formOpen.value = true
}

function openEdit(group: PermissionGroupOut) {
  editingId.value = group.id
  form.name = group.name
  form.description = group.description
  form.capabilities = [...group.capabilities]
  form.allKbs = group.all_kbs
  form.kbIds = [...group.kb_ids]
  formError.value = ''
  formOpen.value = true
}

/** all_kbs 与 kb_ids 表达同一件事的两种口径,必须互斥(后端同给会 422) */
function toggleAllKbs(value: boolean) {
  form.allKbs = value
  if (value) form.kbIds = []
}

function toggleAllKbChecked(value: boolean) {
  form.kbIds = value ? kbs.value.map((k) => k.id) : []
}

async function onSave() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canSave.value) return
  saving.value = true
  formError.value = ''
  const body: UpsertPermissionGroupRequest = {
    name: form.name.trim(),
    description: form.description.trim(),
    capabilities: form.capabilities,
    all_kbs: form.allKbs,
    kb_ids: form.allKbs ? [] : form.kbIds,
  }
  try {
    if (editingId.value) {
      replaceGroup(await apiUpdatePermissionGroup(spaceId, editingId.value, body))
      ElMessage.success('档位已更新,其下全部 Key 与新范围即时生效')
    } else {
      groups.value = [...groups.value, await apiCreatePermissionGroup(spaceId, body)]
      ElMessage.success('档位已创建')
    }
    formOpen.value = false
  } catch (e) {
    // 未知能力名 / 跨空间库 / all_kbs 与 kb_ids 同时给了:后端 message 已是中文成品
    formError.value = e instanceof ApiError ? e.message : errMessage(e)
  } finally {
    saving.value = false
  }
}

/* ───── 删除:只允许删"空档"(挂着凭据或成员一律 400 拒绝,不做顺手级联) ───── */

async function onDelete(group: PermissionGroupOut) {
  const confirmed = await ElMessageBox.confirm(
    `删除档位「${group.name}」?只有没挂 API Key、也没挂成员的档位可以删除 —— 级联删除会一次性抹掉` +
      '一批人/一批程序的权限,而且明文 Key 不可回显,事后无法向使用方交代。' +
      (group.members.length
        ? `\n该档位当前还挂着 ${group.members.length} 名成员,请先在「成员」抽屉里解除分配。`
        : ''),
    '删除权限档位',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  try {
    await deleteGroupWithPurgeRetry(spaceId, group)
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

/**
 * 删档,并在"只被已吊销凭据挡住"时给一次清空重试的机会(OPT-44)。
 *
 * 坑在这:吊销只置 `revoked_at`,台账行还在,而档位外键是 RESTRICT —— 所以"先吊销"
 * 对已吊销的 Key 不是出路,照文案再做一遍也删不掉档。清空历史凭据不可逆(行没了,
 * 归因只剩审计),因此必须单独确认,且只在没有在用凭据时才自动递这一步。
 */
async function deleteGroupWithPurgeRetry(spaceId: string, group: PermissionGroupOut) {
  try {
    await apiDeletePermissionGroup(spaceId, group.id)
  } catch (e) {
    const { active_key_count: active = 0, revoked_key_count: revoked = 0 } =
      groupDeleteBlockedDetails(e)
    if (!revoked || active) throw e // 还有在用的:出路是人工吊销,不替用户决定
    const go = await ElMessageBox.confirm(
      `「${group.name}」还被 ${revoked} 把已吊销的历史凭据占着 —— 吊销不会删掉台账行,` +
        '档位外键是 RESTRICT,所以删不掉。清空这些行之后才能删档:不可逆,清空后这些 Key 的' +
        '归属与最后使用时间只留在审计里。',
      '清空历史凭据并删除档位',
      { type: 'warning', confirmButtonText: `清空 ${revoked} 把并重试`, cancelButtonText: '取消' },
    ).catch(() => false)
    if (!go) {
      ElMessage.info('未清空历史凭据,档位保留')
      return
    }
    await apiPurgeGroupRevokedKeys(spaceId, group.id)
    await apiDeletePermissionGroup(spaceId, group.id)
  }
  groups.value = groups.value.filter((g) => g.id !== group.id)
  if (drawerId.value === group.id) drawerId.value = null
  ElMessage.success('档位已删除')
}

/* ───── 成员抽屉:分配(可用)→ 设为当前(生效)两步 ───── */

const drawerGroup = computed(() => groups.value.find((g) => g.id === drawerId.value) ?? null)
const assignPick = ref('')
const assigning = ref(false)
const unassigningId = ref('')
const settingCurrentId = ref('')

/** 下拉只给"还没挂在这个档位上"的现存成员 */
const assignCandidates = computed(() => {
  const group = drawerGroup.value
  if (!group) return []
  const mounted = new Set(group.members.map((m) => m.user_id))
  return members.value.filter((m) => !mounted.has(m.user_id))
})

function openDrawer(group: PermissionGroupOut) {
  assignPick.value = ''
  drawerId.value = group.id
}

async function onAssign() {
  const spaceId = auth.currentSpaceId
  const group = drawerGroup.value
  if (!spaceId || !group || !assignPick.value) return
  assigning.value = true
  try {
    replaceGroup(await apiAssignGroupMember(spaceId, group.id, assignPick.value))
    assignPick.value = ''
    ElMessage.success('已分配(尚未生效,需点「设为当前」)')
  } catch (e) {
    if (e instanceof ApiError && e.code === 'MEMBER_NOT_FOUND') ElMessage.warning('该用户已不是本空间成员')
    else ElMessage.error(errMessage(e))
  } finally {
    assigning.value = false
  }
}

async function onSetCurrent(group: PermissionGroupOut, m: GroupMemberOut) {
  const confirmed = await ElMessageBox.confirm(
    `将「${memberLine(m)}」的当前生效档位切到「${group.name}」?` +
      '换档 = 权限开关:此人原本生效的档位会自动让位,TA 在网页上可检索的库范围与可用动作会立即变化。',
    '设为当前档位',
    { type: 'warning', confirmButtonText: '确认换档', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  settingCurrentId.value = m.user_id
  try {
    replaceGroup(await apiSetCurrentGroup(spaceId, group.id, m.user_id))
    ElMessage.success('已设为当前档位')
  } catch (e) {
    // 400 = 尚未挂在档上(理论上不会走到,抽屉只列已挂载的人);其余码直显后端文案
    ElMessage.error(errMessage(e))
  } finally {
    settingCurrentId.value = ''
  }
}

async function onUnassign(group: PermissionGroupOut, m: GroupMemberOut) {
  const confirmed = await ElMessageBox.confirm(
    m.is_current
      ? `解除「${memberLine(m)}」在「${group.name}」上的分配?TA 当前生效的就是这一档 —— ` +
          '解除后 TA 没有生效档位,问答与检索会立即被拒(fail-closed),系统不会自动换到别的档。'
      : `解除「${memberLine(m)}」在「${group.name}」上的分配?TA 仍可用其他已分配档位。`,
    '解除档位分配',
    { type: 'warning', confirmButtonText: '确认解除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  unassigningId.value = m.user_id
  try {
    await apiUnassignGroupMember(spaceId, group.id, m.user_id)
    // 解除端点返回 204 无正文,重拉一次拿最新 members(含 current 是否已消失)
    await load()
    ElMessage.success('已解除分配')
  } catch (e) {
    if (e instanceof ApiError && e.code === 'NOT_FOUND') ElMessage.warning('该成员未挂在这个档位上')
    else ElMessage.error(errMessage(e))
  } finally {
    unassigningId.value = ''
  }
}
</script>

<template>
  <div class="page-pad">
    <div class="pg-wrap">
      <div class="page-head-row">
        <div>
          <div class="page-title">权限档位</div>
          <div class="page-sub">一档 = 一种"能做什么 + 能看哪些库";人和 API Key 共用同一份真相</div>
        </div>
        <el-button v-if="canManage" type="primary" data-testid="group-create-open" @click="openCreate">
          <el-icon class="btn-ic"><Plus /></el-icon>新建档位
        </el-button>
      </div>

      <!-- OPT-48 v2:空间级权限挡位功能开关(本空间 Admin+;核心功能清单口径) -->
      <div v-if="canManage && spaceToggle" class="card pg-toggle" data-testid="space-toggle-card">
        <div class="pg-toggle-main">
          <div>
            <div class="pg-toggle-title">权限挡位功能开关</div>
            <div class="pg-toggle-cap">
              {{ spaceToggle.enabled
                ? '已打开:本空间核心功能(问答/检索/文档)需要生效挡位'
                : '已关闭:本空间只看成员角色' }}
              <template v-if="spaceToggle.updated_at && spaceToggle.updated_by_hint">
                · 由 {{ spaceToggle.updated_by_hint }} 于 {{ fmtDateTime(spaceToggle.updated_at) }} 拨动
              </template>
            </div>
          </div>
          <el-switch
            :model-value="spaceToggle.enabled"
            :before-change="confirmToggle"
            :loading="toggleSaving"
            size="large"
            data-testid="space-toggle-switch"
          />
        </div>
        <div class="pg-toggle-impact">
          本空间 {{ spaceToggle.impact.members_total }} 条成员关系中有
          <b>{{ spaceToggle.impact.members_without_active_group }}</b> 条没有生效档
          <span v-if="spaceToggle.enabled && !spaceToggle.impact.actor_has_active_group" class="pg-toggle-warn">
            · 你还没给自己配挡,你将无法使用核心功能(可先在下方给自己分配一档并设为当前)
          </span>
        </div>
      </div>

      <div class="pg-note">
        <el-icon class="pg-note-ic"><WarningFilled /></el-icon>
        <span>
          <b>改权限请在这里做</b>:档位是权限的唯一真相,API Key 只是它的程序化凭据 ——
          改一次档位,其下全部已签发的 Key 与新范围<b>即时生效</b>,不用重发明文。
          成员要生效需两步:先「分配」到某一档(表示可以用),再「设为当前」(表示此刻就按这档走)。
          <b>什么都不勾的档位等于什么都不能做</b>(默认拒绝),"放开全部知识库"必须显式勾选。
          <b v-if="spaceToggle && !spaceToggle.enabled" class="pg-toggle-warn">
            本空间的开关未打开:给成员「分配」和「设为当前」暂不可用。
          </b>
        </span>
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

      <!-- 空态:没档位时,灰度开关一开全员都不可用,所以这里给足提示 -->
      <div v-else-if="!loading && !groups.length" class="state-wrap">
        <div class="state-title">还没有权限档位</div>
        <div class="state-desc">
          先建一档(例如"对话检索 + 全部知识库"),再把它分配给成员或挂到 API Key 上
        </div>
        <el-button v-if="canManage" type="primary" class="state-btn" @click="openCreate">
          <el-icon class="btn-ic"><Plus /></el-icon>新建档位
        </el-button>
        <div v-else class="state-desc role-hint">
          当前角色无权管理档位(写操作需 Admin 及以上,档位列表 Viewer 及以上可看)
        </div>
      </div>

      <!-- 列表 -->
      <div v-else class="card table-card" data-tour="tour-groups" v-loading="loading">
        <el-table :data="groups" style="width: 100%">
          <el-table-column label="档位" min-width="200">
            <template #default="{ row }">
              <div class="pg-name">{{ row.name }}</div>
              <div v-if="row.description" class="pg-desc truncate">{{ row.description }}</div>
            </template>
          </el-table-column>
          <el-table-column label="能力" min-width="170">
            <template #default="{ row }">
              <span v-if="!row.capabilities.length" class="pg-gray">无能力(什么都不能做)</span>
              <span v-for="cap in row.capabilities" :key="cap" class="pg-cap">{{ capabilityLabel(cap) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="知识库范围" min-width="200">
            <template #default="{ row }">
              <span v-if="!row.all_kbs && !row.kb_ids.length" class="pg-gray">不含任何知识库</span>
              <span v-else class="pg-scope">{{ scopeLabel(row as PermissionGroupOut) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="成员" min-width="200">
            <template #default="{ row }">
              <span v-if="!row.members.length" class="pg-gray">未分配</span>
              <span v-else class="pg-members">
                <span v-for="m in row.members" :key="m.user_id" class="pg-member" :class="{ 'pg-member-dead': m.role === null }">
                  {{ memberLine(m) }}
                  <span v-if="m.is_current" class="pg-badge">当前</span>
                </span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="120">
            <template #default="{ row }">
              <span class="cell-dim">{{ fmtDate(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openDrawer(row as PermissionGroupOut)">成员</el-button>
              <template v-if="canManage">
                <el-button link type="primary" @click="openEdit(row as PermissionGroupOut)">编辑</el-button>
                <el-button link type="danger" @click="onDelete(row as PermissionGroupOut)">删除</el-button>
              </template>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="groups.length" class="pg-foot">
          共 {{ groups.length }} 档 · 每人每空间至多一个「当前」档位
        </div>
      </div>
    </div>

    <!-- 建档 / 改档 -->
    <el-dialog
      v-model="formOpen"
      :title="editingId ? '编辑权限档位' : '新建权限档位'"
      width="560px"
      append-to-body
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="名称" required>
          <el-input
            v-model="form.name"
            maxlength="64"
            placeholder="例如:只读检索-全部库"
            data-testid="group-name"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" maxlength="512" placeholder="给谁用、能做什么(选填)" />
        </el-form-item>
        <el-form-item label="能力">
          <el-checkbox-group v-model="form.capabilities" class="pg-caps">
            <label
              v-for="opt in CAPABILITY_OPTIONS"
              :key="opt.value"
              class="pg-cap-opt"
              :data-testid="`group-cap-${opt.value}`"
            >
              <el-checkbox :value="opt.value" />
              <span>
                <span class="pg-cap-title">{{ capabilityLabel(opt.value) }}</span>
                <span class="pg-cap-sub">{{ opt.desc }}</span>
              </span>
            </label>
          </el-checkbox-group>
          <div v-if="!form.capabilities.length" class="pg-warn-line">
            一个能力都没勾:这一档<b>什么都不能做</b>(空清单是默认拒绝,不是"全部允许")
          </div>
        </el-form-item>
        <el-form-item label="知识库范围">
          <div class="pg-scope-box">
            <div class="pg-scope-row">
              <span>放开本空间全部知识库(每次请求实时展开,新建的库自动在范围内)</span>
              <el-switch
                :model-value="form.allKbs"
                data-testid="group-all-kbs"
                @update:model-value="(v: any) => toggleAllKbs(Boolean(v))"
              />
            </div>
            <div v-if="!form.allKbs" class="pg-kb-head">
              <span class="cell-dim">或逐个勾选允许的库</span>
              <el-checkbox
                :model-value="form.kbIds.length > 0 && form.kbIds.length === kbs.length"
                :indeterminate="someKbs"
                :disabled="!kbs.length"
                @update:model-value="(v: any) => toggleAllKbChecked(Boolean(v))"
              >
                全选
              </el-checkbox>
            </div>
            <el-checkbox-group v-if="!form.allKbs" v-model="form.kbIds" class="pg-kb-grid">
              <label v-for="kb in kbs" :key="kb.id" class="pg-kb-opt">
                <el-checkbox :value="kb.id" />
                <span class="truncate">{{ kb.name }}</span>
              </label>
              <div v-if="!kbs.length && !kbsDeniedByGroup" class="cell-dim">当前空间还没有知识库</div>
            </el-checkbox-group>
            <div v-if="kbsDeniedByGroup" class="pg-warn-line">
              读不到知识库列表:<b>你自己还没有生效档位</b>(灰度开关开启后库列表也按档位收口)。
              可先用上面的开关建一档「放开全部知识库」,保存后到「成员」里把自己<b>设为当前档</b>,再回本页就能逐个勾选。
            </div>
            <div v-if="emptyScope" class="pg-warn-line">
              没勾任何知识库:这一档<b>一个库都检索不到</b>(空列表是默认拒绝)。要放开全部库请打开上面的开关。
            </div>
          </div>
        </el-form-item>
      </el-form>
      <div v-if="formError" class="pg-err">{{ formError }}</div>
      <div v-if="editingId" class="pg-hint">
        保存即整组替换能力与范围,其下全部 API Key 与已挂此档的成员<b>立刻</b>按新范围生效;审计会记下改前/改后。
      </div>
      <template #footer>
        <el-button @click="formOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!canSave"
          data-testid="group-submit"
          @click="onSave"
        >
          {{ editingId ? '保存' : '创建档位' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 成员抽屉:分配 / 设为当前 / 解除 -->
    <el-drawer :model-value="!!drawerGroup" title="档位成员" size="440px" @close="drawerId = null">
      <template v-if="drawerGroup">
        <div class="pg-drawer-head">
          <div class="pg-name">{{ drawerGroup.name }}</div>
          <div class="pg-desc">
            <span v-if="!drawerGroup.capabilities.length" class="pg-gray">无能力</span>
            <span v-for="cap in drawerGroup.capabilities" :key="cap" class="pg-cap">{{ capabilityLabel(cap) }}</span>
            <span class="pg-scope"> · {{ scopeLabel(drawerGroup) }}</span>
          </div>
        </div>

        <div v-if="canManage" class="pg-assign">
          <el-select v-model="assignPick" class="pg-assign-select" placeholder="选择要挂到本档的现存成员" clearable>
            <el-option
              v-for="m in assignCandidates"
              :key="m.user_id"
              :label="`${m.nickname}(${m.username}) · ${roleLabel(m.role)}`"
              :value="m.user_id"
            />
          </el-select>
          <el-button
            type="primary"
            :loading="assigning"
            :disabled="!assignPick"
            @click="onAssign"
          >
            分配
          </el-button>
        </div>
        <div v-if="canManage" class="pg-hint">
          分配只表示"可以用这一档",<b>不会自动生效</b>;生效要点该成员的「设为当前」(每人每空间至多一个当前档)。
        </div>

        <div v-if="!drawerGroup.members.length" class="pg-drawer-empty">还没有成员挂在这个档位上</div>
        <div v-else class="pg-member-rows">
          <div
            v-for="m in drawerGroup.members"
            :key="m.user_id"
            class="pg-member-row"
            :class="{ 'pg-member-dead': m.role === null }"
          >
            <div class="pg-member-main">
              <div class="pg-member-name">
                {{ memberLine(m) }}
                <span v-if="m.is_current" class="pg-badge">当前生效</span>
                <span v-if="m.role === null" class="pg-gray">已移出空间</span>
              </div>
              <div v-if="m.role !== null" class="pg-member-role">{{ roleLabel(m.role) }}</div>
            </div>
            <div v-if="canManage" class="pg-member-ops">
              <el-button
                link
                type="primary"
                :disabled="m.is_current || m.role === null"
                :title="m.role === null ? '人已移出空间,不能设为当前(重新加入后再配档)' : ''"
                :loading="settingCurrentId === m.user_id"
                @click="onSetCurrent(drawerGroup!, m)"
              >
                {{ m.is_current ? '已生效' : '设为当前' }}
              </el-button>
              <el-button
                link
                type="danger"
                :loading="unassigningId === m.user_id"
                @click="onUnassign(drawerGroup!, m)"
              >
                解除分配
              </el-button>
            </div>
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.btn-ic {
  margin-right: 4px;
}
.pg-wrap {
  max-width: 1240px;
}
.role-hint {
  margin-top: 12px;
}
.pg-note {
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
.pg-note-ic {
  margin-top: 2px;
  color: #9ca3af;
  flex-shrink: 0;
}
.table-card {
  overflow: hidden;
  min-height: 200px;
}
.pg-name {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.pg-desc {
  margin-top: 2px;
  font-size: 11px;
  color: #9ca3af;
}
.pg-cap {
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
.pg-gray {
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
.pg-scope {
  font-size: 13px;
  color: #374151;
}
.cell-dim {
  font-size: 13px;
  color: #6b7280;
}
.pg-badge {
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
}
.pg-members {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}
.pg-member {
  font-size: 12px;
  color: #374151;
}
.pg-member-dead {
  opacity: 0.5;
}
.pg-foot {
  height: 48px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  border-top: 1px solid #e5e7eb;
  font-size: 12px;
  color: #9ca3af;
}

/*
 * Element Plus 给 .el-checkbox-group 设了 font-size:0 / line-height:0(消除勾选项之间空白),
 * 组里放了自定义文案就会继承成 0 而行盒塌陷,必须重置。scoped 属性选择器优先级更高,可覆盖。
 */
.pg-caps,
.pg-kb-grid {
  font-size: 14px;
  line-height: 1.5;
}
.pg-caps {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pg-cap-opt {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  cursor: pointer;
}
.pg-cap-opt:hover {
  background: #f7f8fa;
}
.pg-cap-title {
  display: block;
  font-size: 13px;
  color: #111827;
}
.pg-cap-sub {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: #9ca3af;
}
.pg-scope-box {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pg-scope-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  font-size: 13px;
  color: #374151;
}
.pg-kb-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.pg-kb-grid {
  width: 100%;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  max-height: 160px;
  overflow-y: auto;
}
.pg-kb-opt {
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
.pg-kb-opt:hover {
  background: #f7f8fa;
}
.pg-warn-line {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: #d97706;
}
.pg-hint {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: #9ca3af;
}
.pg-err {
  margin-top: 4px;
  font-size: 13px;
  color: #ef4444;
}
.pg-drawer-head {
  padding-bottom: 12px;
  border-bottom: 1px solid #e5e7eb;
  margin-bottom: 14px;
}
.pg-assign {
  display: flex;
  gap: 8px;
}
.pg-assign-select {
  flex: 1;
}
.pg-drawer-empty {
  margin-top: 16px;
  font-size: 13px;
  color: #9ca3af;
}
.pg-member-rows {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
}
.pg-member-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid #f1f2f4;
}
.pg-member-name {
  font-size: 13px;
  color: #111827;
}
.pg-member-role {
  margin-top: 2px;
  font-size: 11px;
  color: #9ca3af;
}
.pg-member-ops {
  flex-shrink: 0;
}
.pg-toggle-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.pg-toggle-title {
  font-weight: 600;
  font-size: 14px;
}
.pg-toggle-cap {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pg-toggle-impact {
  margin-top: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pg-toggle-warn {
  color: var(--el-color-warning);
  font-weight: 600;
}
</style>
