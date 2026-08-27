<script setup>
import {
  BadgeCheck,
  BadgeDollarSign,
  Copy,
  EllipsisVertical,
  Eye,
  KeyRound,
  LoaderCircle,
  LogIn,
  MessageSquareMore,
  RefreshCw,
  Search,
  Smartphone,
  Trash2,
  Unlink,
  WalletCards,
  X,
} from '@lucide/vue'
import { nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import DropdownSelect from '../components/DropdownSelect.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { api, queryString } from '../api/client.js'
import { useAdaptiveTable } from '../composables/useAdaptiveTable.js'
import { subscribeRealtime } from '../composables/useRealtime.js'
import { useToast } from '../composables/useToast.js'
import { formatDate, maskedID, shortID } from '../utils.js'

const loading = ref(true)
const accounts = ref([])
const search = ref('')
const pinStatus = ref('')
const selected = ref(null)
const detailOpen = ref(false)
const menuOpen = ref(false)
const menuRef = ref(null)
const pinOpen = ref(false)
const smsCodeOpen = ref(false)
const confirmOpen = ref(false)
const confirmAction = ref('')
const busyID = ref('')
const menuStyle = reactive({ top: '0px', left: '0px', maxHeight: 'calc(100vh - 16px)' })
const pinForm = reactive({ oldPin: '', newPin: '' })
const smsCode = reactive({ taskId: '', phone: '', provider: '', status: 'queued', code: '', error: '' })
const toast = useToast()
const { viewportRef, viewportStyle, schedule: scheduleTable } = useAdaptiveTable({ initialRows: 5, minRows: 5, fitWholeRows: true })
let unsubscribe = () => {}
let refreshTimer = 0
let smsCodeTimer = 0

const pinStatusOptions = [
  { value: '', label: '全部 PIN 状态' },
  { value: 'configured', label: '已配置' },
  { value: 'unknown', label: '未知' },
]

function smsStatusText(value) {
  return ({
    active: '已激活',
    completed: '已释放',
    unavailable: '不可用',
    missing: '未配置',
    rented: '租用中',
    released: '已释放',
    unknown: '未知',
  })[value] || value || '未知'
}

function smsProviderText(value) {
  return ({ smsbower: 'SMSBower', hero_sms: 'Hero-SMS', manual: '手动号码' })[value] || '短信平台'
}

function phoneSourceText(value) {
  return ({ smsbower: 'SMSBower', hero_sms: 'Hero-SMS', manual: '手动号码' })[value] || value || '手动号码'
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const data = await api(`/api/v1/accounts${queryString({ search: search.value.trim(), pin_status: pinStatus.value, all: true })}`)
    accounts.value = data.items || []
  } catch (error) {
    toast.error(error.message)
  } finally {
    if (!silent) loading.value = false
    await nextTick()
    scheduleTable()
  }
}

function applyFilters() {
  load()
}

function showAccount(account) {
  selected.value = account
  detailOpen.value = true
  menuOpen.value = false
}

async function openMenu(account, event) {
  if (menuOpen.value && selected.value?.id === account.id) {
    menuOpen.value = false
    return
  }

  selected.value = account
  const rect = event.currentTarget.getBoundingClientRect()
  const menuWidth = 204
  const edge = 8
  const gap = 8
  const left = Math.min(window.innerWidth - menuWidth - edge, Math.max(edge, rect.right - menuWidth))
  menuStyle.left = `${Math.round(left)}px`
  menuStyle.top = `${Math.round(rect.bottom + gap)}px`
  menuStyle.maxHeight = `${Math.max(96, window.innerHeight - edge * 2)}px`
  menuOpen.value = true

  await nextTick()
  if (!menuOpen.value || selected.value?.id !== account.id) return
  const menuHeight = menuRef.value?.scrollHeight || 330
  const availableBelow = Math.max(0, window.innerHeight - rect.bottom - gap - edge)
  const availableAbove = Math.max(0, rect.top - gap - edge)
  const openBelow = menuHeight <= availableBelow || availableBelow >= availableAbove
  const availableHeight = Math.max(96, openBelow ? availableBelow : availableAbove)
  const renderedHeight = Math.min(menuHeight, availableHeight)
  const top = openBelow ? rect.bottom + gap : Math.max(edge, rect.top - gap - renderedHeight)
  menuStyle.maxHeight = `${Math.round(availableHeight)}px`
  menuStyle.top = `${Math.round(top)}px`
}

function closeMenu(event) {
  if (!event?.target?.closest?.('.account-more-menu, .account-more-button')) menuOpen.value = false
}

async function createAction(account, action, label) {
  menuOpen.value = false
  busyID.value = account.id
  try {
    const data = await api(`/api/v1/accounts/${account.id}/actions/${action}`, { method: 'POST', body: '{}' })
    toast.success(`${label}任务已创建：${shortID(data.task?.id)}`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busyID.value = ''
  }
}

function closeSmsCode() {
  window.clearTimeout(smsCodeTimer)
  smsCodeOpen.value = false
}

async function pollSmsCode(accountID, taskID) {
  if (!smsCodeOpen.value || smsCode.taskId !== taskID) return
  try {
    const data = await api(`/api/v1/tasks/${taskID}?event_limit=1`)
    if (!smsCodeOpen.value || smsCode.taskId !== taskID) return
    smsCode.status = data.task?.status || 'queued'
    if (smsCode.status === 'succeeded') {
      const result = await api(`/api/v1/accounts/${accountID}/actions/refresh-sms-code/${taskID}/result`, {
        method: 'POST',
        body: '{}',
      })
      if (!smsCodeOpen.value || smsCode.taskId !== taskID) return
      smsCode.code = result.code || ''
      smsCode.error = ''
      toast.success('已获取并显示最新验证码')
      await load(true)
      return
    }
    if (['failed', 'cancelled', 'needs_review'].includes(smsCode.status)) {
      smsCode.error = data.task?.last_error_message || '最新验证码获取失败'
      return
    }
    smsCodeTimer = window.setTimeout(() => pollSmsCode(accountID, taskID), 900)
  } catch (error) {
    if (!smsCodeOpen.value || smsCode.taskId !== taskID) return
    smsCode.error = error.message
  }
}

async function refreshLatestSmsCode(account) {
  menuOpen.value = false
  busyID.value = account.id
  smsCode.taskId = ''
  smsCode.phone = account.phone
  smsCode.provider = account.sms_provider || ''
  smsCode.status = 'queued'
  smsCode.code = ''
  smsCode.error = ''
  smsCodeOpen.value = true
  try {
    const data = await api(`/api/v1/accounts/${account.id}/actions/refresh-sms-code`, {
      method: 'POST',
      body: '{}',
    })
    smsCode.taskId = data.task?.id || ''
    toast.success(`最新验证码任务已创建：${shortID(smsCode.taskId)}`)
    await pollSmsCode(account.id, smsCode.taskId)
  } catch (error) {
    smsCode.error = error.message
  } finally {
    busyID.value = ''
  }
}

async function copyLatestSmsCode() {
  if (!smsCode.code) return
  try {
    await navigator.clipboard.writeText(smsCode.code)
    toast.success('最新验证码已复制')
  } catch {
    toast.warning('请手动复制最新验证码')
  }
}

function openPinDialog(account) {
  selected.value = account
  pinForm.oldPin = ''
  pinForm.newPin = ''
  pinOpen.value = true
  menuOpen.value = false
}

async function changePin() {
  if (pinForm.oldPin && !/^\d{6}$/.test(pinForm.oldPin)) {
    toast.warning('原 PIN 必须是 6 位数字，留空时使用账号已保存 PIN')
    return
  }
  if (!/^\d{6}$/.test(pinForm.newPin) || pinForm.newPin === pinForm.oldPin) {
    toast.warning('新 PIN 必须是不同的 6 位数字')
    return
  }
  busyID.value = selected.value.id
  try {
    const data = await api(`/api/v1/accounts/${selected.value.id}/actions/change-pin`, {
      method: 'POST',
      body: JSON.stringify({ old_pin: pinForm.oldPin || null, new_pin: pinForm.newPin }),
    })
    pinOpen.value = false
    toast.success(`修改 PIN 任务已创建：${shortID(data.task?.id)}`)
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busyID.value = ''
  }
}

async function copyPoolFormat(account) {
  menuOpen.value = false
  busyID.value = account.id
  try {
    const data = await api(`/api/v1/accounts/${account.id}/pool-format`)
    await navigator.clipboard.writeText(data.value)
    toast.success('号码池格式已复制')
  } catch (error) {
    toast.error(error.message || '复制号码池格式失败')
  } finally {
    busyID.value = ''
  }
}

function askConfirm(account, action) {
  selected.value = account
  confirmAction.value = action
  confirmOpen.value = true
  menuOpen.value = false
}

async function runConfirmedAction() {
  const account = selected.value
  busyID.value = account.id
  try {
    if (confirmAction.value === 'release') {
      const data = await api(`/api/v1/accounts/${account.id}/actions/release-number`, { method: 'POST', body: '{}' })
      toast.success(`释放号码任务已创建：${shortID(data.task?.id)}`)
    } else {
      await api(`/api/v1/accounts/${account.id}`, { method: 'DELETE' })
      toast.success(`账号 ${account.phone} 已删除`)
    }
    confirmOpen.value = false
    await load(true)
  } catch (error) {
    toast.error(error.message)
  } finally {
    busyID.value = ''
  }
}

onMounted(() => {
  load()
  document.addEventListener('click', closeMenu)
  window.addEventListener('resize', closeMenu)
  window.addEventListener('scroll', closeMenu, true)
  unsubscribe = subscribeRealtime(['account', 'task'], () => {
    window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => load(true), 100)
  })
})

onBeforeUnmount(() => {
  unsubscribe()
  window.clearTimeout(refreshTimer)
  window.clearTimeout(smsCodeTimer)
  document.removeEventListener('click', closeMenu)
  window.removeEventListener('resize', closeMenu)
  window.removeEventListener('scroll', closeMenu, true)
})

</script>

<template>
  <div class="page accounts-page">
    <section class="panel table-panel account-list-panel">
      <header class="command-bar"><div class="filter-group"><label class="search-control search-wide"><Search :size="16" /><input v-model="search" class="field" placeholder="搜索手机号或远程账号 ID" @keyup.enter="applyFilters" /></label><DropdownSelect v-model="pinStatus" class="filter-dropdown" :options="pinStatusOptions" aria-label="PIN 状态" @change="applyFilters" /></div><div class="command-actions"><button class="icon-button" title="刷新账号" aria-label="刷新账号" @click="load()"><RefreshCw :size="16" /></button></div></header>
      <div v-if="loading" class="table-loading"><LoaderCircle :size="22" class="spin" />正在读取账号</div>
      <div v-else ref="viewportRef" class="table-scroll adaptive-table account-table-scroll" :class="{ 'is-empty': !accounts.length }" :style="viewportStyle">
        <table class="data-table account-table account-all-data-table">
          <thead><tr><th>账号 ID</th><th>号码来源</th><th>手机号</th><th>PIN</th><th>余额</th><th>账号状态</th><th>PIN 状态</th><th>PIN 变更</th><th>PIN 变更详情</th><th>短信状态</th><th>远程账号 ID</th><th>注册时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="account in accounts" :key="account.id">
              <td><code class="account-full-value">{{ maskedID(account.id) }}</code></td>
              <td><span class="mode-cell"><Smartphone :size="14" />{{ phoneSourceText(account.phone_source) }}</span></td>
              <td><strong class="phone-value">{{ account.phone }}</strong></td>
              <td><code>{{ account.pin || '-' }}</code></td>
              <td><b class="balance">{{ Number(account.balance || 0).toLocaleString('id-ID') }} Rp</b></td>
              <td><StatusBadge :status="account.account_status" :title="account.account_status_message" /></td>
              <td><StatusBadge :status="account.pin_setup_status" /></td>
              <td><StatusBadge :status="account.pin_change_status || 'not_run'" /></td>
              <td class="message-cell" :title="account.pin_change_message">{{ account.pin_change_message || '—' }}</td>
              <td><StatusBadge :status="account.sms_activation_status" :title="`${smsProviderText(account.sms_provider)} · ${smsStatusText(account.sms_activation_status)}`" /></td>
              <td><code class="account-full-value">{{ account.remote_account_id || '—' }}</code></td>
              <td>{{ formatDate(account.registered_at) }}</td>
              <td><button class="icon-button small account-more-button" :class="{ active: menuOpen && selected?.id === account.id }" data-tooltip="更多操作" :aria-label="`打开 ${account.phone} 的更多操作`" :aria-expanded="menuOpen && selected?.id === account.id" :disabled="busyID === account.id" @click.stop="openMenu(account, $event)"><LoaderCircle v-if="busyID === account.id" :size="15" class="spin" /><EllipsisVertical v-else :size="17" /></button></td>
            </tr>
            <tr v-if="!accounts.length" class="adaptive-empty-row"><td colspan="13"><div class="table-empty"><Smartphone :size="28" /><strong>暂无 GoPay 账号</strong><small>注册或登录成功后，新账号会显示在这里。</small></div></td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <Teleport to="body">
      <div v-if="menuOpen && selected" ref="menuRef" class="account-more-menu" :style="menuStyle" role="menu" :aria-label="`${selected.phone} 操作菜单`" @click.stop>
        <header><div><strong>{{ selected.phone }}</strong><small>GoPay 账号操作</small></div></header>
        <button type="button" role="menuitem" @click="showAccount(selected)"><Eye :size="15" /><span>查看账号详情</span></button>
        <div class="account-more-separator" />
        <button type="button" role="menuitem" @click="createAction(selected, 'refresh', '查询余额')"><BadgeDollarSign :size="15" /><span>查询余额</span></button>
        <button type="button" role="menuitem" @click="createAction(selected, 'check-pin', '检测 PIN')"><BadgeCheck :size="15" /><span>检测 PIN</span></button>
        <button type="button" role="menuitem" @click="openPinDialog(selected)"><KeyRound :size="15" /><span>修改 PIN</span></button>
        <button type="button" role="menuitem" @click="createAction(selected, 'relogin', '重新登录')"><LogIn :size="15" /><span>重新登录</span></button>
        <div class="account-more-separator" />
        <button type="button" role="menuitem" @click="refreshLatestSmsCode(selected)"><MessageSquareMore :size="15" /><span>重新获取最新验证码</span></button>
        <button type="button" role="menuitem" @click="copyPoolFormat(selected)"><Copy :size="15" /><span>复制号码池格式</span></button>
        <button type="button" role="menuitem" @click="askConfirm(selected, 'release')"><Unlink :size="15" /><span>释放号码</span></button>
        <div class="account-more-separator" />
        <button type="button" role="menuitem" class="danger" @click="askConfirm(selected, 'delete')"><Trash2 :size="15" /><span>删除账号</span></button>
      </div>
    </Teleport>

    <div v-if="detailOpen" class="dialog-backdrop" @click.self="detailOpen = false"><section class="dialog panel account-detail"><header><div><h2>账号详情</h2><p>{{ selected?.phone }}</p></div><button class="icon-button" title="关闭账号详情" @click="detailOpen = false"><X :size="17" /></button></header><div class="account-hero"><span><WalletCards :size="24" /></span><div><small>当前余额</small><strong>{{ Number(selected?.balance || 0).toLocaleString('id-ID') }} Rp</strong></div><StatusBadge :status="selected?.account_status" :title="selected?.account_status_message" /></div><dl class="detail-grid"><div><dt>账号 ID</dt><dd>{{ maskedID(selected?.id) }}</dd></div><div><dt>PIN</dt><dd>{{ selected?.pin || '-' }}</dd></div><div><dt>远程账号 ID</dt><dd>{{ selected?.remote_account_id || '—' }}</dd></div><div><dt>注册时间</dt><dd>{{ selected?.registered_at || '—' }}</dd></div><div><dt>账号状态</dt><dd>{{ selected?.account_status_label || '状态未知' }}</dd></div><div><dt>状态说明</dt><dd>{{ selected?.account_status_message || '—' }}</dd></div><div><dt>PIN 变更</dt><dd><StatusBadge :status="selected?.pin_change_status || 'not_run'" /></dd></div><div><dt>短信激活</dt><dd>{{ smsProviderText(selected?.sms_provider) }} · {{ smsStatusText(selected?.sms_activation_status) }}</dd></div></dl></section></div>

    <div v-if="pinOpen" class="dialog-backdrop" @click.self="pinOpen = false"><form class="dialog panel pin-dialog" @submit.prevent="changePin"><header><div><h2>修改账号 PIN</h2><p>{{ selected?.phone }}</p></div><button type="button" class="icon-button" title="关闭修改 PIN 窗口" @click="pinOpen = false"><X :size="17" /></button></header><div class="dialog-body"><label class="form-group"><span>原 PIN</span><input v-model="pinForm.oldPin" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="留空使用账号已保存 PIN" /></label><label class="form-group"><span>新 PIN</span><input v-model="pinForm.newPin" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="输入新的 6 位 PIN" required /></label></div><footer><button type="button" class="secondary-button" @click="pinOpen = false">取消</button><button class="primary-button" :disabled="busyID === selected?.id"><LoaderCircle v-if="busyID === selected?.id" :size="16" class="spin" /><KeyRound v-else :size="16" />创建修改任务</button></footer></form></div>

    <div v-if="smsCodeOpen" class="dialog-backdrop" @click.self="closeSmsCode"><section class="dialog panel sms-code-dialog"><header><div><h2>重新获取最新验证码</h2><p>{{ smsCode.phone }} · {{ shortID(smsCode.taskId) }}</p></div><button type="button" class="icon-button" title="关闭验证码窗口" @click="closeSmsCode"><X :size="17" /></button></header><div class="dialog-body"><div v-if="smsCode.code" class="latest-sms-code"><small>{{ smsProviderText(smsCode.provider) }} 最新验证码</small><strong>{{ smsCode.code }}</strong><button type="button" class="secondary-button" @click="copyLatestSmsCode"><Copy :size="15" />复制验证码</button></div><div v-else-if="smsCode.error" class="sms-code-error"><MessageSquareMore :size="25" /><strong>获取最新验证码失败</strong><p>{{ smsCode.error }}</p></div><div v-else class="sms-code-waiting"><LoaderCircle :size="25" class="spin" /><strong>正在等待下一条新验证码</strong><StatusBadge :status="smsCode.status" /><p>系统已记录并忽略旧验证码，只会显示本次获取的新验证码。</p></div></div><footer><span>验证码只在本窗口读取一次，关闭前请复制。</span><button type="button" class="secondary-button" @click="closeSmsCode">关闭</button></footer></section></div>

    <div v-if="confirmOpen" class="dialog-backdrop" @click.self="confirmOpen = false"><section class="dialog panel confirm-dialog"><header><div><h2>{{ confirmAction === 'delete' ? '删除 GoPay 账号' : `释放 ${smsProviderText(selected?.sms_provider)} 号码` }}</h2><p>{{ selected?.phone }}</p></div><button class="icon-button" title="关闭确认窗口" @click="confirmOpen = false"><X :size="17" /></button></header><div class="confirm-content"><span><Trash2 v-if="confirmAction === 'delete'" :size="22" /><Unlink v-else :size="22" /></span><p>{{ confirmAction === 'delete' ? '账号及其本地密钥记录将被移除，请确认不再需要这项账号数据。' : '系统会创建后台任务释放当前短信激活记录。' }}</p></div><footer><button class="secondary-button" @click="confirmOpen = false">取消</button><button class="danger-button" :disabled="busyID === selected?.id" @click="runConfirmedAction"><LoaderCircle v-if="busyID === selected?.id" :size="16" class="spin" /><Trash2 v-else-if="confirmAction === 'delete'" :size="16" /><Unlink v-else :size="16" />{{ confirmAction === 'delete' ? '确认删除' : '确认释放' }}</button></footer></section></div>
  </div>
</template>
