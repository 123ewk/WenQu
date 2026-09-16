import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** 公开页(登录页),无需登录态 */
    public?: boolean
    /** 顶栏标题/副标题 */
    title?: string
    desc?: string
    /** 占位页的功能名 */
    feature?: string
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
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/placeholder/PlaceholderView.vue'),
          meta: { title: '对话', desc: '与空间知识库问答,支持引用溯源', feature: '对话' },
        },
        {
          path: 'kb',
          name: 'kb-list',
          component: () => import('@/views/placeholder/PlaceholderView.vue'),
          meta: { title: '知识库', desc: '空间内文档知识库管理', feature: '知识库' },
        },
        {
          path: 'kb/:kbId',
          name: 'kb-detail',
          component: () => import('@/views/placeholder/PlaceholderView.vue'),
          meta: { title: '知识库详情', feature: '知识库详情' },
        },
        {
          path: 'kb/:kbId/chunks',
          name: 'kb-chunks',
          component: () => import('@/views/placeholder/PlaceholderView.vue'),
          meta: { title: '分块预览', feature: '分块预览' },
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
          component: () => import('@/views/placeholder/PlaceholderView.vue'),
          meta: { title: 'API Key', desc: '开放接口凭证管理', feature: 'API Key' },
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
