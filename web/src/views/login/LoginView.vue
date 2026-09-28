<script setup lang="ts">
import type { FormInstance, FormRules } from 'element-plus'

import type { DemoStatusOut } from '@/api/types'
import { apiDemoStatus } from '@/api/demo'
import { errMessage, fieldErrors, ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useTourStore } from '@/stores/tour'
import WaterSurface from '@/components/WaterSurface.vue'

/**
 * 登录/注册页 · 「一鉴开」版:深墨水面即镜面(WaterSurface 高度场涟漪模拟),
 * 整页是品牌封面;点「开始提问」或右上「进入提问」弹出登录窗。
 * 登录/注册/演示入口(OPT-46)逻辑与旧版一致;后端错误映射:
 * INVALID_CREDENTIALS → 登录提示;USERNAME_TAKEN → 注册账号字段错误;
 * 422 VALIDATION_ERROR → 字段级提示。
 */
const REPO_URL = 'https://github.com/123ewk/WenQu'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const tour = useTourStore()

const dialogOpen = ref(false)
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

function openLogin(tabName: 'login' | 'reg' = 'login') {
  tab.value = tabName
  loginServerError.value = ''
  dialogOpen.value = true
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
    // OPT-47:演示账号登录后先弹知情确认,再决定是否被导览带着走
    if (demo.value.enabled && loginForm.username.trim() === demo.value.username) {
      tour.armConsent(demo.value)
    }
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

/**
 * OPT-46 演示入口。状态接口免登录;拿不到就当没有(旧版本服务没有这个端点会 404)。
 * 密码不在这里:接口不返回密码,前端也填不了,只把账号名填上去。
 */
const demo = ref<DemoStatusOut>({
  enabled: false,
  username: '',
  space_name: '',
  kb_name: '',
  document_total: 0,
  document_ready: 0,
  ready: false,
})

onMounted(async () => {
  try {
    demo.value = await apiDemoStatus()
  } catch {
    /* 演示模式关闭或服务不支持:什么都不显示 */
  }
})

function fillDemoAccount() {
  tab.value = 'login'
  loginForm.username = demo.value.username
  loginServerError.value = ''
}
</script>

<template>
  <div class="login-page">
    <WaterSurface />
    <div class="vignette" aria-hidden="true" />

    <nav class="nav">
      <div class="brand">问渠 <em>WENQU</em></div>
      <div class="links">
        <a class="pill" @click="openLogin('login')">进入提问</a>
      </div>
    </nav>

    <div class="poem" aria-hidden="true">
      <div class="col">半亩方塘一鉴开</div>
      <div class="col">天光云影共徘徊</div>
      <div class="col hl">问渠那得清如许</div>
      <div class="col hl">为有源头活水来</div>
      <div class="seal">问渠</div>
    </div>

    <div class="center">
      <div class="eyebrow">多租户文档知识库 · RAG 平台</div>
      <h1>问渠</h1>
      <div class="latin">W E N Q U</div>
      <p class="tagline">Ask the stream, and it shall answer.</p>
      <div class="cta">
        <a class="primary" data-testid="login-open" @click="openLogin('login')">开始提问</a>
        <a class="ghost" :href="REPO_URL" target="_blank" rel="noopener">了解问渠</a>
      </div>
    </div>

    <div class="hint">移动光标，向水面提问 · 点击激起涟漪</div>

    <el-dialog
      v-model="dialogOpen"
      width="420px"
      align-center
      class="cover-login-dialog"
      :show-header="false"
      :close-on-click-modal="false"
    >
      <div class="dlg-body">
        <button class="dlg-close" aria-label="关闭" @click="dialogOpen = false">×</button>

        <div class="dlg-brand">
          <div class="dlg-brand-row">
            <span class="logo-mark">问</span>
            <span class="dlg-name">问渠</span>
            <span class="dlg-sub">WenQu</span>
          </div>
          <p class="dlg-slogan">让团队知识随时可问</p>
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
            <el-input v-model="loginForm.username" placeholder="用户名" data-testid="login-username" @input="loginServerError = ''" />
          </el-form-item>
          <el-form-item label="密码" prop="password" :error="loginServerError || undefined">
            <el-input
              v-model="loginForm.password"
              type="password"
              show-password
              placeholder="请输入密码"
              data-testid="login-password"
              @keyup.enter="onLogin"
              @input="loginServerError = ''"
            />
          </el-form-item>
          <el-button
            type="primary"
            class="w-full"
            :loading="submitting"
            native-type="submit"
            data-testid="login-submit"
          >
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

        <!-- 演示入口:后端 APP_DEMO_MODE=1 时才出现,关闭时这里一行 DOM 都没有 -->
        <div v-if="demo.enabled" class="demo-tip" data-testid="demo-tip">
          <template v-if="demo.ready">
            <p class="demo-title">一键体验</p>
            <p class="demo-desc">
              演示空间「{{ demo.space_name }}」已备好 {{ demo.document_ready }} 篇文档,登录后可直接问答。
              密码由讲解人提供。
            </p>
            <el-button class="w-full demo-fill-btn" data-testid="demo-fill" @click="fillDemoAccount">
              填入演示账号
            </el-button>
          </template>
          <template v-else>
            <p class="demo-title" data-testid="demo-not-ready">演示数据未就绪</p>
            <p class="demo-desc">
              演示空间文档入库进度 {{ demo.document_ready }}/{{ demo.document_total }}。
              需要先在服务端执行 make demo-seed。
            </p>
          </template>
        </div>

        <p class="terms">登录即代表同意<a>《服务条款》</a>与<a>《隐私政策》</a></p>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  height: 100vh;
  overflow: hidden;
  background: #04141d;
  font-family: 'KaiTi', 'Kaiti SC', 'STKaiti', 'Noto Serif SC', serif;
  --fg: #e9f4ef;
  --fg-soft: rgba(233, 244, 239, 0.55);
  --line: rgba(233, 244, 239, 0.16);
  --jade: #7fd7c4;
  --jade-deep: #5fbfae;
}
.login-page::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 2;
  pointer-events: none;
  background: radial-gradient(120% 90% at 50% 42%, transparent 52%, rgba(0, 0, 0, 0.38));
}

/* ===== 入场编排:文字依次浮现 ===== */
@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(16px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.eyebrow {
  animation: rise 0.8s 0.15s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
h1 {
  animation: rise 1s 0.3s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
.latin {
  animation: rise 0.9s 0.55s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
.tagline {
  animation: rise 0.9s 0.7s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
.cta {
  animation: rise 0.9s 0.85s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
.poem .col {
  animation: rise 1s 1.05s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
.poem .seal {
  animation: rise 1s 1.3s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
.hint {
  animation: rise 0.9s 1.55s cubic-bezier(0.22, 0.7, 0.3, 1) both;
}
@media (prefers-reduced-motion: reduce) {
  .eyebrow,
  h1,
  .latin,
  .tagline,
  .cta,
  .poem .col,
  .poem .seal,
  .hint {
    animation: none;
  }
}

/* ===== 顶部导航 ===== */
.nav {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 50;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 22px 38px;
  pointer-events: none;
}
.brand {
  font-size: 21px;
  letter-spacing: 6px;
  color: var(--fg);
}
.brand em {
  font-family: Georgia, 'Times New Roman', serif;
  font-style: normal;
  font-size: 11px;
  letter-spacing: 4px;
  color: var(--fg-soft);
}
.links {
  display: flex;
  align-items: center;
}
.links .pill {
  pointer-events: auto;
  font-size: 14px;
  letter-spacing: 3px;
  color: var(--fg);
  background: rgba(233, 244, 239, 0.07);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 7px 20px;
  cursor: pointer;
  transition: all 0.25s;
}
.links .pill:hover {
  border-color: var(--jade);
  color: var(--jade);
}

/* ===== 竖排诗 + 印章 ===== */
.poem {
  position: absolute;
  right: 4.5%;
  top: 50%;
  transform: translateY(-50%);
  z-index: 3;
  display: flex;
  flex-direction: row-reverse;
  gap: 16px;
  align-items: flex-start;
  pointer-events: none;
}
.poem .col {
  writing-mode: vertical-rl;
  font-size: 15px;
  letter-spacing: 12px;
  color: rgba(233, 244, 239, 0.72);
}
.poem .col.hl {
  color: #eef7f4;
  font-size: 16.5px;
}
.seal {
  writing-mode: vertical-rl;
  background: #a83324;
  color: #f6efdf;
  border-radius: 5px;
  font-size: 15px;
  letter-spacing: 5px;
  padding: 9px 5px;
  margin-top: 26px;
  box-shadow: inset 0 0 0 1.5px rgba(246, 239, 223, 0.55), inset 0 0 8px rgba(0, 0, 0, 0.28);
  transform: rotate(-2.5deg);
}

/* ===== 中央标题区 ===== */
.center {
  position: absolute;
  left: 50%;
  top: 45%;
  transform: translate(-50%, -50%);
  z-index: 3;
  text-align: center;
  pointer-events: none;
  width: min(92vw, 860px);
}
.eyebrow {
  font-size: 13px;
  letter-spacing: 7px;
  color: var(--fg-soft);
  margin-bottom: 22px;
}
h1 {
  margin: 0;
  font-size: clamp(76px, 13vw, 164px);
  line-height: 1.04;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-indent: 0.1em;
  color: #eef7f4;
  text-shadow: 0 0 46px rgba(127, 215, 196, 0.28);
}
.latin {
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 14px;
  letter-spacing: 11px;
  text-indent: 11px;
  color: var(--fg-soft);
  margin-top: 14px;
}
.tagline {
  margin: 22px 0 0;
  font-family: Georgia, 'Times New Roman', serif;
  font-style: italic;
  font-size: clamp(15px, 1.8vw, 19px);
  color: var(--fg-soft);
  letter-spacing: 0.04em;
}
.cta {
  margin-top: 34px;
  display: flex;
  gap: 16px;
  justify-content: center;
}
.cta a {
  pointer-events: auto;
  text-decoration: none;
  font-size: 15px;
  letter-spacing: 4px;
  border-radius: 999px;
  padding: 13px 36px;
  cursor: pointer;
  transition: all 0.25s;
}
.cta .primary {
  background: linear-gradient(135deg, var(--jade), var(--jade-deep));
  color: #06231d;
  box-shadow: 0 6px 30px rgba(95, 191, 174, 0.35);
}
.cta .primary:hover {
  transform: translateY(-2px);
  box-shadow: 0 10px 36px rgba(95, 191, 174, 0.5);
}
.cta .ghost {
  border: 1px solid var(--line);
  color: var(--fg);
}
.cta .ghost:hover {
  border-color: var(--jade);
  color: var(--jade);
}

.hint {
  position: absolute;
  left: 50%;
  bottom: 52px;
  transform: translateX(-50%);
  z-index: 3;
  font-size: 13px;
  letter-spacing: 5px;
  color: var(--fg-soft);
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 10px;
  pointer-events: none;
}
.hint::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--jade);
  box-shadow: 0 0 10px var(--jade);
  animation: breath 2.4s ease-in-out infinite;
}
@keyframes breath {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

@media (max-width: 960px) {
  .poem {
    display: none;
  }
}
@media (max-width: 640px) {
  .nav {
    padding: 16px 20px;
  }
  .hint {
    bottom: 70px;
    letter-spacing: 3px;
  }
}
</style>

<!-- 登录窗:dark glass。el-dialog 挂在 body 下,scoped 管不到,用全局样式 + 专属类名隔离 -->
<style>
.cover-login-dialog.el-dialog,
.cover-login-dialog {
  --el-bg-color: #0a232d;
  --el-text-color-primary: #e9f4ef;
  --el-text-color-regular: rgba(233, 244, 239, 0.8);
  --el-text-color-secondary: rgba(233, 244, 239, 0.55);
  --el-border-color: rgba(233, 244, 239, 0.2);
  --el-border-color-light: rgba(233, 244, 239, 0.14);
  --el-fill-color-blur: rgba(233, 244, 239, 0.08);
  --el-fill-color-light: rgba(233, 244, 239, 0.06);
  --el-input-bg-color: rgba(233, 244, 239, 0.05);
  --el-input-text-color: #e9f4ef;
  --el-input-border-color: rgba(233, 244, 239, 0.22);
  --el-color-primary: #5fbfae;
  --el-color-primary-light-3: #82cfbf;
  --el-color-primary-light-5: #a5dfd3;
  --el-color-primary-light-7: #c9ece4;
  --el-color-primary-light-8: #dcf3ee;
  --el-color-primary-light-9: #eef8f5;
  --el-color-primary-dark-2: #4aa898;

  background: rgba(8, 30, 38, 0.92);
  border: 1px solid rgba(127, 215, 196, 0.22);
  border-radius: 14px;
  backdrop-filter: blur(14px);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
  font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif;
}
.cover-login-dialog .el-dialog__header {
  /* 该版本 EP 在 show-header=false 下仍渲染默认关闭钮,与自定义 × 重复,直接藏掉 */
  display: none;
}
.cover-login-dialog .dlg-body {
  padding: 4px 6px 2px;
  position: relative;
}
.cover-login-dialog .dlg-close {
  position: absolute;
  top: -4px;
  right: -2px;
  background: none;
  border: none;
  font-size: 20px;
  line-height: 1;
  color: rgba(233, 244, 239, 0.45);
  cursor: pointer;
  transition: color 0.2s;
}
.cover-login-dialog .dlg-close:hover {
  color: #e9f4ef;
}
.cover-login-dialog .dlg-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  font-family: 'KaiTi', 'Kaiti SC', 'STKaiti', serif;
}
.cover-login-dialog .dlg-brand-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.cover-login-dialog .logo-mark {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: linear-gradient(135deg, #7fd7c4, #5fbfae);
  display: grid;
  place-items: center;
  color: #06231d;
  font-size: 15px;
  font-weight: 600;
}
.cover-login-dialog .dlg-name {
  font-size: 20px;
  letter-spacing: 5px;
  color: #eef7f4;
}
.cover-login-dialog .dlg-sub {
  font-size: 12px;
  color: rgba(233, 244, 239, 0.55);
}
.cover-login-dialog .dlg-slogan {
  margin: 8px 0 0;
  font-size: 13px;
  color: rgba(233, 244, 239, 0.6);
}
.cover-login-dialog .login-tabs {
  margin-top: 12px;
}
.cover-login-dialog .login-tabs .el-tabs__item {
  color: rgba(233, 244, 239, 0.55);
  font-family: 'KaiTi', 'Kaiti SC', 'STKaiti', serif;
  letter-spacing: 3px;
}
.cover-login-dialog .login-tabs .el-tabs__item.is-active {
  color: #7fd7c4;
}
.cover-login-dialog .login-tabs .el-tabs__active-bar {
  background-color: #7fd7c4;
}
.cover-login-dialog .login-tabs .el-tabs__nav-wrap::after {
  height: 1px;
  background-color: rgba(233, 244, 239, 0.12);
}
.cover-login-dialog .login-form {
  margin-top: 20px;
}
.cover-login-dialog .login-form .el-form-item {
  margin-bottom: 16px;
}
.cover-login-dialog .login-form .el-form-item__label {
  padding-bottom: 4px;
  color: rgba(233, 244, 239, 0.75);
}
.cover-login-dialog .el-input__wrapper {
  background: rgba(233, 244, 239, 0.05);
}
.cover-login-dialog .el-button--primary {
  color: #06231d;
}
.cover-login-dialog .el-button:not(.el-button--primary) {
  --el-button-bg-color: transparent;
  --el-button-text-color: #e9f4ef;
  --el-button-border-color: rgba(233, 244, 239, 0.25);
  --el-button-hover-bg-color: rgba(233, 244, 239, 0.08);
  --el-button-hover-text-color: #7fd7c4;
  --el-button-hover-border-color: rgba(127, 215, 196, 0.5);
}
.cover-login-dialog .w-full {
  width: 100%;
  margin-top: 4px;
}
.cover-login-dialog .demo-tip {
  margin-top: 20px;
  padding: 14px 16px;
  border: 1px dashed rgba(127, 215, 196, 0.4);
  border-radius: 10px;
  background: rgba(127, 215, 196, 0.05);
}
.cover-login-dialog .demo-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: #7fd7c4;
}
.cover-login-dialog .demo-desc {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: rgba(233, 244, 239, 0.6);
}
.cover-login-dialog .demo-fill-btn {
  margin-top: 12px;
}
.cover-login-dialog .terms {
  margin: 18px 0 0;
  text-align: center;
  font-size: 12px;
  color: rgba(233, 244, 239, 0.45);
}
.cover-login-dialog .terms a {
  color: inherit;
  cursor: pointer;
}
.cover-login-dialog .terms a:hover {
  color: #7fd7c4;
}
</style>
