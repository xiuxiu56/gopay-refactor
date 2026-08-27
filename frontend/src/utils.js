export function formatDate(value, fallback = '—') {
  if (!value) return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return fallback
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function shortID(value, size = 8) {
  const text = String(value || '')
  return text.length > size ? `${text.slice(0, size)}…` : text || '—'
}

export function maskedID(value) {
  const text = String(value || '')
  if (!text) return '—'
  if (text.length <= 12) return text
  return `${text.slice(0, 4)}****${text.slice(-4)}`
}

export function formatBytes(value) {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const TASK_TYPE_LABELS = Object.freeze({
  'account.login': '已有账号登录',
  'account.check_pin': '账号 PIN 检测',
  'account.release_number': '释放短信号码',
  'account.refresh': '账号余额刷新',
  'account.refresh_sms_code': '重新获取最新验证码',
  'account.register': '新账号注册',
  'account.post_register': '注册后激活奖励',
  'system.echo': '队列回显检查',
  'system.sleep': '并发等待检查',
  'system.wait_input': '一次性输入演示',
  'payment.execute': 'GoPay 支付执行',
  'payment.reconcile': '支付状态复核',
  'sms.cancel_activation': '短信号码延迟释放',
})

export function taskTypeLabel(value, fallback = '未知任务') {
  if (!value) return fallback
  return TASK_TYPE_LABELS[value] || value
}
