<script setup>
import { ArrowLeft, SearchX } from '@lucide/vue'
</script>

<template>
  <main class="not-found"><SearchX :size="48" /><span>404</span><h1>页面不存在</h1><p>请求的控制台页面已经移动或尚未创建。</p><RouterLink to="/"><ArrowLeft :size="16" />返回控制台</RouterLink></main>
</template>
