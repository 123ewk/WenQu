<script setup lang="ts">
import * as session from '@/api/session'
import AppSidebar from '@/components/AppSidebar.vue'
import UploadProgress from '@/components/UploadProgress.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()

const collapsed = ref(session.getSidebarCollapsed())

watch(collapsed, (v) => session.setSidebarCollapsed(v))

onMounted(() => {
  // 窄屏(<1360px)自动折叠,宽屏恢复用户选择
  if (window.innerWidth < 1360) collapsed.value = true
  // F5 后静默校验令牌并刷新用户/空间列表
  auth.bootstrap()
})
</script>

<template>
  <div class="app-shell">
    <AppSidebar v-model:collapsed="collapsed" />
    <div class="app-main">
      <header class="app-topbar">
        <h1>{{ route.meta.title }}</h1>
        <span v-if="route.meta.desc" class="topbar-desc">{{ route.meta.desc }}</span>
        <div class="topbar-right">
          <UploadProgress />
        </div>
      </header>
      <main class="app-content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 4px;
}
</style>
