<script setup lang="ts">
import type { FormInstance, FormRules } from 'element-plus'

import { errMessage, fieldErrors, ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

/**
 * 登录/注册页(原型 01):420px 居中卡片,极淡主色几何背景。
 * 后端错误映射:INVALID_CREDENTIALS → 登录提示;USERNAME_TAKEN → 注册账号字段错误;
 * 422 VALIDATION_ERROR → 字段级提示。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const tab = ref<'login' | 'reg'>('login')
const loginRef = ref<FormInstance>()
const regRef = ref<FormInstance>()
const submitting = ref(false)

const loginForm = reactive({ username: '', password: '' })
const regForm = reactive({ nickname: '', username: '', password: '', confirm: '' })
const loginServerError = ref('')
const regServerErrors = reactive<Record<string, string>>({})

const loginRules: FormRules = {
  username: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码长度至少 8 位', trigger: 'blur' },
  ],
}

const regRules: FormRules = {
  nickname: [
    { required: true, message: '请输入昵称', trigger: 'blur' },
    { max: 32, message: '昵称不能超过 32 个字符', trigger: 'blur' },
  ],
  username: [
    { required: true, message: '请输入账号', trigger: 'blur' },
    {
      pattern: /^[a-zA-Z0-9_-]{3,32}$/,
      message: '3-32 位字母/数字/下划线/短横线',
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码长度至少 8 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== regForm.password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

function goWorkspace() {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/chat'
  router.push(redirect)
}

async function onLogin() {
  const valid = await loginRef.value?.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  loginServerError.value = ''
  try {
    await auth.login(loginForm.username.trim(), loginForm.password)
    ElMessage.success('登录成功')
    goWorkspace()
  } catch (e) {
    loginServerError.value = errMessage(e)
  } finally {
    submitting.value = false
  }
}

async function onRegister() {
  const valid = await regRef.value?.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  regServerErrors.username = ''
  try {
    await auth.register(regForm.username.trim(), regForm.nickname.trim(), regForm.password)
    ElMessage.success('注册成功,已自动登录')
    goWorkspace()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'USERNAME_TAKEN') {
      regServerErrors.username = '该账号已被注册'
    } else {
      const fields = fieldErrors((e as { details?: unknown }).details)
      const key = fields.username ? 'username' : fields.nickname ? 'nickname' : fields.password ? 'password' : ''
      if (key) regServerErrors[key] = fields[key]!
      else ElMessage.error(errMessage(e))
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 背景:极淡主色几何色块 -->
    <div class="geo">
      <div class="geo-a"></div>
      <div class="geo-b"></div>
      <div class="geo-c"></div>
      <div class="geo-d"></div>
    </div>

    <div class="login-center">
      <div class="login-card">
        <div class="brand">
          <div class="brand-row">
            <div class="logo-mark">问</div>
            <span class="brand-name">问渠</span>
            <span class="brand-sub">WenQu</span>
          </div>
          <p class="brand-slogan">让团队知识随时可问</p>
        </div>

        <el-tabs v-model="tab" stretch class="login-tabs">
          <el-tab-pane label="登录" name="login" />
          <el-tab-pane label="注册" name="reg" />
        </el-tabs>

        <!-- 登录 -->
        <el-form
          v-show="tab === 'login'"
          ref="loginRef"
          :model="loginForm"
          :rules="loginRules"
          label-position="top"
          class="login-form"
          @submit.prevent="onLogin"
        >
          <el-form-item label="账号" prop="username">
            <el-input v-model="loginForm.username" placeholder="用户名" @input="loginServerError = ''" />
          </el-form-item>
          <el-form-item label="密码" prop="password" :error="loginServerError || undefined">
            <el-input
              v-model="loginForm.password"
              type="password"
              show-password
              placeholder="请输入密码"
              @input="loginServerError = ''"
            />
          </el-form-item>
          <el-button type="primary" class="w-full" :loading="submitting" native-type="submit">
            登 录
          </el-button>
        </el-form>

        <!-- 注册 -->
        <el-form
          v-show="tab === 'reg'"
          ref="regRef"
          :model="regForm"
          :rules="regRules"
          label-position="top"
          class="login-form"
          @submit.prevent="onRegister"
        >
          <el-form-item label="昵称" prop="nickname" :error="regServerErrors.nickname || undefined">
            <el-input v-model="regForm.nickname" placeholder="团队内显示的名称" />
          </el-form-item>
          <el-form-item label="账号" prop="username" :error="regServerErrors.username || undefined">
            <el-input v-model="regForm.username" placeholder="用户名" />
          </el-form-item>
          <el-form-item label="密码" prop="password" :error="regServerErrors.password || undefined">
            <el-input v-model="regForm.password" type="password" show-password placeholder="至少 8 位" />
          </el-form-item>
          <el-form-item label="确认密码" prop="confirm">
            <el-input v-model="regForm.confirm" type="password" show-password placeholder="再次输入密码" />
          </el-form-item>
          <el-button type="primary" class="w-full" :loading="submitting" native-type="submit">
            注 册
          </el-button>
        </el-form>
      </div>

      <p class="terms">登录即代表同意<a>《服务条款》</a>与<a>《隐私政策》</a></p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  height: 100vh;
  overflow: hidden;
  background: #f7f8fa;
  display: grid;
  place-items: center;
}
.geo {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
}
.geo-a {
  position: absolute;
  top: -112px;
  left: -96px;
  width: 440px;
  height: 440px;
  background: rgba(79, 110, 242, 0.05);
  border-radius: 64px;
  transform: rotate(12deg);
}
.geo-b {
  position: absolute;
  bottom: -128px;
  right: -80px;
  width: 520px;
  height: 520px;
  background: rgba(79, 110, 242, 0.04);
  border-radius: 72px;
  transform: rotate(-6deg);
}
.geo-c {
  position: absolute;
  top: 96px;
  right: 160px;
  width: 256px;
  height: 256px;
  border: 1px solid rgba(79, 110, 242, 0.1);
  border-radius: 50%;
}
.geo-d {
  position: absolute;
  bottom: 64px;
  left: 192px;
  width: 16px;
  height: 16px;
  background: rgba(79, 110, 242, 0.1);
}

.login-center {
  position: relative;
}
.login-card {
  width: 420px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 32px 32px 28px;
}
.brand {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.brand-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.logo-mark {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #4f6ef2;
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
}
.brand-name {
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}
.brand-sub {
  font-size: 12px;
  color: #9ca3af;
  margin-left: -4px;
  margin-top: 4px;
}
.brand-slogan {
  margin: 10px 0 0;
  font-size: 13px;
  color: #6b7280;
}

.login-tabs {
  margin-top: 12px;
}
.login-tabs :deep(.el-tabs__header) {
  margin: 0;
}
.login-tabs :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
}

.login-form {
  margin-top: 20px;
}
.login-form :deep(.el-form-item) {
  margin-bottom: 16px;
}
.login-form :deep(.el-form-item__label) {
  padding-bottom: 4px;
}
.w-full {
  width: 100%;
  margin-top: 4px;
}

.terms {
  margin-top: 20px;
  text-align: center;
  font-size: 12px;
  color: #9ca3af;
}
.terms a {
  color: inherit;
  cursor: pointer;
}
.terms a:hover {
  color: #4f6ef2;
}
</style>
