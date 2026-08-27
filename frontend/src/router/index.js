import { createRouter, createWebHistory } from 'vue-router'
import { loadAuthStatus } from '../composables/useAuth.js'
import AppLayout from '../layouts/AppLayout.vue'
import AccountsView from '../views/AccountsView.vue'
import DashboardView from '../views/DashboardView.vue'
import LoginView from '../views/LoginView.vue'
import NotFoundView from '../views/NotFoundView.vue'
import PaymentsView from '../views/PaymentsView.vue'
import RegistrationView from '../views/RegistrationView.vue'
import SettingsView from '../views/SettingsView.vue'

const router = createRouter({
  history: createWebHistory(),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true, title: '管理员登录' } },
    {
      path: '/',
      component: AppLayout,
      children: [
        { path: '', name: 'dashboard', component: DashboardView, meta: { title: '控制台', subtitle: '查看账号、任务和本地服务状态' } },
        { path: 'registration', name: 'registration', component: RegistrationView, meta: { title: '注册与登录', subtitle: '创建 GoPay 注册或已有账号登录任务' } },
        { path: 'tasks', redirect: { name: 'registration' } },
        { path: 'accounts', name: 'accounts', component: AccountsView, meta: { title: 'GoPay 账号', subtitle: '查看账号状态、余额和刷新任务' } },
        { path: 'payments', name: 'payments', component: PaymentsView, meta: { title: '支付管理', subtitle: '录入 Midtrans 地址并执行可恢复的 GoPay 支付' } },
        { path: 'settings', name: 'settings', component: SettingsView, meta: { title: '系统设置', subtitle: '配置注册登录、区域代理、短信服务和 Worker' } },
      ],
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView, meta: { public: true, title: '页面不存在' } },
  ],
})

router.beforeEach(async (to) => {
  document.title = `${to.meta.title || '控制台'} · GoPay 本地控制台`
  const state = await loadAuthStatus()
  if (to.meta.public) {
    if (to.name === 'login' && state.authenticated) return { name: 'dashboard' }
    return true
  }
  if (!state.authenticated) return { name: 'login', query: { redirect: to.fullPath } }
  return true
})

export default router
