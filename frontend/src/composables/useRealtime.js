import { reactive } from 'vue'

export const realtimeState = reactive({ status: 'closed', lastSequence: 0 })

let source = null
let listenerSequence = 0
const listeners = new Map()

function dispatch(event) {
  let change
  try {
    change = JSON.parse(event.data)
  } catch {
    return
  }
  const sequence = Number(change.sequence || event.lastEventId || 0)
  if (sequence && sequence <= realtimeState.lastSequence) return
  if (sequence) realtimeState.lastSequence = sequence
  listeners.forEach((listener) => {
    if (listener.resources && !listener.resources.has(change.resource)) return
    listener.callback(change)
  })
}

export function connectRealtime() {
  if (source || typeof window.EventSource !== 'function') return
  realtimeState.status = 'connecting'
  source = new window.EventSource('/api/v1/realtime')
  source.addEventListener('open', () => {
    realtimeState.status = 'connected'
  })
  source.addEventListener('error', () => {
    if (source) realtimeState.status = 'reconnecting'
  })
  source.addEventListener('task.updated', dispatch)
  source.addEventListener('account.updated', dispatch)
}

export function disconnectRealtime() {
  if (source) {
    source.close()
    source = null
  }
  realtimeState.status = 'closed'
  realtimeState.lastSequence = 0
}

export function subscribeRealtime(resources, callback) {
  const normalized = resources
    ? new Set((Array.isArray(resources) ? resources : [resources]).map(String))
    : null
  const id = ++listenerSequence
  listeners.set(id, { resources: normalized, callback })
  return () => listeners.delete(id)
}
