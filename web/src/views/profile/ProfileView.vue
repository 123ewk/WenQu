<script setup lang="ts">
import { Check } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'

import { ApiError, errMessage, fieldErrors } from '@/api/http'
import { apiChangePassword } from '@/api/users'
import { useAuthStore } from '@/stores/auth'
import { daysSince } from '@/utils/format'

/**
 * 个人中心(原型 09):基本信息 + 修改密码,居中 640px。
 * - 昵称 PATCH /users/me,成功后行内"已保存";
 * - 改密 POST /users/me/password(204)成功后后端吊销全部会话 → 前端清凭据强制回登录页;
 * - 原型中的"更换头像"依赖的接口 M1 未提供,不渲染假入口(见 对接缺口清单.md)。
 */
const auth = useAuthStore()
const router = useRouter()

const avatarChar = computed(() => (auth.user?.nickname || auth.user?.username || '?').slice(0, 1))
const joinDays = computed(() => daysSince(auth.user?.created_at))

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
            <div class="big-avatar">{{ avatarChar }}</div>
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
