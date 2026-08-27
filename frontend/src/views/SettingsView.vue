<script setup>
import {
  CheckCircle2,
  CircleDollarSign,
  Copy,
  Database,
  KeyRound,
  LoaderCircle,
  Plus,
  Radio,
  RefreshCw,
  Save,
  Server,
  Settings,
  ShieldCheck,
  Smartphone,
  Trash2,
  Workflow,
  X,
} from '@lucide/vue'
import { computed, onMounted, reactive, ref } from 'vue'
import DropdownSelect from '../components/DropdownSelect.vue'
import { api } from '../api/client.js'
import { useToast } from '../composables/useToast.js'
import { formatBytes, taskTypeLabel } from '../utils.js'

const loading = ref(true)
const status = ref(null)
const types = ref([])
const savingSms = ref(false)
const savingHeroSms = ref(false)
const testingHeroSms = ref(false)
const savingAccount = ref(false)
const savingProxy = ref(false)
const proxyOpen = ref(false)
const proxyDraft = ref('')
const proxyTest = ref(null)
const sms = reactive({ apiKey: '', configured: false, masked: '', baseUrl: 'https://smsbower.page', service: 'ni', country: '6' })
const heroSms = reactive({ apiKey: '', configured: false, masked: '', baseUrl: 'https://hero-sms.com/stubs/handler_api.php', service: 'ni', country: '6', balance: '' })
const account = reactive({
  registerPin: '',
  loginPin: '',
  newPin: '',
  taskCount: 1,
  concurrency: 2,
  smsOtpTimeoutSeconds: 60,
  manualOtpTimeoutSeconds: 300,
  changePinEnabled: true,
  defaultProxyRegion: '',
  proxyCount: 0,
  profiles: [],
})
const toast = useToast()
const proxyRegionOptions = computed(() => [
  { value: '', label: '不使用代理', description: '任务直接连接 GoPay' },
  ...account.profiles.map((item) => ({
    value: item.region,
    label: item.label,
    description: `${item.region} · ${item.count} 条代理 · ${item.masked}`,
  })),
])

function applyAccountDefaults(data) {
  account.registerPin = data.register_pin || ''
  account.loginPin = data.login_pin || ''
  account.newPin = data.new_pin || ''
  account.taskCount = Number(data.task_count || 1)
  account.concurrency = Number(data.concurrency || 2)
  account.smsOtpTimeoutSeconds = Number(data.sms_otp_timeout_seconds || 60)
  account.manualOtpTimeoutSeconds = Number(data.manual_otp_timeout_seconds || 300)
  account.changePinEnabled = Boolean(data.change_pin_enabled)
  account.defaultProxyRegion = data.default_proxy_region || ''
  account.proxyCount = Number(data.proxy_count || 0)
  account.profiles = data.proxy_profiles || []
}

async function load() {
  loading.value = true
  try {
    const [systemData, typeData, smsData, heroSmsData, accountData] = await Promise.all([
      api('/api/v1/system/status'),
      api('/api/v1/tasks/types'),
      api('/api/v1/settings/smsbower'),
      api('/api/v1/settings/hero-sms'),
      api('/api/v1/settings/account-flow'),
    ])
    status.value = systemData
    types.value = typeData
    sms.configured = Boolean(smsData.api_key_configured)
    sms.masked = smsData.api_key_masked || ''
    sms.baseUrl = smsData.base_url
    sms.service = smsData.service
    sms.country = smsData.country
    heroSms.configured = Boolean(heroSmsData.api_key_configured)
    heroSms.masked = heroSmsData.api_key_masked || ''
    heroSms.baseUrl = heroSmsData.base_url
    heroSms.service = heroSmsData.service
    heroSms.country = heroSmsData.country
    applyAccountDefaults(accountData)
  } catch (error) {
    toast.error(error.message)
  } finally {
    loading.value = false
  }
}

async function saveAccountDefaults() {
  for (const [label, value] of [['注册 PIN', account.registerPin], ['登录原 PIN', account.loginPin], ['登录新 PIN', account.newPin]]) {
    if (value && !/^\d{6}$/.test(value)) {
      toast.warning(`${label}必须是 6 位数字`)
      return
    }
  }
  if (account.changePinEnabled && account.loginPin && account.newPin === account.loginPin) {
    toast.warning('登录新 PIN 需要与原 PIN 不同')
    return
  }
  const smsOtpTimeout = Number(account.smsOtpTimeoutSeconds)
  const manualOtpTimeout = Number(account.manualOtpTimeoutSeconds)
  if (!Number.isInteger(smsOtpTimeout) || smsOtpTimeout < 30 || smsOtpTimeout > 60) {
    toast.warning('自动取码超时需要设置为 30 到 60 秒')
    return
  }
  if (!Number.isInteger(manualOtpTimeout) || manualOtpTimeout < 60 || manualOtpTimeout > 1800) {
    toast.warning('手动 OTP 等待超时需要设置为 60 到 1800 秒')
    return
  }
  savingAccount.value = true
  try {
    const data = await api('/api/v1/settings/account-flow', {
      method: 'PUT',
      body: JSON.stringify(accountDefaultsPayload()),
    })
    applyAccountDefaults(data)
    toast.success('注册、登录与区域代理默认配置已保存')
  } catch (error) {
    toast.error(error.message)
  } finally {
    savingAccount.value = false
  }
}

function accountDefaultsPayload(overrides = {}) {
  return {
    register_pin: account.registerPin || null,
    login_pin: account.loginPin || null,
    new_pin: account.newPin || null,
    task_count: Number(account.taskCount),
    concurrency: Number(account.concurrency),
    sms_otp_timeout_seconds: Number(account.smsOtpTimeoutSeconds),
    manual_otp_timeout_seconds: Number(account.manualOtpTimeoutSeconds),
    change_pin_enabled: account.changePinEnabled,
    default_proxy_region: account.defaultProxyRegion,
    proxy_pool: null,
    clear_proxy_pool: false,
    ...overrides,
  }
}

function openProxyDialog() {
  proxyDraft.value = ''
  proxyTest.value = null
  proxyOpen.value = true
}

async function addProxyPool() {
  if (!proxyDraft.value.trim()) {
    toast.warning('请粘贴至少一条代理')
    return
  }
  savingProxy.value = true
  proxyTest.value = null
  try {
    const data = await api('/api/v1/settings/account-flow/proxies/test-and-add', {
      method: 'POST',
      body: JSON.stringify({ proxy_pool: proxyDraft.value }),
    })
    applyAccountDefaults(data)
    proxyTest.value = data.proxy_test || null
    const passed = Number(proxyTest.value?.passed || 0)
    const failed = Number(proxyTest.value?.failed || 0)
    if (passed && !failed) {
      proxyOpen.value = false
      toast.success(`${passed} 条代理测试通过并已添加，当前共 ${data.proxy_count || 0} 条`)
    } else if (passed) {
      toast.warning(`${passed} 条已添加，${failed} 条测试失败`)
    } else {
      toast.error(`未添加代理，${failed} 条均未通过连通性测试`)
    }
  } catch (error) {
    toast.error(error.message)
  } finally {
    savingProxy.value = false
  }
}

async function clearProxyPool() {
  if (!window.confirm(`确认清空当前 ${account.proxyCount} 条代理？`)) return
  savingAccount.value = true
  try {
    const data = await api('/api/v1/settings/account-flow', {
      method: 'PUT',
      body: JSON.stringify(accountDefaultsPayload({ default_proxy_region: '', clear_proxy_pool: true })),
    })
    applyAccountDefaults(data)
    toast.success('区域代理池已清空')
  } catch (error) {
    toast.error(error.message)
  } finally {
    savingAccount.value = false
  }
}

async function saveSms() {
  savingSms.value = true
  try {
    const data = await api('/api/v1/settings/smsbower', {
      method: 'PUT',
      body: JSON.stringify({
        api_key: sms.apiKey.trim() || null,
        base_url: sms.baseUrl.trim(),
        service: sms.service.trim(),
        country: sms.country.trim(),
      }),
    })
    sms.apiKey = ''
    sms.configured = Boolean(data.api_key_configured)
    sms.masked = data.api_key_masked || ''
    toast.success('SMSBower 配置已加密保存')
  } catch (error) {
    toast.error(error.message)
  } finally {
    savingSms.value = false
  }
}

async function saveHeroSms() {
  savingHeroSms.value = true
  try {
    const data = await api('/api/v1/settings/hero-sms', {
      method: 'PUT',
      body: JSON.stringify({
        api_key: heroSms.apiKey.trim() || null,
        base_url: heroSms.baseUrl.trim(),
        service: heroSms.service.trim(),
        country: heroSms.country.trim(),
      }),
    })
    heroSms.apiKey = ''
    heroSms.configured = Boolean(data.api_key_configured)
    heroSms.masked = data.api_key_masked || ''
    toast.success('Hero-SMS 配置已加密保存')
  } catch (error) {
    toast.error(error.message)
  } finally {
    savingHeroSms.value = false
  }
}

async function testHeroSms() {
  testingHeroSms.value = true
  heroSms.balance = ''
  try {
    const data = await api('/api/v1/settings/hero-sms/test', {
      method: 'POST',
      body: JSON.stringify({
        api_key: heroSms.apiKey.trim() || null,
        base_url: heroSms.baseUrl.trim(),
        service: heroSms.service.trim(),
        country: heroSms.country.trim(),
      }),
    })
    heroSms.balance = data.balance || '0'
    toast.success(`Hero-SMS 连接正常，余额 ${heroSms.balance}`)
  } catch (error) {
    toast.error(error.message)
  } finally {
    testingHeroSms.value = false
  }
}

async function copyCommand() {
  try {
    await navigator.clipboard.writeText('.venv/bin/python main.py')
    toast.success('启动命令已复制')
  } catch {
    toast.warning('请手动复制启动命令')
  }
}

onMounted(load)
</script>

<template>
  <div v-if="loading" class="page-loading"><LoaderCircle :size="24" class="spin" />正在读取系统设置</div>
  <div v-else class="page settings-page">
    <section class="panel account-defaults-panel">
      <header class="panel-heading settings-main-heading"><div><span class="settings-heading-icon"><KeyRound :size="19" /></span><div><h2>注册与登录默认配置</h2><p>注册与登录页面会自动读取这些值，PIN 和代理使用 AES-GCM 加密保存。</p></div></div><button class="primary-button" :disabled="savingAccount" @click="saveAccountDefaults"><LoaderCircle v-if="savingAccount" :size="16" class="spin" /><Save v-else :size="16" />保存默认配置</button></header>
      <div class="defaults-fields">
          <label class="form-group"><span>注册设置 PIN</span><input v-model="account.registerPin" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="6 位 PIN" /></label>
          <label class="form-group"><span>登录原 PIN</span><input v-model="account.loginPin" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="6 位 PIN" /></label>
          <label class="form-group"><span>登录新 PIN</span><input v-model="account.newPin" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="6" placeholder="新的 6 位 PIN" /></label>
          <label class="form-group"><span>默认任务数量</span><input v-model.number="account.taskCount" class="field" type="number" min="1" max="50" /></label>
          <label class="form-group"><span>默认期望并发</span><input v-model.number="account.concurrency" class="field" type="number" min="1" max="50" /></label>
          <label class="form-group"><span>自动取码超时（秒）</span><input v-model="account.smsOtpTimeoutSeconds" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="2" placeholder="60" /><small>只轮询一次，允许设置 30 到 60 秒；超时后释放 Worker 并等待手动 OTP。</small></label>
          <label class="form-group"><span>手动 OTP 等待超时（秒）</span><input v-model="account.manualOtpTimeoutSeconds" class="field" type="text" inputmode="numeric" autocomplete="off" maxlength="4" placeholder="300" /></label>
          <label class="form-group"><span>默认代理区域</span><DropdownSelect v-model="account.defaultProxyRegion" :options="proxyRegionOptions" :visible-rows="5" aria-label="默认代理区域" /></label>
          <label class="settings-switch"><input v-model="account.changePinEnabled" type="checkbox" /><span><strong>登录后修改 PIN</strong><small>默认开启；关闭后注册页的新 PIN 输入框保持显示但不可用。</small></span></label>
      </div>
    </section>

    <section class="panel dynamic-proxy-card">
      <div class="proxy-settings dynamic-proxy-settings compact-proxy-settings"><header><div><h3>动态区域代理池</h3><p>系统根据代理用户名中的 region-XX 自动生成可选区域。</p></div><div class="proxy-header-actions"><span class="proxy-total"><ShieldCheck :size="16" />{{ account.proxyCount }} 条代理</span><button type="button" class="secondary-button proxy-add-button" @click="openProxyDialog"><Plus :size="15" />添加代理</button></div></header><div class="detected-proxy-heading"><div><strong>已检测区域</strong><small>只有包含实际代理的区域才会显示在注册与登录页</small></div><button v-if="account.proxyCount" type="button" class="proxy-clear-button" @click="clearProxyPool"><Trash2 :size="14" />清空代理池</button></div><div class="proxy-region-slot"><div v-if="account.profiles.length" class="proxy-profile-grid dynamic-proxy-grid"><article v-for="item in account.profiles" :key="item.region" class="configured"><header><span class="proxy-flag">{{ item.region }}</span><div><strong>{{ item.label }}</strong><small>{{ item.description }}</small></div><i /></header><footer><span>{{ item.count }} 条代理</span><b>{{ item.masked }}</b></footer></article></div><div v-else class="proxy-pool-empty"><Smartphone :size="21" /><span>还没有检测到区域代理</span></div></div><div class="proxy-help-strip"><ShieldCheck :size="15" /><span>完整代理凭据加密保存；列表仅显示区域、数量和掩码。</span></div></div>
    </section>

    <section class="sms-provider-grid">
      <section class="panel sms-settings-panel">
        <header class="panel-heading"><div><h2>SMSBower 自动取号与取码</h2><p>API Key 使用 AES-GCM 保存，页面只返回掩码。</p></div><span :class="sms.configured ? 'healthy-value' : ''"><Smartphone :size="15" />{{ sms.configured ? `已配置 ${sms.masked}` : '尚未配置' }}</span></header>
        <form class="sms-settings-form" @submit.prevent="saveSms"><label class="form-group sms-key-field"><span>API Key</span><input v-model="sms.apiKey" class="field" type="text" autocomplete="off" :placeholder="sms.configured ? '留空则保留当前密钥' : '请输入 SMSBower API Key'" /></label><label class="form-group"><span>服务地址</span><input v-model="sms.baseUrl" class="field" type="url" required /></label><label class="form-group"><span>服务代码</span><input v-model="sms.service" class="field" required /></label><label class="form-group"><span>国家代码</span><input v-model="sms.country" class="field" required /></label><button class="primary-button sms-save-button" :disabled="savingSms"><LoaderCircle v-if="savingSms" :size="16" class="spin" /><Save v-else :size="16" />保存 SMSBower</button></form>
      </section>

      <section class="panel sms-settings-panel hero-sms-panel">
        <header class="panel-heading"><div><h2>Hero-SMS 自动取号与取码</h2><p>Gojek 服务 ni · 印度尼西亚国家 ID 6，配置与 SMSBower 隔离。</p></div><span :class="heroSms.configured ? 'healthy-value' : ''"><Smartphone :size="15" />{{ heroSms.configured ? `已配置 ${heroSms.masked}` : '尚未配置' }}</span></header>
        <form class="sms-settings-form" @submit.prevent="saveHeroSms"><label class="form-group sms-key-field"><span>API Key</span><input v-model="heroSms.apiKey" class="field" type="text" autocomplete="off" :placeholder="heroSms.configured ? '留空则保留当前密钥' : '请输入 Hero-SMS API Key'" /></label><label class="form-group"><span>服务地址</span><input v-model="heroSms.baseUrl" class="field" type="url" required /></label><label class="form-group"><span>服务代码</span><input v-model="heroSms.service" class="field" required /></label><label class="form-group"><span>国家 ID</span><input v-model="heroSms.country" class="field" required /></label><div class="sms-provider-actions"><button type="button" class="secondary-button" :disabled="testingHeroSms" @click="testHeroSms"><LoaderCircle v-if="testingHeroSms" :size="16" class="spin" /><CircleDollarSign v-else :size="16" />{{ heroSms.balance ? `余额 ${heroSms.balance}` : '测试连接' }}</button><button class="primary-button" :disabled="savingHeroSms"><LoaderCircle v-if="savingHeroSms" :size="16" class="spin" /><Save v-else :size="16" />保存 Hero-SMS</button></div></form>
      </section>
    </section>

    <section class="settings-grid system-settings-grid">
      <article class="panel settings-card database-card"><header><span><Database :size="19" /></span><div><h2>SQLite 数据库</h2><p>唯一业务状态源</p></div><button class="icon-button" title="刷新系统状态" @click="load"><RefreshCw :size="16" /></button></header><dl><div><dt>数据库路径</dt><dd class="path-value">{{ status.path }}</dd></div><div><dt>结构版本</dt><dd>{{ status.schema_version }}</dd></div><div><dt>日志模式</dt><dd><span class="healthy-value"><CheckCircle2 :size="14" />{{ String(status.journal_mode).toUpperCase() }}</span></dd></div><div><dt>完整性检查</dt><dd>{{ status.quick_check }}</dd></div><div><dt>数据库大小</dt><dd>{{ formatBytes(status.database_bytes) }}</dd></div><div><dt>WAL 大小</dt><dd>{{ formatBytes(status.wal_bytes) }}</dd></div></dl></article>
      <article class="panel settings-card"><header><span><Workflow :size="19" /></span><div><h2>固定 Worker 池</h2><p>租约心跳与任务恢复</p></div></header><div class="worker-hero"><strong>{{ status.worker_pool.alive_workers }}/{{ status.worker_pool.configured_workers }}</strong><span>Worker 在线</span></div><dl><div><dt>运行状态</dt><dd><span class="healthy-value"><CheckCircle2 :size="14" />{{ status.worker_pool.started ? '已启动' : '已停止' }}</span></dd></div><div><dt>活跃任务</dt><dd>{{ status.worker_pool.active_tasks }}</dd></div><div><dt>启动恢复</dt><dd>重排 {{ status.worker_pool.recovery.requeued }} · 复核 {{ status.worker_pool.recovery.needs_review }}</dd></div></dl></article>
      <article class="panel settings-card"><header><span><Radio :size="19" /></span><div><h2>实时数据</h2><p>变更日志与服务端事件</p></div></header><div class="worker-hero"><strong>SEQ</strong><span>断点续传</span></div><ul class="security-list"><li><CheckCircle2 :size="16" /><span><strong>最后事件序号</strong><small>页面断线后从最后序号继续回放</small></span></li><li><CheckCircle2 :size="16" /><span><strong>公开摘要事件</strong><small>实时事件不包含任务载荷和账号密钥</small></span></li></ul></article>
    </section>

    <section class="panel handlers-panel"><header class="panel-heading"><div><h2>已注册任务处理器</h2><p>系统只会创建下列已就绪的任务类型</p></div><span>{{ types.length }} 个类型</span></header><div class="handler-grid"><div v-for="item in types" :key="item.task_type"><span><Settings :size="16" /></span><div><strong>{{ taskTypeLabel(item.task_type) }}</strong><small>{{ item.description }}</small></div><b>{{ item.safe_to_retry ? '可恢复' : '需复核' }}</b></div></div></section>
    <section class="panel launch-panel"><div><span><Server :size="20" /></span><div><h2>启动入口</h2><p>从重构项目根目录直接启动前后端一体服务。</p></div></div><code>.venv/bin/python main.py</code><button class="secondary-button" @click="copyCommand"><Copy :size="15" />复制命令</button></section>

    <div v-if="proxyOpen" class="dialog-backdrop" @click.self="proxyOpen = false"><form class="dialog panel proxy-add-dialog" @submit.prevent="addProxyPool"><header><div><h2>添加区域代理</h2><p>一行粘贴一条代理，系统会自动识别区域、去重并测试连通性。</p></div><button type="button" class="icon-button" title="关闭添加代理窗口" @click="proxyOpen = false"><X :size="17" /></button></header><div class="dialog-body"><label class="form-group proxy-pool-editor"><span>代理地址列表</span><textarea v-model="proxyDraft" class="field" rows="11" spellcheck="false" autofocus placeholder="用户名-region-ID-sid-xxxx:密码@主机:端口&#10;用户名-region-JP-sid-xxxx:密码@主机:端口"></textarea><small>支持 http://、https://、socks5:// 或“用户名:密码@主机:端口”；每次最多测试 100 条，代理池最多保存 500 条。</small></label><div class="proxy-dialog-tip"><ShieldCheck :size="16" /><span>只有连通性测试通过的代理才会加密保存。</span></div><div v-if="proxyTest" class="proxy-test-results"><header><strong>代理测试结果</strong><span>{{ proxyTest.passed }} 条通过 · {{ proxyTest.failed }} 条失败</span></header><div><article v-for="item in proxyTest.results" :key="item.index" :class="item.ok ? 'passed' : 'failed'"><CheckCircle2 v-if="item.ok" :size="15" /><X v-else :size="15" /><span><strong>{{ item.region }} · {{ item.proxy }}</strong><small>{{ item.message }}<template v-if="item.ip"> · 出口 {{ item.ip }}</template></small></span></article></div></div></div><footer><button type="button" class="secondary-button" @click="proxyOpen = false">关闭</button><button class="primary-button" :disabled="savingProxy"><LoaderCircle v-if="savingProxy" :size="16" class="spin" /><ShieldCheck v-else :size="16" />{{ savingProxy ? '正在测试代理' : '测试代理并添加' }}</button></footer></form></div>
  </div>
</template>
