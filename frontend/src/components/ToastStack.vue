<script setup>
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from '@lucide/vue'
import { dismissToast, toastState } from '../composables/useToast.js'

const icons = { success: CircleCheck, error: CircleAlert, warning: TriangleAlert, info: Info }
</script>

<template>
  <Teleport to="body"><div class="toast-stack" aria-live="polite" aria-atomic="true">
    <TransitionGroup name="toast">
      <article v-for="item in toastState.items" :key="item.id" class="toast" :class="`toast-${item.tone}`" role="status">
        <component :is="icons[item.tone] || Info" :size="19" />
        <span>{{ item.message }}</span>
        <button type="button" title="关闭提示" aria-label="关闭提示" @click="dismissToast(item.id)"><X :size="15" /></button>
      </article>
    </TransitionGroup>
  </div></Teleport>
</template>
