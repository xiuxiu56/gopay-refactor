import { createApp } from 'vue'
import App from './App.vue'
import router from './router/index.js'
import { initializeTheme } from './composables/useTheme.js'
import './style.css'

initializeTheme()
createApp(App).use(router).mount('#app')
