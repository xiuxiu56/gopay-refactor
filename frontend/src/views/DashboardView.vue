<script setup>
import { ArrowRight, CircleDollarSign, ClipboardList, Database, LoaderCircle, Smartphone, UsersRound } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import StatusBadge from '../components/StatusBadge.vue'
import { api } from '../api/client.js'
import { useAdaptiveList } from '../composables/useAdaptiveList.js'
import { subscribeRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { formatDate, shortID, taskTypeLabel } from '../utils.js'

const loading = ref(true)
const status = ref(null)
const accounts = ref({ items: [], total: 0 })
const tasks = ref({ items: [], total: 0 })
const payments = ref({ items: [], total: 0 })
const toast = useToast()
const { workspaceRef, workspaceStyle, visibleRows, schedule: scheduleWorkspace } = useAdaptiveList()
let unsubscribe = () => {}
let refreshTimer = 0
let mounted = false

const runningTasks = computed(() => tasks.value.items.filter((item) => ['running', 'queued', 'retry_wait', 'waiting_input'].includes(item.status)).length)

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [systemData, accountData, taskData, paymentData] = await Promise.all([
      api('/api/v1/system/status'),
      api(`/api/v1/accounts?limit=${visibleRows.value}`),
      api(`/api/v1/tasks?limit=${visibleRows.value}`),
      api('/api/v1/payments?limit=5'),
    ])
    status.value = systemData
    accounts.value = accountData
    tasks.value = taskData
    payments.value = paymentData
  } catch (error) {
    toast.error(error.message)
  } finally {
    if (!silent) loading.value = false
    await nextTick()
    scheduleWorkspace()
  }
}

onMounted(() => {
  mounted = true
  load()
  unsubscribe = subscribeRealtime(['task', 'account', 'payment'], () => {
    window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => load(true), 120)
  })
})

onBeforeUnmount(() => {
  mounted = false
  unsubscribe()
  window.clearTimeout(refreshTimer)
})

watch(visibleRows, (value, previous) => {
  if (mounted && value !== previous) load(true)
})
</script>

<template>
  <div v-if="loading" class="page-loading"><LoaderCircle :size="24" class="spin" />正在加载控制台</div>
  <div v-else class="page dashboard-page">
    <section class="welcome-panel">
      <div><span class="eyebrow">P5 · Python 重构</span><h1>今天的本地服务运行正常。</h1><p>账号、支付、任务状态和实时事件全部由 SQLite WAL 持久化。</p></div>
      <RouterLink :to="{ name: 'registration' }" class="primary-button">进入注册与登录<ArrowRight :size="16" /></RouterLink>
    </section>
    <section class="metric-grid">
      <article><span class="metric-icon teal"><UsersRound :size="20" /></span><div><small>GoPay 账号</small><strong>{{ accounts.total }}</strong><p>已迁移和已保存账号</p></div></article>
      <article><span class="metric-icon blue"><ClipboardList :size="20" /></span><div><small>任务总数</small><strong>{{ tasks.total }}</strong><p>{{ runningTasks }} 个正在处理</p></div></article>
      <article><span class="metric-icon amber"><CircleDollarSign :size="20" /></span><div><small>支付意图</small><strong>{{ payments.total }}</strong><p>Midtrans 本地状态</p></div></article>
      <article><span class="metric-icon violet"><Database :size="20" /></span><div><small>Worker 池</small><strong>{{ status?.worker_pool?.alive_workers || 0 }}/{{ status?.worker_count || 0 }}</strong><p>SQLite {{ String(status?.journal_mode || '').toUpperCase() }}</p></div></article>
    </section>
    <section ref="workspaceRef" class="dashboard-columns" :style="workspaceStyle">
      <article class="panel table-panel">
        <header class="panel-heading"><div><h2>最近任务</h2><p>注册与登录队列的最新状态</p></div><RouterLink :to="{ name: 'registration' }">查看日志<ArrowRight :size="14" /></RouterLink></header>
        <div class="compact-list">
          <div v-for="task in tasks.items" :key="task.id" class="compact-row"><span class="row-icon"><ClipboardList :size="16" /></span><div><strong>{{ taskTypeLabel(task.task_type) }}</strong><small>{{ shortID(task.id) }} · {{ formatDate(task.updated_at) }}</small></div><StatusBadge :status="task.status" /></div>
          <div v-if="!tasks.items.length" class="empty-compact">还没有任务记录</div>
        </div>
      </article>
      <article class="panel table-panel">
        <header class="panel-heading"><div><h2>账号概览</h2><p>余额和 PIN 配置状态</p></div><RouterLink :to="{ name: 'accounts' }">管理账号<ArrowRight :size="14" /></RouterLink></header>
        <div class="compact-list">
          <div v-for="account in accounts.items" :key="account.id" class="compact-row"><span class="row-icon"><Smartphone :size="16" /></span><div><strong>{{ account.phone }}</strong><small>{{ account.balance }} Rp · 更新于 {{ formatDate(account.updated_at) }}</small></div><StatusBadge :status="account.pin_setup_status" /></div>
          <div v-if="!accounts.items.length" class="empty-compact">还没有 GoPay 账号</div>
        </div>
      </article>
    </section>
  </div>
</template>
