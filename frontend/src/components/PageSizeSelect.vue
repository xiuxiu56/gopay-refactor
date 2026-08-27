<script setup>
import DropdownSelect from './DropdownSelect.vue'

defineProps({ modelValue: { type: Number, default: 5 } })
defineEmits(['update:modelValue', 'change'])

const options = [5, 10, 20, 50, 100].map((value) => ({ value, label: `每页 ${value} 条` }))
</script>

<template>
  <DropdownSelect class="page-size-select" :model-value="modelValue" :options="options" :visible-rows="5" aria-label="每页显示条数" @update:model-value="$emit('update:modelValue', $event)" @change="$emit('change', $event)" />
</template>
