<script setup>
import { computed } from 'vue'

const props = defineProps({ status: { type: String, default: 'unknown' } })

const label = computed(() => ({
  queued: '排队中',
  running: '运行中',
  waiting_input: '等待输入',
  waiting_otp: '等待 OTP',
  retry_wait: '等待重试',
  succeeded: '已成功',
  failed: '已失败',
  cancelled: '已取消',
  needs_review: '待复核',
  not_run: '未执行',
  pending: '待处理',
  changed: '已修改',
  changed_unconfirmed: '已修改待确认',
  setup_unconfirmed: '设置待确认',
  not_changed: '未修改',
  available: '可用',
  no_balance: '余额不足',
  pin_missing: '未设置 PIN',
  relogin_required: '需要重新登录',
  reserved: '使用中',
  payment_success: '支付成功',
  payment_failed: '支付失败',
  active: '已激活',
  completed: '已释放',
  configured: '已配置',
  unavailable: '不可用',
  missing: '未配置',
  rented: '租用中',
  released: '已释放',
  unknown: '未知',
})[props.status] || props.status || '未知')

const tone = computed(() => {
  if (['succeeded', 'available', 'active', 'configured', 'changed', 'payment_success'].includes(props.status)) return 'success'
  if (['running', 'reserved'].includes(props.status)) return 'running'
  if (['queued', 'retry_wait'].includes(props.status)) return 'info'
  if (['waiting_input', 'waiting_otp', 'needs_review', 'pending', 'changed_unconfirmed', 'setup_unconfirmed', 'no_balance'].includes(props.status)) return 'warning'
  if (['failed', 'unavailable', 'missing', 'pin_missing', 'relogin_required', 'payment_failed'].includes(props.status)) return 'danger'
  if (['completed', 'released'].includes(props.status)) return 'info'
  if (['rented'].includes(props.status)) return 'warning'
  return 'neutral'
})
</script>

<template><span class="status-badge" :class="`status-${tone}`"><i />{{ label }}</span></template>
