import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { setAuthExpiredHandler } from './api/http'
import { useAuthStore } from './stores/auth'
import './styles/main.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)

// 401 刷新链彻底失败(一次性 refresh_token 已失效)→ 清空登录态并跳登录页
setAuthExpiredHandler(() => {
  const auth = useAuthStore()
  auth.resetAuth()
  const current = router.currentRoute.value
  if (current.name !== 'login') {
    router.push({ name: 'login', query: { redirect: current.fullPath } })
  }
  ElMessage.error('登录已失效,请重新登录')
})

app.mount('#app')
