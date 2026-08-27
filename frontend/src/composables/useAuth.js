import { reactive } from 'vue'
import { api } from '../api/client.js'

export const authState = reactive({
  loaded: false,
  loading: false,
  setupRequired: false,
  authenticated: false,
  admin: null,
})

export async function loadAuthStatus(force = false) {
  if (authState.loading) return authState
  if (authState.loaded && !force) return authState
  authState.loading = true
  try {
    const data = await api('/api/v1/auth/status')
    authState.setupRequired = Boolean(data.setup_required)
    authState.authenticated = Boolean(data.authenticated)
    authState.admin = data.admin || null
    authState.loaded = true
    return authState
  } finally {
    authState.loading = false
  }
}

export async function signIn(username, password, setupRequired = false) {
  const data = await api(setupRequired ? '/api/v1/auth/setup' : '/api/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  authState.loaded = true
  authState.setupRequired = false
  authState.authenticated = true
  authState.admin = data.admin
  return data.admin
}

export async function signOut() {
  await api('/api/v1/auth/logout', { method: 'POST', body: '{}' })
  authState.loaded = true
  authState.authenticated = false
  authState.admin = null
}

export function useAuth() {
  return { authState, loadAuthStatus, signIn, signOut }
}
