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
          // OPT-41:权限档位。刻意不加 adminOnly —— 读是 Viewer+、写是 Admin+,页内控显隐即可;
          // 且守卫文案写死了"审计日志",复用会把提示语弄错。
          path: 'permission-groups',
          name: 'permission-groups',
          component: () => import('@/views/permissionGroups/PermissionGroupsView.vue'),
          meta: { title: '权限档位', desc: '一档 = 一种能做什么 + 能看哪些库 · 写操作需 Admin 及以上' },
        },
        {
          // OPT-27:文档建议 /space/:sid/settings/models,本前端空间上下文统一走侧栏切换器,故用扁平路由
          path: 'settings/models',
          name: 'settings-models',
          component: () => import('@/views/settings/ModelConfigView.vue'),
          meta: { title: '模型配置', desc: '本空间向量化/对话/重排模型配置 · 写操作需 Admin 及以上' },
        },
        {
          path: 'settings/usage',
          name: 'settings-usage',
          component: () => import('@/views/settings/TokenUsageView.vue'),
          meta: { title: 'Token 用量', desc: '对话与向量化的 token 消耗统计 · 按成员汇总需 Admin 及以上' },
        },
        {
          // OPT-49 v2:调用追踪。成员看自己的,Admin+ 看全空间(后端强制),页内不设 adminOnly
          path: 'traces',
          name: 'traces',
          component: () => import('@/views/traces/TraceView.vue'),
          meta: { title: '调用追踪', desc: '问答链路 trace 列表与详情 · 成员看自己的,Admin 及以上可看全空间' },
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
