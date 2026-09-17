import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** 公开页(登录页),无需登录态 */
    public?: boolean
    /** 顶栏标题/副标题 */
    title?: string
    desc?: string
    /** 仅当前空间 Admin 及以上可访问 */
    adminOnly?: boolean
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/login/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        { path: '', redirect: '/chat' },
        {
          path: 'chat/:conversationId?',
          name: 'chat',
          component: () => import('@/views/chat/ChatView.vue'),
          meta: { title: '对话', desc: '与当前空间知识库问答,回答自带引用溯源' },
        },
        {
          path: 'kb',
          name: 'kb-list',
          component: () => import('@/views/kb/KbListView.vue'),
          meta: { title: '知识库', desc: '空间内文档知识库的创建与文档入库' },
        },
        {
          path: 'kb/:kbId',
          name: 'kb-detail',
          component: () => import('@/views/kb/KbDetailView.vue'),
          meta: { title: '知识库详情', desc: '文档入库状态、检索测试与知识库设置' },
        },
        {
          path: 'kb/:kbId/chunks/:docId',
          name: 'kb-chunks',
          component: () => import('@/views/kb/ChunkPreviewView.vue'),
          meta: { title: '分块预览', desc: '查看文档分块内容与溯源元数据' },
        },
        {
          path: 'space',
          name: 'space',
          component: () => import('@/views/space/SpaceView.vue'),
          meta: { title: '空间管理', desc: '空间内成员与空间信息由 Admin 及以上管理' },
        },
        {
          path: 'api-keys',
          name: 'api-keys',
          component: () => import('@/views/apiKeys/ApiKeysView.vue'),
          meta: { title: 'API Key', desc: '空间内程序化接入凭证管理' },
        },
        {
          path: 'audit',
          name: 'audit',
          component: () => import('@/views/audit/AuditView.vue'),
          meta: { title: '审计日志', desc: '空间内操作记录 · 仅 Admin 及以上可见', adminOnly: true },
        },
        {
          path: 'profile',
          name: 'profile',
          component: () => import('@/views/profile/ProfileView.vue'),
          meta: { title: '个人中心' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已登录访问登录页 → 回工作台
    if (auth.isLoggedIn) return { path: '/chat' }
    return true
  }
  if (!auth.isLoggedIn) {
    return { name: 'login', query: to.fullPath === '/' ? undefined : { redirect: to.fullPath } }
  }
  if (to.meta.adminOnly && !auth.isAdmin) {
    ElMessage.warning('审计日志仅空间 Admin 及以上可访问')
    return { path: '/chat' }
  }
  return true
})

export default router
