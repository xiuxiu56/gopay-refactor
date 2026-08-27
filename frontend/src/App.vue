<script setup>
import ToastStack from './components/ToastStack.vue'
import TooltipLayer from './components/TooltipLayer.vue'
</script>

<template>
  <RouterView />
  <ToastStack />
  <TooltipLayer />
</template>
