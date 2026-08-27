<script setup>
import {
  CircleAlert,
  CircleDollarSign,
  KeyRound,
  Link2,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Send,
  Trash2,
  X,
} from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import DropdownSelect from '../components/DropdownSelect.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { api, queryString } from '../api/client.js'
import { useAdaptiveTable } from '../composables/useAdaptiveTable.js'
import { subscribeRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { formatDate, shortID } from '../utils.js'

const loading = ref(true)
const payments = ref([])
const accounts = ref([])
const total = ref(0)
const statusFilter = ref('')
const inputOpen = ref(false)
const clearOpen = ref(false)
const selectedPayment = ref(null)
const otp = ref('')
const busy = ref('')
const createForm = reactive({ midtransUrl: '', accountId: '', pin: '', proxy: '', confirmed: true })
const toast = useToast()
const { viewportRef, viewportStyle, visibleRows, schedule: scheduleTable } = useAdaptiveTable({ initialRows: 5, minRows: 3 })
let unsubscribe = () => {}
let refreshTimer = 0
let mounted = false

const accountMap = computed(() => new Map(accounts.value.map((account) => [account.id, account])))
const accountOptions = computed(() => [
  {
    value: '',
    label: '自动选择可用账号',
    description: '自动匹配余额充足且已配置 PIN 的 GoPay 账号',
  },
  ...accounts.value.map((account) => ({
    value: account.id,
    label: account.phone,
    description: `${Number(account.balance || 0).toLocaleString('id-ID')} Rp · ${account.pin_setup_status === 'configured' ? 'PIN 已配置' : 'PIN 未配置'}`,
  })),
])
const statusOptions = [
  { value: '', label: '全部支付状态', description: '显示全部支付任务记录' },
  { value: 'queued', label: '排队中', description: '等待 Worker 领取任务' },
  { value: 'running', label: '运行中', description: '支付任务正在后台执行' },
  { value: 'waiting_otp', label: '等待 OTP', description: '等待提交支付验证码' },
  { value: 'retry_wait', label: '等待重试', description: '任务将在稍后自动重新执行' },
  { value: 'succeeded', label: '已成功', description: '支付任务已经完成' },
  { value: 'failed', label: '已失败', description: '支付任务执行失败' },
  { value: 'needs_review', label: '待复核', description: '需要检查远端支付状态' },
]
const validLink = computed(() => /^https:\/\/app\.midtrans\.com\/snap\/v[34]\/redirection\/[0-9a-f-]{36}(?:\/|[?#]|$)/i.test(createForm.midtransUrl.trim()))

function accountLabel(accountID) {
  const account = accountMap.value.get(accountID)
  return account ? `${account.phone} · ${Number(account.balance || 0).toLocaleString('id-ID')} Rp` : shortID(accountID, 12)
}

function amountLabel(payment) {
  const amount = Number(payment.amount || 0)
  if (!amount) return '待获取'
  return `${amount.toLocaleString('id-ID')} ${payment.currency || 'IDR'}`
}

function transactionLabel(status) {
  return ({
    capture: '已扣款',
    settlement: '已结算',
    pending: '处理中',
    deny: '已拒绝',
    cancel: '已取消',
    expire: '已过期',
    refund: '已退款',
    partial_refund: '部分退款',
    unknown: '状态未知',
  })[status] || status || '待核验'
}

function canReconcile(payment) {
  return !['queued', 'running', 'waiting_otp', 'retry_wait'].includes(payment.status)
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [paymentData, accountData] = await Promise.all([
      api(`/api/v1/payments${queryString({ status: statusFilter.value, limit: visibleRows.value })}`),
      api('/api/v1/accounts?limit=200'),
    ])
    payments.value = paymentData.items || []
    total.value = paymentData.total || 0
    accounts.value = accountData.items || []
  } catch (error) {
    toast.error(error.message)
  } finally {
    if (!silent) loading.value = false
    await nextTick()
    scheduleTable()
  }
}

async function createPayment() {
  const midtransUrl = createForm.midtransUrl.trim()
  if (!validLink.value) {
    toast.warning('请输入有效的 Midtrans Snap 支付地址')
    return
  }
  if (createForm.pin && !/^\d{6}$/.test(createForm.pin)) {
    toast.warning('PIN 覆盖值必须是 6 位数字')
    return
  }
  if (!createForm.confirmed) {
    toast.warning('请确认支付信息后再创建任务')
    return
  }
  busy.value = 'create'
  try {
    const data = await api('/api/v1/payments', {
      method: 'POST',
      body: JSON.stringify({
        midtrans_url: midtransUrl,
        account_id: createForm.accountId || null,
        pin: createForm.pin,
        proxy: createForm.proxy.trim() || null,
      }),
    })
    createForm.midtransUrl = ''
    createForm.pin = ''
    createForm.proxy = ''
    toast.success(data.created ? `支付任务已创建：${shortID(data.task?.id)}` : '该支付地址已经存在，已返回原任务')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

function openInput(payment) {
  if (!payment.task_id) {
    toast.warning('当前支付记录尚未关联可恢复任务')
    return
  }
  selectedPayment.value = payment
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
    await api(`/api/v1/tasks/${selectedPayment.value.task_id}/input`, {
      method: 'POST',
      body: JSON.stringify({ input_type: 'otp', value: otp.value, ttl_seconds: 300 }),
    })
    inputOpen.value = false
    toast.success('支付 OTP 已加密提交，任务将从检查点继续')
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function reconcilePayment(payment) {
  busy.value = `reconcile-${payment.id}`
  try {
    const data = await api(`/api/v1/payments/${payment.id}/reconcile`, { method: 'POST', body: '{}' })
    toast.success(`远端复核任务已创建：${shortID(data.task?.id)}`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busy.value = ''
  }
}

async function clearPaymentLogs() {
  busy.value = 'clear'
  try {
    const data = await api('/api/v1/payments', { method: 'DELETE' })
    clearOpen.value = false
    toast.success(`已停止相关任务并清空 ${data.removed || 0} 条支付日志`)
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
  unsubscribe = subscribeRealtime(['payment', 'task'], () => {
    window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => load(true), 120)
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
  <div class="page payments-page">
    <section class="panel payment-workbench">
      <form class="payment-action-bar" @submit.prevent="createPayment">
        <div class="payment-action-fields">
          <label class="form-group payment-url-field"><span>Midtrans Snap 支付地址</span><div class="input-with-leading"><Link2 :size="14" /><input v-model.trim="createForm.midtransUrl" class="field" type="url" autocomplete="off" placeholder="粘贴完整 Snap 支付地址" required /></div></label>
          <label class="form-group payment-account-field"><span>GoPay 支付账号</span><DropdownSelect v-model="createForm.accountId" class="payment-account-select" :options="accountOptions" :visible-rows="5" aria-label="GoPay 支付账号" /></label>
          <label class="form-group payment-pin-field"><span>PIN 覆盖值</span><input v-model="createForm.pin" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="留空" /></label>
          <label class="form-group payment-proxy-field"><span>任务代理覆盖值</span><input v-model="createForm.proxy" class="field" type="text" autocomplete="off" placeholder="留空" /></label>
          <label class="payment-confirm" title="已核对支付地址与账号信息"><input v-model="createForm.confirmed" type="checkbox" /><span>已核对支付地址与账号信息</span></label>
        </div>
        <div class="payment-action-controls">
          <button class="icon-button task-control-button task-start-control payment-create-control" :disabled="busy === 'create'" title="创建并执行支付任务" aria-label="创建并执行支付任务"><LoaderCircle v-if="busy === 'create'" :size="16" class="spin" /><Send v-else :size="17" /></button>
          <span class="handler-state ready"><i />支付处理器已就绪</span>
        </div>
      </form>
    </section>

    <section class="panel table-panel payment-table-panel">
      <header class="command-bar"><div class="filter-group"><DropdownSelect v-model="statusFilter" class="filter-dropdown payment-status-select" :options="statusOptions" :visible-rows="5" aria-label="支付状态" @change="load()" /><span class="result-count">显示 {{ payments.length }} 条，共 {{ total }} 条支付记录</span></div><div class="command-actions"><button class="icon-button" title="刷新支付状态" aria-label="刷新支付状态" @click="load()"><RefreshCw :size="16" /></button><button type="button" class="icon-button danger-hover" title="停止任务并清空全部支付日志" aria-label="停止任务并清空全部支付日志" @click="clearOpen = true"><Trash2 :size="16" /></button></div></header>
      <div v-if="loading" class="table-loading"><LoaderCircle :size="22" class="spin" />正在读取支付状态</div>
      <div v-else ref="viewportRef" class="table-scroll adaptive-table" :class="{ 'is-empty': !payments.length }" :style="viewportStyle">
        <table class="data-table payment-table"><thead><tr><th>支付 ID</th><th>订单号</th><th>支付账号</th><th>金额</th><th>任务状态</th><th>远端状态</th><th>详情</th><th>更新时间</th><th>操作</th></tr></thead><tbody><tr v-for="payment in payments" :key="payment.id"><td><code :title="payment.id">{{ shortID(payment.id, 10) }}</code></td><td><strong :title="payment.order_id">{{ payment.order_id || '待获取' }}</strong></td><td class="message-cell" :title="accountLabel(payment.account_id)">{{ accountLabel(payment.account_id) }}</td><td><b class="balance">{{ amountLabel(payment) }}</b></td><td><StatusBadge :status="payment.status" /></td><td>{{ transactionLabel(payment.transaction_status) }}</td><td class="message-cell" :title="payment.last_error_message">{{ payment.last_error_message || '状态正常' }}</td><td>{{ formatDate(payment.updated_at) }}</td><td><div class="row-actions"><button v-if="payment.status === 'waiting_otp'" class="icon-button small warning" title="提交支付 OTP" @click="openInput(payment)"><KeyRound :size="15" /></button><button v-if="canReconcile(payment)" class="icon-button small" title="读取远端状态进行复核" :disabled="busy === `reconcile-${payment.id}`" @click="reconcilePayment(payment)"><LoaderCircle v-if="busy === `reconcile-${payment.id}`" :size="15" class="spin" /><RotateCcw v-else :size="15" /></button></div></td></tr><tr v-if="!payments.length" class="adaptive-empty-row"><td colspan="9"><div class="table-empty"><CircleDollarSign :size="28" /><strong>暂无支付记录</strong><small>在上方录入 Midtrans Snap 支付地址即可创建任务。</small></div></td></tr></tbody></table>
      </div>
    </section>

    <div v-if="inputOpen" class="dialog-backdrop" @click.self="inputOpen = false"><form class="dialog panel input-dialog" @submit.prevent="submitInput"><header><div><h2>提交支付 OTP</h2><p>支付任务 {{ shortID(selectedPayment?.task_id) }} 会从加密检查点继续。</p></div><button type="button" class="icon-button" title="关闭支付 OTP 窗口" @click="inputOpen = false"><X :size="17" /></button></header><div class="dialog-body"><label class="form-group"><span>OTP 验证码</span><input v-model.trim="otp" class="field" type="text" inputmode="numeric" autocomplete="one-time-code" minlength="4" maxlength="8" autofocus required /></label></div><footer><button type="button" class="secondary-button" @click="inputOpen = false">取消</button><button class="primary-button" :disabled="busy === 'input'"><LoaderCircle v-if="busy === 'input'" :size="16" class="spin" /><KeyRound v-else :size="16" />加密提交</button></footer></form></div>

    <div v-if="clearOpen" class="dialog-backdrop" @click.self="clearOpen = false"><section class="dialog panel confirm-dialog"><header><div><h2>清空全部支付日志</h2><p>进行中的支付与复核任务也会停止并删除。</p></div><button type="button" class="icon-button" title="关闭清理确认" @click="clearOpen = false"><X :size="17" /></button></header><div class="confirm-content"><span><CircleAlert :size="22" /></span><p>将清空全部支付记录、关联任务及阶段事件，操作后支付日志列表不保留任何记录。</p></div><footer><button type="button" class="secondary-button" @click="clearOpen = false">取消</button><button type="button" class="danger-button" :disabled="busy === 'clear'" @click="clearPaymentLogs"><LoaderCircle v-if="busy === 'clear'" :size="16" class="spin" /><Trash2 v-else :size="16" />停止并全部清空</button></footer></section></div>
  </div>
</template>
