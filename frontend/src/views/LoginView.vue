<script setup>
import { KeyRound, LoaderCircle, LockKeyhole, ShieldCheck, User, WalletCards } from '@lucide/vue'
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ThemeToggle from '../components/ThemeToggle.vue'
import { useAuth } from '../composables/useAuth.js'
import { useTheme } from '../composables/useTheme.js'
import { useToast } from '../composables/useToast.js'

const route = useRoute()
const router = useRouter()
const { authState, signIn } = useAuth()
const { dark, toggleTheme } = useTheme()
const toast = useToast()
const username = ref('admin')
const password = ref('')
const confirmation = ref('')
const loading = ref(false)

const setupRequired = computed(() => authState.setupRequired)
const title = computed(() => setupRequired.value ? '创建本地管理员' : '欢迎回来')
const submitText = computed(() => setupRequired.value ? '创建管理员并进入' : '登录控制台')

async function submit() {
  if (setupRequired.value && password.value !== confirmation.value) {
    toast.error('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    await signIn(username.value.trim(), password.value, setupRequired.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect)
  } catch (error) {
    toast.error(error.message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <div class="auth-theme"><ThemeToggle :dark="dark" @toggle="toggleTheme" /></div>
    <div class="auth-orb auth-orb-one" /><div class="auth-orb auth-orb-two" />
    <section class="auth-card">
      <div class="auth-story">
        <div class="auth-brand"><span><WalletCards :size="28" /></span><div><strong>GoPay Local Console</strong><small>支付协议与任务控制台</small></div></div>
        <div class="auth-message">
          <span class="glass-pill"><ShieldCheck :size="15" />单管理员本地模式</span>
          <h1>统一管理账号，<br />可靠执行支付任务。</h1>
          <p>集中管理 GoPay 注册登录、账号状态、Midtrans 支付和 OTP 任务。</p>
        </div>
        <div class="auth-features"><div><b>Python</b><span>模块化后端</span></div><div><b>Vue 3</b><span>实时响应前端</span></div><div><b>SQLite WAL</b><span>本地持久化</span></div></div>
      </div>
      <div class="auth-form-wrap">
        <form class="auth-form" @submit.prevent="submit">
          <span class="eyebrow">{{ setupRequired ? '首次运行' : '安全登录' }}</span>
          <h2>{{ title }}</h2>
          <p>{{ setupRequired ? '首次启动需要创建唯一的本地管理员。' : '输入本地管理员账号后进入控制台。' }}</p>
          <div class="auth-fields">
            <label><span>账号</span><div class="input-icon"><User :size="17" /><input v-model="username" class="field" autocomplete="username" placeholder="admin" required /></div></label>
            <label><span>密码</span><div class="input-icon"><KeyRound :size="17" /><input v-model="password" class="field" type="text" autocomplete="off" placeholder="至少 8 位" minlength="8" required /></div></label>
            <label v-if="setupRequired"><span>确认密码</span><div class="input-icon"><LockKeyhole :size="17" /><input v-model="confirmation" class="field" type="text" autocomplete="off" placeholder="再次输入密码" minlength="8" required /></div></label>
          </div>
          <button class="primary-button auth-submit" :disabled="loading"><LoaderCircle v-if="loading" :size="18" class="spin" /><ShieldCheck v-else :size="18" />{{ loading ? '正在处理…' : submitText }}</button>
          <small class="auth-hint">登录状态保存在 HttpOnly Cookie 中，写操作同时校验 CSRF。</small>
        </form>
      </div>
    </section>
  </main>
</template>
