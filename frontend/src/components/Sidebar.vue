<script setup>
import {
  ChevronRight,
  CircleDollarSign,
  LayoutDashboard,
  LogIn,
  Settings,
  Smartphone,
  WalletCards,
  X,
} from '@lucide/vue'
import ThemeToggle from './ThemeToggle.vue'

defineProps({ open: Boolean, dark: Boolean })
const emit = defineEmits(['close', 'toggle-theme'])

const items = [
  { name: 'dashboard', label: '控制台', icon: LayoutDashboard },
  { name: 'registration', label: '注册与登录', icon: LogIn },
  { name: 'accounts', label: 'GoPay 账号', icon: Smartphone },
  { name: 'payments', label: '支付管理', icon: CircleDollarSign },
  { name: 'settings', label: '系统设置', icon: Settings },
]
</script>

<template>
  <div v-if="open" class="sidebar-backdrop" @click="emit('close')" />
  <aside class="sidebar" :class="{ 'sidebar-open': open }">
    <div class="brand">
      <RouterLink :to="{ name: 'dashboard' }" class="brand-link" @click="emit('close')">
        <span class="brand-icon"><WalletCards :size="22" /></span>
        <span class="brand-copy"><strong>GoPay Console</strong><small>Python 本地控制台</small></span>
      </RouterLink>
      <button class="sidebar-close icon-button" aria-label="关闭导航" @click="emit('close')"><X :size="18" /></button>
    </div>
    <nav class="sidebar-nav">
      <p>工作区</p>
      <RouterLink v-for="item in items" :key="item.name" :to="{ name: item.name }" class="nav-item" @click="emit('close')">
        <component :is="item.icon" :size="18" />
        <span>{{ item.label }}</span>
        <ChevronRight :size="14" class="nav-chevron" />
      </RouterLink>
    </nav>
    <div class="sidebar-footer">
      <div class="sidebar-footer-top">
        <div class="service-state"><i /><span><strong>本地服务正常</strong><small>SQLite WAL · 固定 Worker</small></span></div>
        <ThemeToggle :dark="dark" @toggle="emit('toggle-theme')" />
      </div>
      <div class="version-row"><span>重构阶段</span><b>v0.6.0 · P5</b></div>
    </div>
  </aside>
</template>
