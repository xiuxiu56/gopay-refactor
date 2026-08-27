<script setup>
import { Braces, CircleAlert, ClipboardList, Eye, KeyRound, LoaderCircle, Plus, RefreshCw, RotateCcw, Square, X } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import DropdownSelect from '../components/DropdownSelect.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { api, queryString } from '../api/client.js'
import { useAdaptiveTable } from '../composables/useAdaptiveTable.js'
import { subscribeRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { formatDate, shortID, taskTypeLabel } from '../utils.js'

const loading = ref(true)
const tasks = ref([])
const total = ref(0)
const types = ref([])
const filters = reactive({ status: '', taskType: '' })
const createOpen = ref(false)
const detailOpen = ref(false)
const inputOpen = ref(false)
const busy = ref('')
const selected = ref(null)
const events = ref([])
const createForm = reactive({ taskType: 'system.echo', payload: '{\n  "value": "队列运行正常"\n}', priority: 0, maxAttempts: 3, idempotencyKey: '' })
const inputForm = reactive({ inputType: 'otp', value: '' })
const toast = useToast()
const { viewportRef, viewportStyle, visibleRows, schedule: scheduleTable } = useAdaptiveTable()
let unsubscribe = () => {}
let refreshTimer = 0
let mounted = false

const presets = {
  'system.echo': { value: '队列运行正常' },
  'system.sleep': { seconds: 1 },
  'system.wait_input': { input_type: 'otp', timeout_seconds: 300 },
  'account.refresh': { account_id: '' },
}

const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'waiting_input', label: '等待输入' },
  { value: 'retry_wait', label: '等待重试' },
  { value: 'succeeded', label: '已成功' },
  { value: 'failed', label: '已失败' },
  { value: 'cancelled', label: '已取消' },
  { value: 'needs_review', label: '待复核' },
]
const taskTypeOptions = computed(() => [
  { value: '', label: '全部类型' },
  ...types.value.map((item) => ({
    value: item.task_type,
    label: taskTypeLabel(item.task_type),
    description: item.description,
  })),
])
const createTypeOptions = computed(() => types.value.map((item) => ({
  value: item.task_type,
  label: taskTypeLabel(item.task_type),
  description: item.description,
})))
const inputTypeOptions = [
  { value: 'otp', label: 'OTP 验证码', description: '提交一次性短信验证码' },
  { value: 'pin', label: 'PIN', description: '提交账号 PIN' },
]

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [taskData, typeData] = await Promise.all([
      api(`/api/v1/tasks${queryString({ status: filters.status, task_type: filters.taskType, limit: visibleRows.value })}`),
      api('/api/v1/tasks/types'),
    ])
    tasks.value = taskData.items || []
    total.value = taskData.total || 0
    types.value = typeData || []
    if (!types.value.some((item) => item.task_type === createForm.taskType) && types.value.length) {
      setCreateType(types.value[0].task_type)
    }
  } catch (error) {
    toast.error(error.message)
  } finally {
    if (!silent) loading.value = false
    await nextTick()
    scheduleTable()
  }
}

function setCreateType(value) {
  createForm.taskType = value
  createForm.payload = JSON.stringify(presets[value] || {}, null, 2)
}

async function createTask() {
  let payload
  try {
    payload = JSON.parse(createForm.payload || '{}')
  } catch {
    toast.warning('任务载荷需要是有效 JSON 对象')
    return
  }
  busy.value = 'create'
  try {
    const data = await api('/api/v1/tasks', {
      method: 'POST',
      body: JSON.stringify({
        task_type: createForm.taskType,
        payload,
        priority: Number(createForm.priority),
        max_attempts: Number(createForm.maxAttempts),
        idempotency_key: createForm.idempotencyKey.trim() || null,
      }),
    })
    createOpen.value = false
    toast.success(data.created ? '任务已进入持久化队列' : '幂等任务已经存在，已返回原任务')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function showDetail(task) {
  busy.value = `detail-${task.id}`
  try {
    const data = await api(`/api/v1/tasks/${task.id}?event_limit=300`)
    selected.value = data.task
    events.value = data.events || []
    detailOpen.value = true
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function cancelTask(task) {
  if (!window.confirm(`确认取消任务 ${shortID(task.id)}？`)) return
  busy.value = `cancel-${task.id}`
  try {
    await api(`/api/v1/tasks/${task.id}/cancel`, { method: 'POST', body: '{}' })
    toast.success('任务已取消')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function retryTask(task) {
  busy.value = `retry-${task.id}`
  try {
    await api(`/api/v1/tasks/${task.id}/retry`, { method: 'POST', body: '{}' })
    toast.success('任务已重新进入队列')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

function openInput(task) {
  selected.value = task
  inputForm.inputType = 'otp'
  inputForm.value = ''
  inputOpen.value = true
}

async function submitInput() {
  busy.value = 'input'
  try {
    await api(`/api/v1/tasks/${selected.value.id}/input`, {
      method: 'POST',
      body: JSON.stringify({ input_type: inputForm.inputType, value: inputForm.value }),
    })
    inputOpen.value = false
    toast.success('一次性输入已加密提交')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  mounted = true
  load()
  unsubscribe = subscribeRealtime('task', () => {
    window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => load(true), 100)
  })
})

onBeforeUnmount(() => {
  mounted = false
  unsubscribe()
  window.clearTimeout(refreshTimer)
})

watch(visibleRows, (value, previous) => {
  if (mounted && value !== previous) load(true)
})
</script>

<template>
  <div class="page task-center-page">
    <section class="panel table-panel full-height-panel">
      <header class="command-bar">
        <div class="filter-group">
          <div class="task-filter-control"><ClipboardList :size="16" /><DropdownSelect v-model="filters.status" :options="statusOptions" :visible-rows="5" aria-label="任务状态" @change="load()" /></div>
          <div class="task-filter-control task-type-filter"><Braces :size="16" /><DropdownSelect v-model="filters.taskType" :options="taskTypeOptions" :visible-rows="5" aria-label="任务类型" @change="load()" /></div>
          <span class="result-count">显示 {{ tasks.length }} 条，共 {{ total }} 条任务</span>
        </div>
        <div class="command-actions"><button class="icon-button" title="刷新任务" @click="load()"><RefreshCw :size="16" /></button><button class="primary-button" @click="createOpen = true"><Plus :size="16" />创建任务</button></div>
      </header>
      <div v-if="loading" class="table-loading"><LoaderCircle :size="22" class="spin" />正在读取任务</div>
      <div v-else ref="viewportRef" class="table-scroll adaptive-table" :class="{ 'is-empty': !tasks.length }" :style="viewportStyle">
        <table class="data-table task-table">
          <thead><tr><th>任务</th><th>类型</th><th>状态</th><th>进度</th><th>尝试</th><th>错误信息</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="task in tasks" :key="task.id"><td><code :title="task.id">{{ shortID(task.id, 10) }}</code></td><td><strong class="type-name">{{ taskTypeLabel(task.task_type) }}</strong></td><td><StatusBadge :status="task.status" /></td><td><div class="progress-cell"><span><i :style="{ width: `${Math.round(task.progress * 100)}%` }" /></span><b>{{ Math.round(task.progress * 100) }}%</b></div></td><td>{{ task.attempt }}/{{ task.max_attempts }}</td><td class="message-cell" :title="task.last_error_message">{{ task.last_error_message || '—' }}</td><td>{{ formatDate(task.updated_at) }}</td><td><div class="row-actions"><button class="icon-button small" title="查看详情" @click="showDetail(task)"><Eye :size="15" /></button><button v-if="task.status === 'waiting_input'" class="icon-button small warning" title="提交输入" @click="openInput(task)"><KeyRound :size="15" /></button><button v-if="['queued', 'running', 'waiting_input', 'retry_wait'].includes(task.status)" class="icon-button small danger" title="取消任务" @click="cancelTask(task)"><Square :size="14" /></button><button v-if="['failed', 'cancelled', 'needs_review'].includes(task.status)" class="icon-button small" title="重新入队" @click="retryTask(task)"><RotateCcw :size="15" /></button></div></td></tr>
            <tr v-if="!tasks.length" class="adaptive-empty-row"><td colspan="8"><div class="table-empty"><ClipboardList :size="28" /><strong>当前筛选下没有任务</strong><small>创建任务后，Worker 领取与执行状态会实时显示。</small></div></td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="createOpen" class="dialog-backdrop" @click.self="createOpen = false">
      <form class="dialog panel" @submit.prevent="createTask"><header><div><h2>创建持久化任务</h2><p>载荷、检查点和结果会使用 AES-GCM 加密保存。</p></div><button type="button" class="icon-button" title="关闭创建任务窗口" @click="createOpen = false"><X :size="17" /></button></header><div class="dialog-body"><label class="form-group"><span>任务类型</span><DropdownSelect :model-value="createForm.taskType" :options="createTypeOptions" :visible-rows="5" aria-label="创建任务类型" @update:model-value="setCreateType" /></label><label class="form-group"><span>JSON 载荷</span><textarea v-model="createForm.payload" class="field code-field" rows="9" spellcheck="false" /></label><div class="form-grid"><label class="form-group"><span>优先级</span><input v-model.number="createForm.priority" class="field" type="number" min="-100" max="100" /></label><label class="form-group"><span>最大尝试次数</span><input v-model.number="createForm.maxAttempts" class="field" type="number" min="1" max="20" /></label></div><label class="form-group"><span>幂等键</span><input v-model="createForm.idempotencyKey" class="field" maxlength="160" placeholder="可选，用于避免重复创建" /></label></div><footer><button type="button" class="secondary-button" @click="createOpen = false">取消</button><button class="primary-button" :disabled="busy === 'create'"><LoaderCircle v-if="busy === 'create'" :size="16" class="spin" /><Plus v-else :size="16" />确认创建</button></footer></form>
    </div>

    <div v-if="detailOpen" class="dialog-backdrop" @click.self="detailOpen = false">
      <section class="dialog panel detail-dialog"><header><div><h2>任务详情</h2><p>{{ taskTypeLabel(selected?.task_type) }} · {{ selected?.id }}</p></div><button class="icon-button" title="关闭任务详情" @click="detailOpen = false"><X :size="17" /></button></header><div class="detail-summary"><StatusBadge :status="selected?.status" /><span>进度 {{ Math.round((selected?.progress || 0) * 100) }}%</span><span>尝试 {{ selected?.attempt }}/{{ selected?.max_attempts }}</span><span>更新 {{ formatDate(selected?.updated_at) }}</span></div><div class="event-list"><div v-for="event in events" :key="event.sequence" class="event-row"><span class="event-dot" :class="event.level" /><div><strong>{{ event.message }}</strong><small>{{ event.event_type }} · {{ formatDate(event.created_at) }}</small></div></div><div v-if="!events.length" class="table-empty"><CircleAlert :size="24" /><strong>暂无事件</strong></div></div></section>
    </div>

    <div v-if="inputOpen" class="dialog-backdrop" @click.self="inputOpen = false">
      <form class="dialog panel input-dialog" @submit.prevent="submitInput"><header><div><h2>提交一次性输入</h2><p>输入会加密保存，并在 Worker 消费后立即清除密文。</p></div><button type="button" class="icon-button" title="关闭一次性输入窗口" @click="inputOpen = false"><X :size="17" /></button></header><div class="dialog-body"><label class="form-group"><span>输入类型</span><DropdownSelect v-model="inputForm.inputType" :options="inputTypeOptions" :visible-rows="5" aria-label="一次性输入类型" /></label><label class="form-group"><span>输入值</span><input v-model="inputForm.value" class="field" type="text" autocomplete="one-time-code" required /></label></div><footer><button type="button" class="secondary-button" @click="inputOpen = false">取消</button><button class="primary-button" :disabled="busy === 'input'"><KeyRound :size="16" />加密提交</button></footer></form>
    </div>
  </div>
</template>
