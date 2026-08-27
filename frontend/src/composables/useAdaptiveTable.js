import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

export function useAdaptiveTable(options = {}) {
  const viewportRef = ref(null)
  const visibleRows = ref(options.initialRows || 8)
  const viewportHeight = ref(0)
  const emptyHeight = ref(0)
  let resizeObserver = null
  let resizeTimer = 0
  let scrollContainer = null
  let observedViewport = null

  const viewportStyle = computed(() => ({
    height: viewportHeight.value ? `${viewportHeight.value}px` : undefined,
    '--table-empty-height': emptyHeight.value ? `${emptyHeight.value}px` : undefined,
  }))

  function calculate() {
    const element = viewportRef.value
    if (!element) return visibleRows.value

    scrollContainer = element.closest('.page-scroll')
    if (observedViewport !== element) {
      if (observedViewport) resizeObserver?.unobserve(observedViewport)
      observedViewport = element
      observedViewport.scrollLeft = 0
      resizeObserver?.observe(observedViewport)
      if (scrollContainer) resizeObserver?.observe(scrollContainer)
    }
    const viewportTop = element.getBoundingClientRect().top
    const scrollBottom = scrollContainer?.getBoundingClientRect().bottom || window.innerHeight
    const scrollStyle = scrollContainer ? window.getComputedStyle(scrollContainer) : null
    const bottomPadding = Number.parseFloat(scrollStyle?.paddingBottom || '0') || 0
    const headerHeight = element.querySelector('thead')?.getBoundingClientRect().height || options.headerHeight || 35
    const dataRow = element.querySelector('tbody tr:not(.adaptive-empty-row)')
    const rowHeight = dataRow?.getBoundingClientRect().height || options.rowHeight || 47
    const emptyCell = element.querySelector('tbody tr.adaptive-empty-row td')
    const emptyCellStyle = emptyCell ? window.getComputedStyle(emptyCell) : null
    const emptyCellChrome = emptyCellStyle
      ? ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth']
        .reduce((total, name) => total + (Number.parseFloat(emptyCellStyle[name]) || 0), 0)
      : 0
    const tableWidth = element.querySelector('table')?.scrollWidth || 0
    const horizontalScrollbar = tableWidth > element.clientWidth + 1 ? 9 : 0
    const reservedHeight = horizontalScrollbar + 1
    const mobile = window.matchMedia('(max-width: 700px)').matches
    const minimumRows = mobile ? options.mobileMinRows || 3 : options.minRows || 5
    const maximumRows = options.maxRows || 200
    const availableHeight = scrollBottom - viewportTop - bottomPadding - 2
    const nextRows = Math.max(
      minimumRows,
      Math.min(maximumRows, Math.floor((availableHeight - headerHeight - reservedHeight) / rowHeight)),
    )
    const minimumHeight = headerHeight + minimumRows * rowHeight + reservedHeight
    const fittedHeight = headerHeight + nextRows * rowHeight + reservedHeight
    const nextHeight = Math.max(
      minimumHeight,
      options.fitWholeRows ? Math.min(Math.floor(availableHeight), fittedHeight) : Math.floor(availableHeight),
    )

    visibleRows.value = nextRows
    emptyHeight.value = Math.max(
      rowHeight,
      nextHeight - headerHeight - reservedHeight - emptyCellChrome,
    )
    viewportHeight.value = nextHeight
    return nextRows
  }

  function schedule() {
    window.clearTimeout(resizeTimer)
    resizeTimer = window.setTimeout(calculate, 80)
  }

  onMounted(async () => {
    await nextTick()
    scrollContainer = viewportRef.value?.closest('.page-scroll') || null
    resizeObserver = new ResizeObserver(schedule)
    if (scrollContainer) resizeObserver.observe(scrollContainer)
    if (viewportRef.value) {
      observedViewport = viewportRef.value
      observedViewport.scrollLeft = 0
      resizeObserver.observe(observedViewport)
    }
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

  return { viewportRef, viewportStyle, visibleRows, calculate, schedule }
}
