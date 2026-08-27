import { reactive } from 'vue'

export const toastState = reactive({ items: [] })
const timers = new Map()
let sequence = 0

function schedule(id, timeout) {
  const prior = timers.get(id)
  if (prior) window.clearTimeout(prior)
  timers.delete(id)
  if (timeout > 0) timers.set(id, window.setTimeout(() => dismissToast(id), timeout))
}

export function showToast(message, tone = 'info', timeout = 3800) {
  while (toastState.items.length >= 3) dismissToast(toastState.items[0].id)
  const id = ++sequence
  toastState.items.push({ id, message: String(message || ''), tone, persistent: timeout <= 0 })
  schedule(id, timeout)
  return id
}

export function updateToast(id, message, tone = 'info', timeout = 3800) {
  const item = toastState.items.find((candidate) => candidate.id === id)
  if (!item) return showToast(message, tone, timeout)
  item.message = String(message || '')
  item.tone = tone
  item.persistent = timeout <= 0
  schedule(id, timeout)
  return id
}

export function dismissToast(id) {
  const timer = timers.get(id)
  if (timer) window.clearTimeout(timer)
  timers.delete(id)
  const index = toastState.items.findIndex((item) => item.id === id)
  if (index >= 0) toastState.items.splice(index, 1)
}

export function useToast() {
  return {
    items: toastState.items,
    dismiss: dismissToast,
    update: updateToast,
    info: (message, timeout = 3800) => showToast(message, 'info', timeout),
    success: (message, timeout = 4200) => showToast(message, 'success', timeout),
    warning: (message, timeout = 6200) => showToast(message, 'warning', timeout),
    error: (message, timeout = 7200) => showToast(message, 'error', timeout),
  }
}
