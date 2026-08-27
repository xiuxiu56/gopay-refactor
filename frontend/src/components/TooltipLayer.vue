<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const visible = ref(false)
const text = ref('')
const placement = ref('bottom')
const tooltip = ref(null)
const left = ref(0)
const top = ref(0)
let activeTarget = null
let showTimer = 0
let observer = null

// 自动为纯图标按钮生成统一中文提示，并保留无障碍名称。
function enhanceIconButtons(root) {
  const buttons = []
  if (root instanceof Element && root.matches('button')) buttons.push(root)
  if (root instanceof Element) buttons.push(...root.querySelectorAll('button'))
  for (const button of buttons) {
    const label = String(
      button.getAttribute('title')
      || button.dataset.tooltip
      || button.getAttribute('aria-label')
      || '',
    ).trim()
    if (!label || button.textContent.trim()) continue
    if (button.dataset.tooltip !== label) button.dataset.tooltip = label
    if (!button.getAttribute('aria-label')) button.setAttribute('aria-label', label)
    if (activeTarget === button && visible.value) text.value = label
    if (button.hasAttribute('title')) button.removeAttribute('title')
  }
}

function findTarget(origin) {
  return origin instanceof Element ? origin.closest('[data-tooltip]') : null
}

async function positionTooltip(target) {
  await nextTick()
  if (!visible.value || activeTarget !== target || !tooltip.value) return
  const targetRect = target.getBoundingClientRect()
  const tooltipRect = tooltip.value.getBoundingClientRect()
  const viewportPadding = 8
  const centeredLeft = targetRect.left + targetRect.width / 2
  left.value = Math.max(
    viewportPadding + tooltipRect.width / 2,
    Math.min(window.innerWidth - viewportPadding - tooltipRect.width / 2, centeredLeft),
  )
  const bottomTop = targetRect.bottom + 7
  if (bottomTop + tooltipRect.height <= window.innerHeight - viewportPadding) {
    placement.value = 'bottom'
    top.value = bottomTop
  } else {
    placement.value = 'top'
    top.value = Math.max(viewportPadding, targetRect.top - tooltipRect.height - 7)
  }
}

function show(target, immediate = false) {
  window.clearTimeout(showTimer)
  activeTarget = target
  const open = () => {
    if (activeTarget !== target || !document.contains(target)) return
    const label = String(target.dataset.tooltip || '').trim()
    if (!label) return
    text.value = label
    visible.value = true
    positionTooltip(target)
  }
  if (immediate) open()
  else showTimer = window.setTimeout(open, 55)
}

function hide(target = null) {
  if (target && activeTarget !== target) return
  window.clearTimeout(showTimer)
  activeTarget = null
  visible.value = false
}

function handlePointerOver(event) {
  const target = findTarget(event.target)
  if (!target || target === activeTarget) return
  show(target)
}

function handlePointerOut(event) {
  const target = findTarget(event.target)
  if (!target || target !== activeTarget) return
  if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return
  hide(target)
}

function handleFocusIn(event) {
  const target = findTarget(event.target)
  if (target) show(target, true)
}

function handleFocusOut(event) {
  const target = findTarget(event.target)
  if (target) hide(target)
}

function handleViewportChange() {
  hide()
}

onMounted(() => {
  enhanceIconButtons(document.body)
  observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === 'attributes') enhanceIconButtons(mutation.target)
      for (const node of mutation.addedNodes) enhanceIconButtons(node)
    }
  })
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['title', 'aria-label'],
  })
  document.addEventListener('pointerover', handlePointerOver)
  document.addEventListener('pointerout', handlePointerOut)
  document.addEventListener('focusin', handleFocusIn)
  document.addEventListener('focusout', handleFocusOut)
  window.addEventListener('scroll', handleViewportChange, true)
  window.addEventListener('resize', handleViewportChange)
})

onBeforeUnmount(() => {
  window.clearTimeout(showTimer)
  observer?.disconnect()
  document.removeEventListener('pointerover', handlePointerOver)
  document.removeEventListener('pointerout', handlePointerOut)
  document.removeEventListener('focusin', handleFocusIn)
  document.removeEventListener('focusout', handleFocusOut)
  window.removeEventListener('scroll', handleViewportChange, true)
  window.removeEventListener('resize', handleViewportChange)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="tooltip">
      <div
        v-if="visible"
        ref="tooltip"
        class="global-tooltip"
        :class="`global-tooltip-${placement}`"
        :style="{ left: `${left}px`, top: `${top}px` }"
        role="tooltip"
      >
        {{ text }}
      </div>
    </Transition>
  </Teleport>
</template>
