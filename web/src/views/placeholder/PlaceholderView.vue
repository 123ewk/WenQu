<script setup lang="ts">
/**
 * 功能占位页:M1 未上线的页面(对话/知识库/分块/API Key)统一渲染此空态,
 * 禁止渲染任何假数据冒充真实功能。
 */
const route = useRoute()
const router = useRouter()

const feature = computed(() => (route.meta.feature as string | undefined) ?? '该功能')
</script>

<template>
  <div class="ph-wrap">
    <div class="ph-inner">
      <svg width="96" height="72" viewBox="0 0 96 72" fill="none" stroke="#9CA3AF" stroke-width="1.5">
        <rect x="24" y="10" width="48" height="40" rx="6" />
        <path d="M34 22h28M34 32h18" />
        <circle cx="62" cy="52" r="9" stroke="#4F6EF2" />
        <path d="M69 59l8 8" stroke="#4F6EF2" />
      </svg>
      <div class="ph-title">「{{ feature }}」功能接口将于后续版本上线</div>
      <div class="ph-desc">本阶段已交付账号体系与多空间管理,可前往体验完整流程</div>
      <div class="ph-actions">
        <el-button @click="router.push('/space')">空间管理</el-button>
        <el-button @click="router.push('/profile')">个人中心</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ph-wrap {
  height: 100%;
  display: grid;
  place-items: center;
}
.ph-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-bottom: 48px;
}
.ph-title {
  margin-top: 20px;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.ph-desc {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
.ph-actions {
  margin-top: 20px;
  display: flex;
  gap: 8px;
}
</style>
