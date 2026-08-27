import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

export function useAdaptiveList(options = {}) {
  const workspaceRef = ref(null)
  const visibleRows = ref(options.initialRows || 5)
  const workspaceHeight = ref(0)
  let resizeObserver = null
  let resizeTimer = 0

  const workspaceStyle = computed(() => ({
    '--adaptive-list-height': workspaceHeight.value ? `${workspaceHeight.value}px` : undefined,
  }))

  function calculate() {
    const element = workspaceRef.value
    if (!element) return visibleRows.value
    if (window.matchMedia(options.stackAt || '(max-width: 980px)').matches) {
      workspaceHeight.value = 0
      visibleRows.value = options.mobileRows || 5
      return visibleRows.value
    }

    const scrollContainer = element.closest('.page-scroll')
    const scrollBottom = scrollContainer?.getBoundingClientRect().bottom || window.innerHeight
    const scrollStyle = scrollContainer ? window.getComputedStyle(scrollContainer) : null
    const bottomPadding = Number.parseFloat(scrollStyle?.paddingBottom || '0') || 0
    const availableHeight = scrollBottom - element.getBoundingClientRect().top - bottomPadding - 2
    const headerHeight = element.querySelector('.panel-heading')?.getBoundingClientRect().height || 61
    const rowHeight = element.querySelector('.compact-row')?.getBoundingClientRect().height || 58
    const minimumRows = options.minRows || 3
    const maximumRows = options.maxRows || 10
    const nextRows = Math.max(
      minimumRows,
      Math.min(maximumRows, Math.floor((availableHeight - headerHeight - 2) / rowHeight)),
    )

    visibleRows.value = nextRows
    workspaceHeight.value = Math.max(headerHeight + minimumRows * rowHeight + 2, Math.floor(availableHeight))
    return nextRows
  }

  function schedule() {
    window.clearTimeout(resizeTimer)
    resizeTimer = window.setTimeout(calculate, 80)
  }

  onMounted(async () => {
    await nextTick()
    const scrollContainer = workspaceRef.value?.closest('.page-scroll')
    resizeObserver = new ResizeObserver(schedule)
    if (scrollContainer) resizeObserver.observe(scrollContainer)
    if (workspaceRef.value) resizeObserver.observe(workspaceRef.value)
    window.addEventListener('resize', schedule)
    window.visualViewport?.addEventListener('resize', schedule)
    schedule()
  })

  onBeforeUnmount(() => {
    window.clearTimeout(resizeTimer)
    resizeObserver?.disconnect()
    window.removeEventListener('resize', schedule)
    window.visualViewport?.removeEventListener('resize', schedule)
  })

  return { workspaceRef, workspaceStyle, visibleRows, calculate, schedule }
}
