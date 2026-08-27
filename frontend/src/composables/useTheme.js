import { ref } from 'vue'

const storageKey = 'gopay_console_theme'
const dark = ref(false)
let initialized = false

function preferredDark() {
  const stored = localStorage.getItem(storageKey)
  if (stored) return stored === 'dark'
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function updateThemeColor(value) {
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', value ? '#020617' : '#f8fafc')
}

export function applyTheme(value, persist = true) {
  dark.value = Boolean(value)
  document.documentElement.classList.toggle('dark', dark.value)
  document.documentElement.style.colorScheme = dark.value ? 'dark' : 'light'
  updateThemeColor(dark.value)
  if (persist) localStorage.setItem(storageKey, dark.value ? 'dark' : 'light')
}

export function initializeTheme() {
  if (initialized) return
  initialized = true
  applyTheme(preferredDark(), false)

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (event) => {
    if (!localStorage.getItem(storageKey)) applyTheme(event.matches, false)
  })
}

export function useTheme() {
  return {
    dark,
    setTheme: applyTheme,
    toggleTheme: () => applyTheme(!dark.value),
  }
}
