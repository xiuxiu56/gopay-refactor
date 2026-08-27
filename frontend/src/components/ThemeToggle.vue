<script setup>
import { Moon, Sun } from '@lucide/vue'

defineProps({ dark: Boolean })
defineEmits(['toggle'])
</script>

<template>
  <button
    type="button"
    class="theme-toggle"
    :title="dark ? '当前为深色模式，切换浅色模式' : '当前为浅色模式，切换深色模式'"
    :aria-label="dark ? '切换浅色模式' : '切换深色模式'"
    @click="$emit('toggle')"
  >
    <Moon v-if="dark" :size="17" />
    <Sun v-else :size="17" class="theme-sun" />
  </button>
</template>
