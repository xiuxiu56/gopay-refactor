<script setup>
import { Activity, ChevronDown, LogOut, Menu, UserRound } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from '../components/Sidebar.vue'
import ThemeToggle from '../components/ThemeToggle.vue'
import { useAuth } from '../composables/useAuth.js'
import { connectRealtime, disconnectRealtime, realtimeState } from '../composables/useRealtime.js'
import { useTheme } from '../composables/useTheme.js'

const route = useRoute()
const router = useRouter()
const { authState, signOut } = useAuth()
const { dark, toggleTheme } = useTheme()
const sidebarOpen = ref(false)
const profileOpen = ref(false)

const title = computed(() => route.meta.title || '控制台')
const subtitle = computed(() => route.meta.subtitle || '')
const realtimeText = computed(() => ({
  connected: '实时已连接',
  connecting: '实时连接中',
  reconnecting: '实时重连中',
  closed: '实时已断开',
})[realtimeState.status] || '实时状态未知')

async function logout() {
  try {
    await signOut()
  } finally {
    disconnectRealtime()
    await router.replace({ name: 'login' })
  }
}

function handleKeydown(event) {
  if (event.key === 'Escape') {
    sidebarOpen.value = false
    profileOpen.value = false
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  connectRealtime()
})

onBeforeUnmount(() => {
  disconnectRealtime()
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<template>
  <div class="app-shell">
    <Sidebar :open="sidebarOpen" :dark="dark" @close="sidebarOpen = false" @toggle-theme="toggleTheme" />
    <div class="main-shell">
      <header class="topbar">
        <div class="topbar-heading">
          <button class="mobile-nav icon-button" aria-label="打开导航" @click="sidebarOpen = true"><Menu :size="19" /></button>
          <div><div class="breadcrumb"><span>控制台</span><b>/</b><strong>{{ title }}</strong></div><p>{{ subtitle }}</p></div>
        </div>
        <div class="topbar-actions">
          <span class="realtime-mode" :class="`realtime-${realtimeState.status}`"><i />{{ realtimeText }}</span>
          <span class="local-mode"><Activity :size="14" />本地模式</span>
          <ThemeToggle :dark="dark" @toggle="toggleTheme" />
          <div class="profile-menu">
            <button
              class="profile-trigger"
              :aria-expanded="profileOpen"
              :aria-label="profileOpen ? '关闭管理员菜单' : '打开管理员菜单'"
              :data-tooltip="profileOpen ? '关闭管理员菜单' : '打开管理员菜单'"
              @click="profileOpen = !profileOpen"
            ><span><UserRound :size="16" /></span>{{ authState.admin?.username || '管理员' }}<ChevronDown :size="14" /></button>
            <div v-if="profileOpen" class="profile-dropdown">
              <div><strong>{{ authState.admin?.username }}</strong><small>单管理员本地模式</small></div>
              <button @click="logout"><LogOut :size="15" />退出登录</button>
            </div>
          </div>
        </div>
      </header>
      <main class="page-scroll"><RouterView /></main>
    </div>
  </div>
</template>
