<script setup>
import { Check, ChevronDown } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  options: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: '选择选项' },
  visibleRows: { type: Number, default: 5 },
  disabled: Boolean,
})
const emit = defineEmits(['update:modelValue', 'change'])
const root = ref(null)
const menu = ref(null)
const open = ref(false)
const menuStyle = reactive({ top: '0px', left: '0px', width: '0px', maxHeight: '222px' })
const selected = computed(() => props.options.find((item) => item.value === props.modelValue))

function placeMenu() {
  const rect = root.value?.getBoundingClientRect()
  if (!rect) return
  const rowHeight = 42
  const visible = Math.max(1, props.visibleRows || 5)
  const menuHeight = Math.min(props.options.length || 1, visible) * rowHeight + 12
  const width = rect.width
  const left = Math.min(window.innerWidth - width - 10, Math.max(10, rect.left))
  const top = rect.bottom + menuHeight > window.innerHeight - 10
    ? Math.max(10, rect.top - menuHeight - 7)
    : rect.bottom + 7
  menuStyle.left = `${Math.round(left)}px`
  menuStyle.top = `${Math.round(top)}px`
  menuStyle.width = `${width}px`
  menuStyle.maxHeight = `${visible * rowHeight + 12}px`
}

async function toggle() {
  if (props.disabled) return
  open.value = !open.value
  if (open.value) {
    await nextTick()
    placeMenu()
  }
}

function choose(option) {
  if (option.disabled) return
  emit('update:modelValue', option.value)
  emit('change', option.value)
  open.value = false
}

function closeOutside(event) {
  if (root.value?.contains(event.target) || menu.value?.contains(event.target)) return
  open.value = false
}

function closeMenu() {
  open.value = false
}

function onScroll(event) {
  if (event.target instanceof Node && menu.value?.contains(event.target)) return
  closeMenu()
}

function onKeydown(event) {
  if (event.key === 'Escape') closeMenu()
}

onMounted(() => {
  document.addEventListener('pointerdown', closeOutside, true)
  document.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', closeMenu)
  window.addEventListener('scroll', onScroll, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeOutside, true)
  document.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', closeMenu)
  window.removeEventListener('scroll', onScroll, true)
})
</script>

<template>
  <div ref="root" class="dropdown-select" :class="{ disabled }">
    <button type="button" class="dropdown-trigger" :disabled="disabled" :aria-label="ariaLabel" :aria-expanded="open" aria-haspopup="listbox" @click="toggle">
      <span>{{ selected?.label || '请选择' }}</span><ChevronDown :size="14" :class="{ rotated: open }" />
    </button>
    <Teleport to="body">
      <Transition name="menu">
        <div v-if="open" ref="menu" class="dropdown-menu" :style="menuStyle" role="listbox" :aria-label="ariaLabel" @pointerdown.stop>
          <button v-for="option in options" :key="option.value" type="button" :class="{ selected: option.value === modelValue }" :disabled="option.disabled" role="option" :aria-selected="option.value === modelValue" @click="choose(option)">
            <span><strong>{{ option.label }}</strong><small v-if="option.description">{{ option.description }}</small></span><Check v-if="option.value === modelValue" :size="14" />
          </button>
          <div v-if="!options.length" class="dropdown-empty">暂无可选项</div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
