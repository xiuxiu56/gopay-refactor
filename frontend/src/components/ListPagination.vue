<script setup>
import { ChevronLeft, ChevronRight } from '@lucide/vue'
import { computed } from 'vue'

const props = defineProps({
  page: { type: Number, default: 1 },
  total: { type: Number, default: 0 },
  pageSize: { type: Number, default: 5 },
})
const emit = defineEmits(['change'])
const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const start = computed(() => props.total ? ((props.page - 1) * props.pageSize) + 1 : 0)
const end = computed(() => Math.min(props.page * props.pageSize, props.total))

function go(value) {
  emit('change', Math.max(1, Math.min(pages.value, value)))
}
</script>

<template>
  <footer class="list-pagination" aria-label="列表分页">
    <span>第 {{ page }} / {{ pages }} 页<span class="pagination-range">· 当前 {{ start }}–{{ end }} / {{ total }} 条</span></span>
    <div><button type="button" class="icon-button small" title="上一页" aria-label="上一页" :disabled="page <= 1" @click="go(page - 1)"><ChevronLeft :size="16" /></button><button type="button" class="icon-button small" title="下一页" aria-label="下一页" :disabled="page >= pages" @click="go(page + 1)"><ChevronRight :size="16" /></button></div>
  </footer>
</template>
