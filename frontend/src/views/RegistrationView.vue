<script setup>
import {
  CircleAlert,
  CircleStop,
  Clock3,
  FileClock,
  KeyRound,
  LoaderCircle,
  LogIn,
  MessageSquareText,
  Play,
  RefreshCw,
  ShieldPlus,
  Smartphone,
  Trash2,
  X,
} from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import DropdownSelect from '../components/DropdownSelect.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { api } from '../api/client.js'
import { useAdaptiveTable } from '../composables/useAdaptiveTable.js'
import { subscribeRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { formatDate, maskedID } from '../utils.js'

const loading = ref(true)
const busy = ref('')
const tasks = ref([])
const relatedTotal = ref(0)
const activeCounts = reactive({ register: 0, login: 0 })
const supportedTypes = ref(new Set())
const phoneSources = ref([])
const proxyProfiles = ref([])
const inputOpen = ref(false)
const detailOpen = ref(false)
const clearOpen = ref(false)
const selectedTask = ref(null)
const taskEvents = ref([])
const currentRun = ref(null)
const otp = ref('')
const toast = useToast()
const { viewportRef, viewportStyle, schedule: scheduleTable } = useAdaptiveTable({ initialRows: 5, minRows: 5, fitWholeRows: true })
const form = reactive({
  mode: 'register',
  phoneSource: 'smsbower',
  pin: '',
  newPin: '',
  count: 1,
  concurrency: 2,
  changePin: true,
  proxyRegion: '',
})
const defaults = reactive({ registerPin: '', loginPin: '', newPin: '', changePin: true })
let unsubscribe = () => {}
let refreshTimer = 0

const modeOptions = [
  { value: 'register', label: '注册新账号', description: '使用已选短信平台获取全新号码' },
  { value: 'login', label: '登录已有账号', description: '使用短信平台新号码检测并登录已有 GoPay 账号' },
]
const activeStatuses = new Set(['queued', 'running', 'retry_wait', 'waiting_input'])

const taskType = computed(() => form.mode === 'register' ? 'account.register' : 'account.login')
const handlerReady = computed(() => supportedTypes.value.has(taskType.value))
const sourceOptions = computed(() => phoneSources.value
  .filter((item) => (item.modes || []).includes(form.mode))
  .map((item) => ({
    value: item.value,
    label: item.label,
    description: item.available ? item.description : `${item.description} · 尚未配置`,
    disabled: !item.available,
  })))
const sourceReady = computed(() => sourceOptions.value.some(
  (item) => item.value === form.phoneSource && !item.disabled,
))
const proxyOptions = computed(() => proxyProfiles.value.length
  ? proxyProfiles.value.map((item) => ({
      value: item.region,
      label: `${item.region} · ${item.label}`,
      description: `${item.count} 条代理 · ${item.masked}`,
    }))
  : [{ value: '', label: '未配置代理池', description: '请先在系统设置中添加代理', disabled: true }])
const summary = computed(() => {
  const total = Number(currentRun.value?.target ?? relatedTotal.value)
  const succeeded = Number(currentRun.value?.succeeded ?? tasks.value.filter((item) => item.status === 'succeeded').length)
  const failed = Number(currentRun.value?.failed ?? tasks.value.filter((item) => ['failed', 'cancelled', 'needs_review'].includes(item.status)).length)
  return {
    total,
    remaining: Number(currentRun.value?.remaining ?? Math.max(0, total - succeeded - failed)),
    running: Number(currentRun.value?.active ?? (activeCounts.register + activeCounts.login)),
    succeeded,
    failed,
  }
})

function applyModeDefaults() {
  form.pin = form.mode === 'register' ? defaults.registerPin : defaults.loginPin
  form.newPin = defaults.newPin
  form.changePin = form.mode === 'login' ? defaults.changePin : false
  const available = sourceOptions.value.find((item) => !item.disabled)
  if (!sourceOptions.value.some((item) => item.value === form.phoneSource && !item.disabled)) {
    form.phoneSource = available?.value || 'smsbower'
  }
}

function completionText(task) {
  if (task.finished_at) return formatDate(task.finished_at)
  if (['failed', 'cancelled', 'needs_review', 'succeeded'].includes(task.status)) return formatDate(task.updated_at)
  return '尚未完成'
}

function phoneSourceText(value) {
  return ({
    smsbower: 'SMSBower',
    hero_sms: 'Hero-SMS',
    manual: '手动号码',
    accounts: '账号库',
  })[value] || value || '手动号码'
}

function detailText(task) {
  if (task.last_error_message) return task.last_error_message
  if (task.latest_event_message) return task.latest_event_message
  return ({
    queued: '任务正在等待 Worker 领取',
    running: '任务正在执行',
    waiting_input: '正在等待手动提交 OTP',
    retry_wait: '任务将在稍后自动重试',
    succeeded: '任务已完成',
    cancelled: '任务已取消',
    needs_review: '任务需要人工复核',
    failed: '任务执行失败',
  })[task.status] || '任务状态正常'
}

function isActiveTask(task) {
  return activeStatuses.has(task.status)
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [typeData, logData, defaultData, sourceData] = await Promise.all([
      api('/api/v1/tasks/types'),
      api('/api/v1/account-flows/logs?all=true'),
      api('/api/v1/settings/account-flow'),
      api('/api/v1/account-flows/sources'),
    ])
    supportedTypes.value = new Set(typeData.map((item) => item.task_type))
    tasks.value = logData.items || []
    relatedTotal.value = Number(logData.total || 0)
    activeCounts.register = Number(logData.active?.register || 0)
    activeCounts.login = Number(logData.active?.login || 0)
    currentRun.value = logData.runs?.current || null
    phoneSources.value = sourceData || []
    defaults.registerPin = defaultData.register_pin || ''
    defaults.loginPin = defaultData.login_pin || ''
    defaults.newPin = defaultData.new_pin || ''
    defaults.changePin = Boolean(defaultData.change_pin_enabled)
    proxyProfiles.value = defaultData.proxy_profiles || []
    if (!silent) {
      if (currentRun.value?.mode) form.mode = currentRun.value.mode
      form.count = Number(defaultData.task_count || 1)
      form.concurrency = Number(defaultData.concurrency || 2)
      form.proxyRegion = defaultData.default_proxy_region || proxyProfiles.value[0]?.region || ''
      applyModeDefaults()
    }
  } catch (error) {
    toast.error(error.message)
  } finally {
    if (!silent) loading.value = false
    await nextTick()
    scheduleTable()
  }
}

async function submit() {
  if (!handlerReady.value) {
    toast.info('账号任务处理器尚未就绪，请检查后端启动状态')
    return
  }
  if (!sourceReady.value) {
    toast.warning('当前号码来源尚未配置，请先到系统设置完成配置')
    return
  }
  if (!/^\d{6}$/.test(form.pin)) {
    toast.warning(`${form.mode === 'register' ? '设置 PIN' : '原 PIN'}必须是 6 位数字`)
    return
  }
  if (form.mode === 'login' && form.changePin && (!/^\d{6}$/.test(form.newPin) || form.newPin === form.pin)) {
    toast.warning('新 PIN 必须是不同的 6 位数字')
    return
  }
  if (!/^\d{1,4}$/.test(String(form.count)) || Number(form.count) < 1 || Number(form.count) > 1000) {
    toast.warning('任务数量必须是 1 到 1000 的整数')
    return
  }
  if (!/^\d{1,2}$/.test(String(form.concurrency)) || Number(form.concurrency) < 1 || Number(form.concurrency) > 50) {
    toast.warning('期望并发必须是 1 到 50 的整数')
    return
  }
  busy.value = 'submit'
  try {
    const data = await api('/api/v1/account-flows', {
      method: 'POST',
      body: JSON.stringify({
        mode: form.mode,
        phone_source: form.phoneSource,
        phone: '',
        pin: form.pin,
        change_pin: form.mode === 'login' && form.changePin,
        new_pin: form.mode === 'login' && form.changePin ? form.newPin : null,
        proxy_region: form.proxyRegion,
        count: Number(form.count),
        concurrency: Number(form.concurrency),
      }),
    })
    const created = Number(data.batch?.created || data.tasks?.length || 0)
    const appended = Number(data.batch?.appended || 0)
    const modeLabel = form.mode === 'register' ? '注册' : '登录'
    toast.success(appended
      ? `已向当前${modeLabel}批次追加 ${appended} 条任务，当前补充 ${created} 条，目标 ${data.batch?.total} 条`
      : `已开启${modeLabel}任务：当前运行 ${created} 条，目标 ${data.batch?.total || form.count} 条`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function stopRun() {
  const run = currentRun.value
  if (!run?.id) return
  busy.value = 'stop-run'
  try {
    const data = await api(`/api/v1/account-flows/runs/${run.id}/stop`, { method: 'POST', body: '{}' })
    toast.success(`已停止${run.mode === 'register' ? '注册' : '登录'}任务，停止 ${data.stopped_tasks || 0} 条进行中任务`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

function openInput(task) {
  selectedTask.value = task
  otp.value = ''
  inputOpen.value = true
}

async function submitInput() {
  if (!/^\d{4,8}$/.test(otp.value)) {
    toast.warning('OTP 必须是 4 到 8 位数字')
    return
  }
  busy.value = 'input'
  try {
    await api(`/api/v1/tasks/${selectedTask.value.id}/input`, {
      method: 'POST',
      body: JSON.stringify({ input_type: 'otp', value: otp.value, ttl_seconds: 300 }),
    })
    inputOpen.value = false
    toast.success('OTP 已加密提交，任务将从检查点继续')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function stopTask(task) {
  busy.value = `stop-${task.id}`
  try {
    await api(`/api/v1/tasks/${task.id}/cancel`, { method: 'POST', body: '{}' })
    toast.success(`任务 ${maskedID(task.id)} 已停止`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function showLog(task, silent = false) {
  if (!silent) {
    selectedTask.value = task
    taskEvents.value = []
    detailOpen.value = true
    busy.value = `detail-${task.id}`
  }
  try {
    const data = await api(`/api/v1/tasks/${task.id}?event_limit=200`)
    selectedTask.value = { ...task, ...data.task }
    taskEvents.value = data.events || []
  } catch (error) {
    if (!silent) toast.error(error.message)
  } finally {
    if (!silent) busy.value = ''
  }
}

async function clearLogs() {
  busy.value = 'clear'
  try {
    const data = await api('/api/v1/account-flows/logs', { method: 'DELETE' })
    clearOpen.value = false
    toast.success(`已停止并清空 ${data.removed || 0} 条注册与登录日志`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  load()
  unsubscribe = subscribeRealtime('task', () => {
    window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(async () => {
      await load(true)
      if (detailOpen.value && selectedTask.value?.id) {
        const refreshed = tasks.value.find((item) => item.id === selectedTask.value.id)
        await showLog(refreshed || selectedTask.value, true)
      }
    }, 100)
  })
})

onBeforeUnmount(() => {
  unsubscribe()
  window.clearTimeout(refreshTimer)
})

watch(() => form.mode, applyModeDefaults)
</script>

<template>
  <div v-if="loading" class="page-loading"><LoaderCircle :size="24" class="spin" />正在加载注册与登录工作台</div>
  <div v-else class="page operation-page registration-page">
    <section class="panel operation-workbench registration-log-panel">
      <form class="operation-command-bar registration-command-bar" @submit.prevent="submit">
        <div class="operation-controls registration-controls">
          <label class="command-control"><span>执行模式</span><DropdownSelect v-model="form.mode" :options="modeOptions" aria-label="执行模式" /></label>
          <label class="command-control command-source"><span>号码来源</span><DropdownSelect v-model="form.phoneSource" :options="sourceOptions" aria-label="号码来源" /></label>
          <label class="command-control command-pin"><span>{{ form.mode === 'login' ? '原 PIN' : '设置 PIN' }}</span><input v-model="form.pin" class="field" name="pin" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="6 位 PIN" /></label>
          <label class="command-control command-pin"><span>新 PIN</span><input v-model="form.newPin" class="field" name="new-pin" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="新的 6 位 PIN" :disabled="form.mode !== 'login' || !form.changePin" /></label>
          <label class="command-control command-count"><span>任务数量</span><input v-model="form.count" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="4" pattern="[0-9]*" /></label>
          <label class="command-control command-count"><span>期望并发</span><input v-model="form.concurrency" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="2" pattern="[0-9]*" /></label>
          <label class="command-toggle pin-toggle" :class="{ disabled: form.mode !== 'login' }"><input v-model="form.changePin" type="checkbox" :disabled="form.mode !== 'login'" /><span>登录后修改 PIN</span></label>
          <label class="command-control command-proxy"><span>任务代理</span><DropdownSelect v-model="form.proxyRegion" :options="proxyOptions" :visible-rows="5" :disabled="!proxyProfiles.length" aria-label="任务代理区域" /></label>
        </div>
        <div class="operation-actions">
          <span class="handler-state" :class="handlerReady ? 'ready' : 'pending'"><i />{{ handlerReady ? '任务处理器已就绪' : '任务处理器尚未就绪' }}</span>
          <button type="button" class="icon-button" title="刷新任务日志" aria-label="刷新任务日志" @click="load(true)"><RefreshCw :size="16" /></button>
          <button type="button" class="icon-button danger-hover" title="停止并清空全部注册与登录日志" aria-label="停止并清空全部注册与登录日志" @click="clearOpen = true"><Trash2 :size="16" /></button>
          <button type="submit" class="icon-button task-control-button task-start-control" :disabled="Boolean(busy) || !handlerReady || !sourceReady" :title="currentRun ? `继续添加${currentRun.mode === 'register' ? '注册' : '登录'}任务` : (form.mode === 'register' ? '开始注册任务' : '开始登录任务')" :aria-label="currentRun ? '继续添加注册或登录任务' : (form.mode === 'register' ? '开始注册任务' : '开始登录任务')"><LoaderCircle v-if="busy === 'submit'" :size="16" class="spin" /><Play v-else :size="17" /></button>
          <button type="button" class="icon-button task-control-button task-stop-control" :disabled="!currentRun || Boolean(busy)" :title="currentRun ? `停止当前${currentRun.mode === 'register' ? '注册' : '登录'}任务` : '当前没有可停止的任务'" aria-label="停止当前注册或登录任务" @click="stopRun"><LoaderCircle v-if="busy === 'stop-run'" :size="16" class="spin" /><CircleStop v-else :size="17" /></button>
        </div>
      </form>

      <div class="operation-summary">
        <div class="summary-title"><span><Clock3 :size="17" /></span><div><strong>运行概览</strong><small>持久化任务实时状态</small></div></div>
        <dl><dt>当前模式</dt><dd>{{ (currentRun?.mode || form.mode) === 'register' ? '注册新账号' : '登录已有账号' }}</dd></dl>
        <dl><dt>任务总数</dt><dd>{{ summary.total }}</dd></dl>
        <dl><dt>当前剩余任务</dt><dd class="value-blue">{{ summary.remaining }}</dd></dl>
        <dl><dt>进行中</dt><dd class="value-blue">{{ summary.running }}</dd></dl>
        <dl><dt>执行成功</dt><dd class="value-green">{{ summary.succeeded }}</dd></dl>
        <dl><dt>执行失败</dt><dd class="value-red">{{ summary.failed }}</dd></dl>
        <dl><dt>期望并发</dt><dd>{{ currentRun?.desired_concurrency || form.concurrency }}</dd></dl>
      </div>

      <div class="log-heading"><div><h2>注册与登录日志</h2><p>全部日志实时显示，记录较多时可在表格内滚动查看</p></div></div>
      <div ref="viewportRef" class="table-scroll operation-table-scroll adaptive-table registration-table-scroll" :class="{ 'is-empty': !tasks.length }" :style="viewportStyle">
        <table class="data-table operation-table registration-log-table">
          <thead><tr><th>ID</th><th>号码来源</th><th>手机号</th><th>模式</th><th>状态</th><th>进度</th><th>详情</th><th>完成/失败时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="task in tasks" :key="task.id">
              <td><code>{{ maskedID(task.id) }}</code></td>
              <td><span class="mode-cell"><Smartphone :size="14" />{{ phoneSourceText(task.phone_source) }}</span></td>
              <td><strong class="phone-value">{{ task.phone || '等待取号' }}</strong></td>
              <td><span class="mode-cell"><ShieldPlus v-if="task.task_type === 'account.register'" :size="15" /><LogIn v-else :size="15" />{{ task.task_type === 'account.register' ? '注册' : '登录' }}</span></td>
              <td><StatusBadge :status="task.status" /></td>
              <td><span class="progress-value">{{ Math.round(task.progress * 100) }}%</span></td>
              <td class="message-cell" :title="detailText(task)">{{ detailText(task) }}</td>
              <td>{{ completionText(task) }}</td>
              <td><div class="row-actions"><button v-if="isActiveTask(task)" class="icon-button small danger-hover" :disabled="busy === `stop-${task.id}`" :title="`停止任务 ${maskedID(task.id)}`" :aria-label="`停止任务 ${maskedID(task.id)}`" @click="stopTask(task)"><LoaderCircle v-if="busy === `stop-${task.id}`" :size="15" class="spin" /><CircleStop v-else :size="15" /></button><button v-if="task.status === 'waiting_input'" class="icon-button small warning" title="提交 OTP" @click="openInput(task)"><KeyRound :size="15" /></button><button class="icon-button small" :title="`查看 ${maskedID(task.id)} 的日志详情`" @click="showLog(task)"><LoaderCircle v-if="busy === `detail-${task.id}`" :size="15" class="spin" /><MessageSquareText v-else :size="15" /></button></div></td>
            </tr>
            <tr v-if="!tasks.length" class="adaptive-empty-row"><td colspan="9"><div class="table-empty"><Smartphone :size="25" /><strong>暂无注册或登录日志</strong><small>创建任务后，手机号、状态和结果会实时显示在这里。</small></div></td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="detailOpen" class="dialog-backdrop" @click.self="detailOpen = false">
      <section class="dialog panel task-log-dialog">
        <header><div><h2>任务日志详情</h2><p>{{ selectedTask?.phone || '等待取号' }} · {{ maskedID(selectedTask?.id) }}</p></div><button type="button" class="icon-button" title="关闭日志详情" @click="detailOpen = false"><X :size="17" /></button></header>
        <div class="task-detail-summary"><div><span>ID</span><code>{{ maskedID(selectedTask?.id) }}</code></div><div><span>模式</span><strong>{{ selectedTask?.task_type === 'account.register' ? '注册' : '登录' }}</strong></div><div><span>状态</span><StatusBadge :status="selectedTask?.status" /></div><div><span>完成时间</span><strong>{{ selectedTask ? completionText(selectedTask) : '—' }}</strong></div></div>
        <div class="event-list"><div v-if="busy.startsWith('detail-')" class="dialog-loading"><LoaderCircle :size="20" class="spin" />正在读取日志详情</div><article v-for="event in taskEvents" :key="event.sequence"><span class="event-sequence">{{ event.sequence }}</span><div><header><strong>{{ event.message || '任务状态已更新' }}</strong><time>{{ formatDate(event.created_at) }}</time></header><p>{{ event.event_type }} · {{ event.level === 'error' ? '错误' : event.level === 'warning' ? '警告' : '信息' }}</p></div></article><div v-if="!busy.startsWith('detail-') && !taskEvents.length" class="empty-events"><FileClock :size="24" /><span>这项任务还没有阶段日志</span></div></div>
      </section>
    </div>

    <div v-if="inputOpen" class="dialog-backdrop" @click.self="inputOpen = false">
      <form class="dialog panel input-dialog" @submit.prevent="submitInput"><header><div><h2>提交 GoPay OTP</h2><p>任务 {{ maskedID(selectedTask?.id) }} 会从加密检查点恢复。</p></div><button type="button" class="icon-button" title="关闭 OTP 窗口" @click="inputOpen = false"><X :size="17" /></button></header><div class="dialog-body"><label class="form-group"><span>OTP 验证码</span><input v-model.trim="otp" class="field" type="text" inputmode="numeric" autocomplete="one-time-code" minlength="4" maxlength="8" autofocus required /></label></div><footer><button type="button" class="secondary-button" @click="inputOpen = false">取消</button><button class="primary-button" :disabled="busy === 'input'"><LoaderCircle v-if="busy === 'input'" :size="16" class="spin" /><KeyRound v-else :size="16" />加密提交</button></footer></form>
    </div>

    <div v-if="clearOpen" class="dialog-backdrop" @click.self="clearOpen = false">
      <section class="dialog panel confirm-dialog"><header><div><h2>清空全部注册与登录</h2><p>进行中的任务也会立即停止并删除。</p></div><button type="button" class="icon-button" title="关闭清理确认" @click="clearOpen = false"><X :size="17" /></button></header><div class="confirm-content"><span><CircleAlert :size="22" /></span><p>将清空全部注册与登录任务、阶段事件和所属空批次，操作后列表不保留任何日志。</p></div><footer><button type="button" class="secondary-button" @click="clearOpen = false">取消</button><button type="button" class="danger-button" :disabled="busy === 'clear'" @click="clearLogs"><LoaderCircle v-if="busy === 'clear'" :size="16" class="spin" /><Trash2 v-else :size="16" />停止并全部清空</button></footer></section>
    </div>
  </div>
</template>
