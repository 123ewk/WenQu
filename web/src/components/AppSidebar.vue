<script setup lang="ts">
import type { PopoverInstance } from 'element-plus'
import {
  ChatLineRound,
  Check,
  Collection,
  DCaret,
  Document,
  Expand,
  Fold,
  Key,
  OfficeBuilding,
  Plus,
  Setting,
  SwitchButton,
  User,
} from '@element-plus/icons-vue'

import CreateSpaceDialog from '@/components/CreateSpaceDialog.vue'
import { errMessage } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { roleLabel } from '@/utils/roles'

const collapsed = defineModel<boolean>('collapsed', { default: false })
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const spacePop = ref<PopoverInstance>()
const userPop = ref<PopoverInstance>()
const createOpen = ref(false)

const avatarChar = computed(() => (auth.user?.nickname || auth.user?.username || '?').slice(0, 1))

function toggleCollapse() {
  collapsed.value = !collapsed.value
}

async function onSwitchSpace(id: string) {
  spacePop.value?.hide()
  if (id === auth.currentSpaceId) return
  try {
    await auth.switchSpace(id)
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

function onManageSpace() {
  spacePop.value?.hide()
  router.push('/space')
}

function onProfile() {
  userPop.value?.hide()
  router.push('/profile')
}

async function onLogout() {
  userPop.value?.hide()
  try {
    await auth.logout()
    router.push({ name: 'login' })
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}
</script>

<template>
  <aside class="app-sidebar" :class="{ collapsed }">
    <!-- Logo -->
    <div class="sb-logo">
      <div class="logo-mark">问</div>
      <div class="sb-text logo-name">
        <div class="name">问渠</div>
        <div class="sub">WenQu</div>
      </div>
      <button class="icon-btn" :title="collapsed ? '展开侧边栏' : '折叠侧边栏'" @click="toggleCollapse">
        <el-icon v-if="!collapsed" class="sb-text"><Fold /></el-icon>
        <el-icon v-else><Expand /></el-icon>
      </button>
    </div>

    <!-- 空间切换器 -->
    <div class="sb-space">
      <el-popover ref="spacePop" placement="bottom-start" :width="232" trigger="click" popper-class="menu-pop">
        <template #reference>
          <button class="space-btn">
            <el-icon class="space-ic"><OfficeBuilding /></el-icon>
            <span class="sb-text space-info">
              <span class="space-name">{{ auth.currentSpace?.name ?? '未选择空间' }}</span>
              <span class="space-role">{{ auth.role ? roleLabel(auth.role) : '点击选择' }}</span>
            </span>
            <el-icon v-if="!collapsed" class="sb-text space-caret"><DCaret /></el-icon>
          </button>
        </template>

        <div class="pop-label">我的空间</div>
        <div v-if="auth.spaces.length === 0" class="pop-empty">还没有空间,创建一个开始使用</div>
        <button
          v-for="s in auth.spaces"
          :key="s.id"
          class="pop-item"
          @click="onSwitchSpace(s.id)"
        >
          <el-icon v-if="s.id === auth.currentSpaceId" class="ic-check"><Check /></el-icon>
          <span v-else class="ic-ph"></span>
          <span class="pop-name">{{ s.name }}</span>
          <span class="pop-side">{{ roleLabel(s.role) }}</span>
        </button>
        <div class="pop-divider"></div>
        <button class="pop-item pop-primary" @click="spacePop?.hide(); createOpen = true">
          <el-icon><Plus /></el-icon>新建空间
        </button>
        <button class="pop-item" :disabled="!auth.currentSpaceId" @click="onManageSpace">
          <el-icon><Setting /></el-icon>空间管理
        </button>
      </el-popover>
    </div>

    <!-- 主导航 -->
    <nav class="sb-nav">
      <router-link to="/chat" class="nav-item" active-class="nav-active">
        <el-icon><ChatLineRound /></el-icon><span class="sb-text">对话</span>
      </router-link>
      <router-link to="/kb" class="nav-item" active-class="nav-active">
        <el-icon><Collection /></el-icon><span class="sb-text">知识库</span>
      </router-link>
      <router-link to="/api-keys" class="nav-item" active-class="nav-active">
        <el-icon><Key /></el-icon><span class="sb-text">API Key</span>
      </router-link>
      <router-link v-if="auth.isAdmin" to="/audit" class="nav-item" active-class="nav-active">
        <el-icon><Document /></el-icon><span class="sb-text">审计日志</span>
      </router-link>
    </nav>

    <!-- 用户菜单 -->
    <div class="sb-user">
      <el-popover ref="userPop" placement="top-start" :width="180" trigger="click" popper-class="menu-pop">
        <template #reference>
          <button class="user-btn">
            <span class="avatar">{{ avatarChar }}</span>
            <span class="sb-text user-info">
              <span class="user-name">{{ auth.user?.nickname ?? '未登录' }}</span>
              <span class="user-sub">
                {{ auth.currentSpace ? `${auth.currentSpace.name} · ${roleLabel(auth.role!)}` : '未选择空间' }}
              </span>
            </span>
          </button>
        </template>
        <button class="pop-item" :class="{ 'pop-on': route.name === 'profile' }" @click="onProfile">
          <el-icon><User /></el-icon>个人中心
        </button>
        <button class="pop-item pop-danger" @click="onLogout">
          <el-icon><SwitchButton /></el-icon>退出登录
        </button>
      </el-popover>
    </div>

    <CreateSpaceDialog v-model="createOpen" />
  </aside>
</template>

<style scoped>
.app-sidebar {
  width: 240px;
  flex-shrink: 0;
  height: 100%;
  background: #fff;
  border-right: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  transition: width 0.2s;
  position: relative;
  z-index: 30;
}
.app-sidebar.collapsed {
  width: 64px;
}
.app-sidebar.collapsed .sb-text {
  display: none;
}
.app-sidebar.collapsed .space-btn,
.app-sidebar.collapsed .user-btn {
  justify-content: center;
  padding: 0;
}

.sb-logo {
  height: 56px;
  padding: 0 16px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.logo-mark {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: #4f6ef2;
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}
.logo-name {
  line-height: 1.2;
  min-width: 0;
}
.logo-name .name {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.logo-name .sub {
  font-size: 10px;
  color: #9ca3af;
  margin-top: 2px;
}
.icon-btn {
  margin-left: auto;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: none;
  display: grid;
  place-items: center;
  color: #6b7280;
  cursor: pointer;
  flex-shrink: 0;
}
.icon-btn:hover {
  background: #f7f8fa;
  color: #111827;
}

.sb-space {
  padding: 4px 12px 0;
  flex-shrink: 0;
}
.space-btn {
  width: 100%;
  height: 44px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #fff;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.space-btn:hover {
  background: #f7f8fa;
}
.space-ic {
  color: #6b7280;
  flex-shrink: 0;
}
.space-info {
  flex: 1;
  min-width: 0;
  text-align: left;
  line-height: 1.25;
}
.space-name {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: #111827;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.space-role {
  display: block;
  font-size: 11px;
  color: #9ca3af;
}
.space-caret {
  color: #9ca3af;
  flex-shrink: 0;
}

.sb-nav {
  padding: 16px 12px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  overflow-y: auto;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 12px;
  border-radius: 8px;
  font-size: 14px;
  color: #374151;
  text-decoration: none;
  transition: background 0.15s;
}
.nav-item:hover {
  background: #f7f8fa;
}
.nav-item.nav-active {
  background: #eef1fe;
  color: #4f6ef2;
  font-weight: 500;
}

.sb-user {
  padding: 12px;
  border-top: 1px solid #e5e7eb;
  flex-shrink: 0;
}
.user-btn {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px;
  border: none;
  border-radius: 8px;
  background: none;
  cursor: pointer;
}
.user-btn:hover {
  background: #f7f8fa;
}
.avatar {
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
.user-info {
  flex: 1;
  min-width: 0;
  text-align: left;
  line-height: 1.25;
}
.user-name {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: #111827;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-sub {
  display: block;
  font-size: 11px;
  color: #9ca3af;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pop-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ic-check {
  color: #4f6ef2;
  flex-shrink: 0;
}
.pop-item.pop-on {
  background: #eef1fe;
  color: #4f6ef2;
}
.pop-item:disabled {
  color: #9ca3af;
  cursor: not-allowed;
}
</style>
