<script setup lang="ts">
import { ArrowDown, OfficeBuilding, Plus, WarningFilled } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'

import { ApiError, errMessage, fieldErrors } from '@/api/http'
import {
  apiAddMember,
  apiDeleteSpace,
  apiGetSpace,
  apiLeaveSpace,
  apiListMembers,
  apiRemoveMember,
  apiUpdateMemberRole,
  apiUpdateSpace,
} from '@/api/spaces'
import type { MemberOut, SpaceOut } from '@/api/types'
import CreateSpaceDialog from '@/components/CreateSpaceDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { fmtDate } from '@/utils/format'
import { grantableRoles, isAtLeast, roleLabel, ROLE } from '@/utils/roles'

/**
 * 空间管理(原型 06):成员表 + 空间设置两个 Tab。
 * 按角色显隐(前端体验层,后端 403/404 兜底):
 * - 成员列表:所有角色可见;邀请/改角色/移除仅 Admin+;
 * - 角色下拉只能授予严格低于自己的角色;Owner 额外有"转让所有权"(role=40,高危二次确认);
 * - 空间设置 Tab 仅 Admin+;删除空间仅 Owner;退出空间 Owner 不可用(先转让或删空间)。
 */
const router = useRouter()
const auth = useAuthStore()

const loading = ref(false)
const loadError = ref<ApiError | null>(null)
const space = ref<SpaceOut | null>(null)
const members = ref<MemberOut[]>([])

const tab = ref<'members' | 'settings'>('members')
const createOpen = ref(false)

const myRole = computed(() => auth.role ?? space.value?.role ?? 0)
const canManage = computed(() => isAtLeast(myRole.value, ROLE.ADMIN))
const isOwner = computed(() => isAtLeast(myRole.value, ROLE.OWNER))
const grantable = computed(() => grantableRoles(myRole.value))
const meId = computed(() => auth.user?.id ?? '')

const ROLE_DESC: Record<number, string> = {
  [ROLE.OWNER]: 'Owner:空间全部权限,含删除空间与转让所有权',
  [ROLE.ADMIN]: 'Admin:成员管理与空间设置,可管理知识库与 API Key',
  [ROLE.EDITOR]: 'Editor:可上传文档、管理自己上传的文档,可对话',
  [ROLE.VIEWER]: 'Viewer:只读对话,不能上传或修改文档,不可见空间设置',
}

function canChangeRole(m: MemberOut): boolean {
  return canManage.value && m.user_id !== meId.value && m.role < myRole.value
}

function canRemoveMember(m: MemberOut): boolean {
  return canManage.value && m.user_id !== meId.value && m.role < myRole.value
}

watch(
  () => auth.currentSpaceId,
  () => {
    tab.value = 'members'
    load()
  },
  { immediate: true },
)

watch(canManage, (v) => {
  if (!v) tab.value = 'members'
})

async function load() {
  const id = auth.currentSpaceId
  if (!id) return
  loading.value = true
  loadError.value = null
  try {
    space.value = await apiGetSpace(id)
    members.value = await apiListMembers(id)
  } catch (e) {
    loadError.value =
      e instanceof ApiError ? e : new ApiError('UNKNOWN', errMessage(e), 0)
  } finally {
    loading.value = false
  }
}

function goChat() {
  router.push('/chat')
}

/** 403 等业务错误的统一提示 */
function actionError(e: unknown) {
  if (e instanceof ApiError && e.code === 'FORBIDDEN') {
    ElMessage.warning('当前角色无权执行此操作')
  } else {
    ElMessage.error(errMessage(e))
  }
}

/* ───── 成员角色 ───── */

async function onChangeRole(m: MemberOut, targetRole: number) {
  if (targetRole === ROLE.OWNER) {
    const confirmed = await ElMessageBox.confirm(
      `将所有权转让给「${m.nickname}」?转让后你将降为 Admin,对方将拥有空间全部权限。`,
      '转让所有权',
      { type: 'warning', confirmButtonText: '确认转让', cancelButtonText: '取消' },
    ).catch(() => false)
    if (!confirmed) return
  }
  try {
    await apiUpdateMemberRole(auth.currentSpaceId!, m.user_id, { role: targetRole })
    ElMessage.success(targetRole === ROLE.OWNER ? '所有权已转让' : `角色已变更为 ${roleLabel(targetRole)}`)
    await loadMembers()
    if (targetRole === ROLE.OWNER) await auth.refreshSpaces() // 自己已降级,刷新侧栏角色
  } catch (e) {
    actionError(e)
  }
}

async function loadMembers() {
  members.value = await apiListMembers(auth.currentSpaceId!)
}

async function onRemoveMember(m: MemberOut) {
  const confirmed = await ElMessageBox.confirm(
    `移除成员「${m.nickname}」?移除后其将立即失去对本空间所有知识库与对话的访问权限,其上传的文档将保留。`,
    '移除成员',
    { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiRemoveMember(auth.currentSpaceId!, m.user_id)
    ElMessage.success('成员已移除')
    await loadMembers()
  } catch (e) {
    actionError(e)
  }
}

/* ───── 邀请成员 ───── */

const inviteOpen = ref(false)
const inviting = ref(false)
const inviteRef = ref<FormInstance>()
const inviteForm = reactive({ username: '', role: ROLE.VIEWER as number })
const inviteServerError = ref('')

const inviteRules: FormRules = {
  username: [{ required: true, message: '请输入对方账号', trigger: 'blur' }],
}

watch(inviteOpen, (v) => {
  if (v) {
    inviteForm.username = ''
    inviteForm.role = ROLE.VIEWER
    inviteServerError.value = ''
  }
})

async function onInvite() {
  const valid = await inviteRef.value?.validate().catch(() => false)
  if (!valid) return
  inviting.value = true
  try {
    await apiAddMember(auth.currentSpaceId!, {
      username: inviteForm.username.trim(),
      role: inviteForm.role,
    })
    ElMessage.success(`已添加成员「${inviteForm.username.trim()}」`)
    inviteOpen.value = false
    await loadMembers()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'USER_NOT_FOUND') {
      inviteServerError.value = '该账号尚未注册'
    } else if (e instanceof ApiError && e.code === 'MEMBER_ALREADY') {
      inviteServerError.value = '该用户已是空间成员'
    } else {
      const fields = fieldErrors((e as { details?: unknown }).details)
      if (fields.username) inviteServerError.value = fields.username
      else actionError(e)
    }
  } finally {
    inviting.value = false
  }
}

/* ───── 空间设置 ───── */

const settingForm = reactive({ name: '', description: '' })
const savingSpace = ref(false)

watch(space, (s) => {
  if (s) {
    settingForm.name = s.name
    settingForm.description = s.description
  }
})

async function onSaveSpace() {
  if (!settingForm.name.trim()) {
    ElMessage.warning('空间名称不能为空')
    return
  }
  savingSpace.value = true
  try {
    space.value = await apiUpdateSpace(auth.currentSpaceId!, {
      name: settingForm.name.trim(),
      description: settingForm.description.trim(),
    })
    settingForm.name = space.value.name
    settingForm.description = space.value.description
    ElMessage.success('空间信息已保存')
    await auth.refreshSpaces() // 同步侧栏中的空间名
  } catch (e) {
    actionError(e)
  } finally {
    savingSpace.value = false
  }
}

async function onLeaveSpace() {
  const name = space.value?.name ?? ''
  const confirmed = await ElMessageBox.confirm(
    `退出空间「${name}」?退出后将失去对本空间所有知识库与对话的访问权限。`,
    '退出空间',
    { type: 'warning', confirmButtonText: '退出', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiLeaveSpace(auth.currentSpaceId!)
    await auth.refreshSpaces()
    ElMessage.success('已退出空间')
    goChat()
  } catch (e) {
    actionError(e)
  }
}

async function onDeleteSpace() {
  const name = space.value?.name ?? ''
  const confirmed = await ElMessageBox.confirm(
    `将永久删除空间「${name}」及全部成员关系,此操作不可恢复!`,
    '删除空间',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteSpace(auth.currentSpaceId!)
    await auth.refreshSpaces()
    ElMessage.success('空间已删除')
    goChat()
  } catch (e) {
    actionError(e)
  }
}
</script>

<template>
  <div class="page-pad" v-loading="loading">
    <!-- 尚未选择空间 -->
    <template v-if="!auth.currentSpaceId">
      <div class="empty-wrap">
        <el-empty description="尚未选择空间,先创建一个吧">
          <el-button type="primary" @click="createOpen = true">
            <el-icon class="btn-ic"><Plus /></el-icon>新建空间
          </el-button>
        </el-empty>
      </div>
      <CreateSpaceDialog v-model="createOpen" />
    </template>

    <!-- 空间不存在 / 无权访问(404 语义兜底) -->
    <template v-else-if="loadError && (loadError.code === 'SPACE_NOT_FOUND' || loadError.code === 'NOT_FOUND')">
      <div class="empty-wrap">
        <div class="err-box">
          <el-icon class="err-ic"><WarningFilled /></el-icon>
          <div class="err-title">空间不存在或无权访问</div>
          <div class="err-desc">你可能已被移出该空间,或空间已被删除</div>
          <el-button class="err-btn" @click="goChat">返回对话</el-button>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="space-wrap">
        <!-- 页头 -->
        <div class="page-head">
          <div class="head-icon">
            <el-icon><OfficeBuilding /></el-icon>
          </div>
          <span class="head-name">{{ space?.name ?? '—' }}</span>
          <span class="head-badge">我的角色:{{ roleLabel(myRole) }}</span>
        </div>

        <el-tabs v-model="tab" class="space-tabs">
          <!-- ═══ Tab 成员 ═══ -->
          <el-tab-pane label="成员" name="members">
            <div class="toolbar">
              <div class="toolbar-hint">共 {{ members.length }} 名成员</div>
              <el-button v-if="canManage" type="primary" @click="inviteOpen = true">
                <el-icon class="btn-ic"><Plus /></el-icon>邀请成员
              </el-button>
            </div>

            <div v-if="loadError" class="load-failed">
              <span>成员加载失败:{{ loadError.message }}</span>
              <el-button size="small" @click="load">重试</el-button>
            </div>

            <div class="card table-card">
              <el-table :data="members" style="width: 100%">
                <el-table-column label="成员" min-width="260">
                  <template #default="{ row }">
                    <div class="member-cell">
                      <span class="member-avatar">{{ (row.nickname || row.username).slice(0, 1) }}</span>
                      <div class="member-info">
                        <div class="member-name">
                          {{ row.nickname }}
                          <span v-if="row.user_id === meId" class="member-me">(我)</span>
                        </div>
                        <div class="member-username">{{ row.username }}</div>
                      </div>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="角色" width="220">
                  <template #default="{ row }">
                    <el-dropdown
                      v-if="canChangeRole(row as MemberOut)"
                      trigger="click"
                      popper-class="role-dd"
                      @command="(cmd: number) => onChangeRole(row as MemberOut, cmd)"
                    >
                      <button class="role-btn">
                        {{ roleLabel(row.role) }}
                        <el-icon class="caret"><ArrowDown /></el-icon>
                      </button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <li class="dd-label">变更角色</li>
                          <el-dropdown-item v-for="o in grantable" :key="o.value" :command="o.value">
                            <span class="dd-name" :class="{ on: o.value === row.role }">{{ roleLabel(o.value) }}</span>
                            <span class="dd-desc">{{ o.desc }}</span>
                          </el-dropdown-item>
                          <el-dropdown-item v-if="isOwner" divided :command="ROLE.OWNER">
                            <span class="dd-name danger">转让所有权</span>
                            <span class="dd-desc">对方成为 Owner,你将降为 Admin</span>
                          </el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                    <span v-else class="role-plain" :title="ROLE_DESC[row.role]">{{ roleLabel(row.role) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="加入时间" width="140">
                  <template #default="{ row }">
                    <span class="cell-dim">{{ fmtDate(row.joined_at) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="100" align="right">
                  <template #default="{ row }">
                    <el-link
                      v-if="canRemoveMember(row as MemberOut)"
                      type="danger"
                      :underline="false"
                      @click="onRemoveMember(row as MemberOut)"
                    >
                      移除
                    </el-link>
                    <span
                      v-else
                      class="cell-none"
                      :title="row.user_id === meId && canManage ? '不能对自己执行此操作' : ''"
                    >—</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <!-- 底部权限对照(固定说明文案) -->
            <div class="legend">
              <div class="legend-item">
                <span class="legend-role">Owner</span>
                <span class="legend-desc">空间全部权限,含删除空间与转让所有权</span>
              </div>
              <div class="legend-item">
                <span class="legend-role">Admin</span>
                <span class="legend-desc">成员管理、空间设置与知识库管理</span>
              </div>
              <div class="legend-item">
                <span class="legend-role">Editor</span>
                <span class="legend-desc">上传与管理文档,可对话</span>
              </div>
              <div class="legend-item">
                <span class="legend-role">Viewer</span>
                <span class="legend-desc">只读对话,不可见设置页</span>
              </div>
            </div>
          </el-tab-pane>

          <!-- ═══ Tab 空间设置(仅 Admin+) ═══ -->
          <el-tab-pane v-if="canManage" label="空间设置" name="settings">
            <div class="settings-wrap">
              <div class="card block-card">
                <div class="block-title">基本信息</div>
                <el-form label-position="top" class="block-form">
                  <el-form-item label="空间名称">
                    <el-input v-model="settingForm.name" maxlength="64" />
                  </el-form-item>
                  <el-form-item label="空间描述">
                    <el-input
                      v-model="settingForm.description"
                      type="textarea"
                      :rows="3"
                      maxlength="200"
                      placeholder="这个空间用来做什么"
                    />
                  </el-form-item>
                </el-form>
                <el-button type="primary" :loading="savingSpace" @click="onSaveSpace">保存</el-button>
              </div>

              <div class="card block-card">
                <div class="block-title">默认检索参数</div>
                <div class="block-note">此能力依赖知识库接口,将于后续版本上线。</div>
              </div>

              <div class="card block-card">
                <div class="block-title">危险操作</div>
                <div class="danger-row">
                  <div>
                    <div class="danger-name">退出空间</div>
                    <div class="danger-desc">退出后将失去对本空间所有知识库与对话的访问权限</div>
                  </div>
                  <el-button v-if="!isOwner" type="danger" plain @click="onLeaveSpace">退出空间</el-button>
                  <span v-else class="danger-tip">Owner 不能退出,需先转让所有权或删除空间</span>
                </div>
                <div v-if="isOwner" class="danger-row divider">
                  <div>
                    <div class="danger-name">删除空间</div>
                    <div class="danger-desc">将永久删除空间与全部成员关系,不可恢复</div>
                  </div>
                  <el-button type="danger" @click="onDeleteSpace">删除空间</el-button>
                </div>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>

      <!-- 邀请成员 -->
      <el-dialog v-model="inviteOpen" title="邀请成员" width="420px">
        <el-form ref="inviteRef" :model="inviteForm" :rules="inviteRules" label-position="top" @submit.prevent>
          <el-form-item label="账号" prop="username" :error="inviteServerError || undefined">
            <el-input v-model="inviteForm.username" placeholder="对方的注册账号" @input="inviteServerError = ''" />
          </el-form-item>
          <el-form-item label="角色">
            <el-select v-model="inviteForm.role" class="invite-role">
              <el-option
                v-for="o in grantable"
                :key="o.value"
                :value="o.value"
                :label="o.label"
              />
            </el-select>
            <div class="invite-note">对方注册后即可访问本空间;加入后可在「成员」页变更角色。</div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="inviteOpen = false">取消</el-button>
          <el-button type="primary" :loading="inviting" @click="onInvite">添加成员</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
.space-wrap {
  max-width: 1000px;
}
.btn-ic {
  margin-right: 4px;
}

.empty-wrap {
  display: grid;
  place-items: center;
  padding: 96px 0;
}
.err-box {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.err-ic {
  font-size: 44px;
  width: 48px;
  height: 48px;
  color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
  border-radius: 50%;
  padding: 12px;
  box-sizing: border-box;
}
.err-title {
  margin-top: 16px;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.err-desc {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
.err-btn {
  margin-top: 20px;
}

.page-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.head-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #eef1fe;
  display: grid;
  place-items: center;
  color: #4f6ef2;
}
.head-name {
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}
.head-badge {
  font-size: 11px;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
  background: #f7f8fa;
  color: #6b7280;
  display: inline-flex;
  align-items: center;
}

.space-tabs {
  margin-top: 20px;
}
.space-tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 16px 0 12px;
}
.toolbar-hint {
  font-size: 12px;
  color: #9ca3af;
}
.load-failed {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 13px;
  color: #ef4444;
}

.table-card {
  overflow: hidden;
}
.member-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.member-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #eef1fe;
  color: #4f6ef2;
  font-size: 12px;
  font-weight: 500;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.member-info {
  line-height: 1.3;
}
.member-name {
  font-size: 13px;
  color: #111827;
}
.member-me {
  font-size: 11px;
  color: #9ca3af;
  margin-left: 4px;
}
.member-username {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 2px;
}
.role-btn {
  height: 28px;
  padding: 0 8px;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
  background: #fff;
  font-size: 13px;
  color: #374151;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.role-btn:hover {
  background: #f7f8fa;
}
.role-btn .caret {
  color: #9ca3af;
  font-size: 12px;
}
.role-plain {
  font-size: 13px;
  color: #374151;
  cursor: default;
}
.cell-dim {
  font-size: 13px;
  color: #9ca3af;
}
.cell-none {
  color: #9ca3af;
}
:deep(.dd-label) {
  padding: 6px 8px;
  font-size: 11px;
  color: #9ca3af;
  list-style: none;
}

.legend {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-top: 16px;
  font-size: 12px;
}
.legend-item {
  border-radius: 8px;
  background: #f7f8fa;
  padding: 10px 12px;
}
.legend-role {
  color: #374151;
  font-weight: 500;
}
.legend-desc {
  display: block;
  margin-top: 4px;
  color: #9ca3af;
  line-height: 1.6;
}

.settings-wrap {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding-top: 16px;
}
.block-card {
  padding: 24px;
}
.block-title {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}
.block-form {
  margin-top: 12px;
}
.block-note {
  margin-top: 6px;
  font-size: 12px;
  color: #9ca3af;
}
.danger-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 16px;
}
.danger-row.divider {
  border-top: 1px solid #f3f4f6;
  padding-top: 16px;
}
.danger-name {
  font-size: 13px;
  color: #374151;
  font-weight: 500;
}
.danger-desc {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 2px;
}
.danger-tip {
  font-size: 12px;
  color: #9ca3af;
}

.invite-role {
  width: 100%;
}
.invite-note {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 6px;
  line-height: 1.6;
}
</style>
