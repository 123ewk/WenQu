<script setup lang="ts">
import { Check, Delete, Upload } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'

import { ApiError, errMessage, fieldErrors } from '@/api/http'
import { apiChangePassword, apiDeleteAvatar, apiGetAvatar, apiUploadAvatar } from '@/api/users'
import { useAuthStore } from '@/stores/auth'
import { daysSince, fmtDateTime } from '@/utils/format'

/**
 * 个人中心(原型 09):基本信息(含头像)+ 上次登录 + 修改密码,居中 640px。
 * - 昵称 PATCH /users/me(空白 422,前端先拦),成功后行内"已保存";
 * - 头像三端点:读取需 Authorization → blob 渲染 + revokeObjectURL;上传前端先校验
 *   PNG/JPEG/WEBP、≤2MB、单边 ≤4096px(上限是运行时配置,契约表达不了);
 * - 上次登录:last_login_at/last_login_ip 直显;IP 取直连地址,文案保持中性(不写归属地);
 * - 改密 POST /users/me/password(204)成功后后端吊销全部会话 → 清凭据强制回登录页。
 */
const AVATAR_MAX_MB = 2
const AVATAR_MAX_EDGE = 4096
const AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/webp']

const auth = useAuthStore()
const router = useRouter()

const avatarChar = computed(() => (auth.user?.nickname || auth.user?.username || '?').slice(0, 1))
const joinDays = computed(() => daysSince(auth.user?.created_at))

/* ───── 头像 ───── */

const avatarObjectUrl = ref<string | null>(null)
const avatarLoading = ref(false)
const avatarFileInput = ref<HTMLInputElement>()

async function loadAvatarBlob() {
  if (!auth.user?.avatar_url) {
    clearAvatarObjectUrl()
    return
  }
  try {
    const blob = await apiGetAvatar()
    clearAvatarObjectUrl()
    avatarObjectUrl.value = URL.createObjectURL(blob)
  } catch {
    clearAvatarObjectUrl() // 404 等:退回字母头像
  }
}

function clearAvatarObjectUrl() {
  if (avatarObjectUrl.value) URL.revokeObjectURL(avatarObjectUrl.value)
  avatarObjectUrl.value = null
}

watch(() => auth.user?.avatar_url, loadAvatarBlob, { immediate: true })
onUnmounted(clearAvatarObjectUrl)

function pickAvatar() {
  avatarFileInput.value?.click()
}

async function onAvatarChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许重复选择同一文件
  if (!file) return
  if (!AVATAR_TYPES.includes(file.type)) {
    ElMessage.warning('请上传 PNG/JPEG/WEBP 格式图片')
    return
  }
  if (file.size > AVATAR_MAX_MB * 1024 * 1024) {
    ElMessage.warning(`图片不能超过 ${AVATAR_MAX_MB}MB`)
    return
  }
  const dims = await imageEdge(file)
  if (dims > AVATAR_MAX_EDGE) {
    ElMessage.warning(`图片单边不能超过 ${AVATAR_MAX_EDGE}px(当前 ${dims}px)`)
    return
  }
  avatarLoading.value = true
  try {
    auth.applyUser(await apiUploadAvatar(file))
    ElMessage.success('头像已更新')
  } catch (e) {
    if (e instanceof ApiError && e.code === 'UNSUPPORTED_FORMAT') ElMessage.warning('请上传 PNG/JPEG/WEBP 格式图片')
    else if (e instanceof ApiError && e.code === 'FILE_TOO_LARGE') ElMessage.warning(`图片超过限制(≤${AVATAR_MAX_MB}MB、单边 ≤${AVATAR_MAX_EDGE}px)`)
    else ElMessage.error(errMessage(e))
  } finally {
    avatarLoading.value = false
  }
}

/** 读取图片单边最大像素(防解压炸弹由后端兜底,这里提前拦截省一次上传) */
function imageEdge(file: File): Promise<number> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      resolve(Math.max(img.naturalWidth, img.naturalHeight))
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      resolve(0) // 解码失败交给后端判格式
    }
    img.src = url
  })
}

async function onDeleteAvatar() {
  avatarLoading.value = true
  try {
    await apiDeleteAvatar()
    if (auth.user) auth.applyUser({ ...auth.user, avatar_url: null })
    ElMessage.success('头像已删除')
  } catch (e) {
    ElMessage.error(errMessage(e))
  } finally {
    avatarLoading.value = false
  }
}

/* ───── 基本信息 ───── */

const nickname = ref(auth.user?.nickname ?? '')
const savingProfile = ref(false)
const profileOk = ref(false)
let profileOkTimer: ReturnType<typeof setTimeout> | null = null

watch(nickname, () => {
  profileOk.value = false
})

async function onSaveProfile() {
  const name = nickname.value.trim()
  if (!name) {
    ElMessage.warning('昵称不能为空')
    return
  }
  savingProfile.value = true
  try {
    await auth.updateNickname(name)
    profileOk.value = true
    if (profileOkTimer) clearTimeout(profileOkTimer)
    profileOkTimer = setTimeout(() => (profileOk.value = false), 3000)
  } catch (e) {
    ElMessage.error(errMessage(e))
  } finally {
    savingProfile.value = false
  }
}

/* ───── 修改密码 ───── */

const pwdRef = ref<FormInstance>()
const changingPwd = ref(false)
const pwdForm = reactive({ oldPassword: '', newPassword: '', confirm: '' })
const oldPwdServerError = ref('')
const newPwdServerError = ref('')

const pwdRules: FormRules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, message: '新密码长度至少 8 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== pwdForm.newPassword) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function onChangePassword() {
  const valid = await pwdRef.value?.validate().catch(() => false)
  if (!valid) return
  changingPwd.value = true
  oldPwdServerError.value = ''
  newPwdServerError.value = ''
  try {
    await apiChangePassword({
      old_password: pwdForm.oldPassword,
      new_password: pwdForm.newPassword,
    })
    // 后端已吊销该用户全部会话:清空凭据并回到登录页
    await ElMessageBox.alert('密码已修改,请使用新密码重新登录', '修改成功', {
      confirmButtonText: '重新登录',
      type: 'success',
    })
    auth.resetAuth()
    router.push({ name: 'login' })
  } catch (e) {
    if (e instanceof ApiError && e.code === 'INVALID_CREDENTIALS') {
      oldPwdServerError.value = '原密码不正确'
    } else if (e instanceof ApiError && e.code === 'VALIDATION_ERROR') {
      const fields = fieldErrors(e.details)
      const key = fields.old_password ? 'old' : 'new'
      if (key === 'old') oldPwdServerError.value = fields.old_password!
      else newPwdServerError.value = fields.new_password ?? e.message
    } else {
      ElMessage.error(errMessage(e))
    }
  } finally {
    changingPwd.value = false
  }
}
</script>

<template>
  <div class="page-pad">
    <div class="profile-wrap">
      <!-- 卡片 1:基本信息 -->
      <div class="card block-card">
        <div class="block-title">基本信息</div>
        <div class="block-body">
          <div class="avatar-row">
            <div class="big-avatar">
              <img v-if="avatarObjectUrl" :src="avatarObjectUrl" alt="头像" />
              <template v-else>{{ avatarChar }}</template>
            </div>
            <div class="avatar-actions">
              <el-button size="small" :loading="avatarLoading" @click="pickAvatar">
                <el-icon class="btn-ic"><Upload /></el-icon>{{ auth.user?.avatar_url ? '更换头像' : '上传头像' }}
              </el-button>
              <el-button
                v-if="auth.user?.avatar_url"
                size="small"
                text
                :disabled="avatarLoading"
                @click="onDeleteAvatar"
              >
                <el-icon class="btn-ic"><Delete /></el-icon>删除
              </el-button>
              <input
                ref="avatarFileInput"
                type="file"
                accept="image/png,image/jpeg,image/webp"
                class="hidden-input"
                @change="onAvatarChange"
              />
            </div>
            <div class="login-meta">
              <div class="field-label">上次登录</div>
              <div class="login-value">
                {{ auth.user?.last_login_at ? fmtDateTime(auth.user.last_login_at) : '—' }}
                <template v-if="auth.user?.last_login_ip">
                  <span class="login-src">来源 {{ auth.user.last_login_ip }}</span>
                </template>
              </div>
            </div>
          </div>
          <div>
            <div class="field-label">昵称</div>
            <el-input v-model="nickname" maxlength="32" class="field-input" />
          </div>
          <div>
            <div class="field-label">账号(只读)</div>
            <el-input :model-value="auth.user?.username" disabled class="field-input" />
          </div>
          <div class="save-row">
            <el-button type="primary" :loading="savingProfile" @click="onSaveProfile">保存</el-button>
            <span v-if="profileOk" class="inline-ok">
              <el-icon><Check /></el-icon>已保存
            </span>
          </div>
        </div>
      </div>

      <!-- 卡片 2:修改密码 -->
      <div class="card block-card">
        <div class="block-title">修改密码</div>
        <el-form
          ref="pwdRef"
          :model="pwdForm"
          :rules="pwdRules"
          label-position="top"
          class="block-body"
          @submit.prevent="onChangePassword"
        >
          <el-form-item label="原密码" prop="oldPassword" :error="oldPwdServerError || undefined">
            <el-input
              v-model="pwdForm.oldPassword"
              type="password"
              show-password
              placeholder="请输入当前密码"
              @input="oldPwdServerError = ''"
            />
          </el-form-item>
          <el-form-item label="新密码" prop="newPassword" :error="newPwdServerError || undefined">
            <el-input
              v-model="pwdForm.newPassword"
              type="password"
              show-password
              placeholder="至少 8 位,建议包含字母与数字"
              @input="newPwdServerError = ''"
            />
          </el-form-item>
          <el-form-item label="确认新密码" prop="confirm">
            <el-input v-model="pwdForm.confirm" type="password" show-password placeholder="再次输入新密码" />
          </el-form-item>
          <el-button type="primary" :loading="changingPwd" native-type="submit">提交修改</el-button>
        </el-form>
      </div>

      <div v-if="joinDays !== null" class="foot-meta">加入问渠已 {{ joinDays }} 天</div>
    </div>
  </div>
</template>

<style scoped>
.profile-wrap {
  max-width: 640px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 8px 0 24px;
}
.block-card {
  padding: 24px;
}
.block-title {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}
.block-body {
  margin-top: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.avatar-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.big-avatar {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: #eef1fe;
  color: #4f6ef2;
  font-size: 20px;
  font-weight: 500;
  display: grid;
  place-items: center;
  overflow: hidden;
  flex-shrink: 0;
}
.big-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.avatar-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.avatar-actions .el-button + .el-button {
  margin-left: 0;
}
.hidden-input {
  display: none;
}
.login-meta {
  margin-left: auto;
  text-align: right;
}
.login-value {
  font-size: 13px;
  color: #111827;
}
.login-src {
  margin-left: 8px;
  font-size: 12px;
  color: #9ca3af;
}

.field-label {
  font-size: 13px;
  color: #374151;
  margin-bottom: 6px;
}
.field-input {
  max-width: 360px;
}

.save-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.inline-ok {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: #10b981;
}

.foot-meta {
  text-align: center;
  font-size: 12px;
  color: #9ca3af;
}
</style>
