<template>
  <section class="mobile-panel">
    <div class="panel-heading">
      <div><h3>公众号采集与授权状态</h3><p>移动端负责分页列表，Web 授权负责正文；新增公众号后自动采集。</p></div>
      <a-button :loading="loading" @click="refreshStatus">刷新状态</a-button>
    </div>
    <a-alert v-if="loadError" type="error">{{ loadError }}<span v-if="status">（下方为最近一次成功查询的状态）</span></a-alert>
    <a-spin :loading="loading && !status" style="display: block">
      <div v-if="status" class="auth-grid">
        <div class="auth-card">
          <div class="auth-title"><strong>移动端 · 分页列表</strong><a-tag :color="mobileColor">{{ mobileLabel }}</a-tag></div>
          <p>最近授权：{{ formatTime(status.authorized_at) }}</p>
          <p>最近续期：{{ formatTime(status.refreshed_at) }}</p>
          <p v-if="status.refresh_retry_at && status.refresh_error">下次重试：{{ formatTime(status.refresh_retry_at) }}</p>
          <a-alert v-if="status.refresh_error" :type="status.needs_reauth ? 'error' : 'warning'">{{ status.refresh_error.message }}（{{ status.refresh_error.code }}）</a-alert>
          <a-button type="primary" :loading="startingLogin" @click="startLogin">移动端扫码授权</a-button>
        </div>
        <div class="auth-card">
          <div class="auth-title"><strong>Web · 文章正文</strong><a-tag :color="webColor">{{ webLabel }}</a-tag></div>
          <p>最近检查：{{ formatTime(status.web_auth?.checked_at) }}</p>
          <p>最近续期：{{ formatTime(status.web_auth?.refreshed_at) }}</p>
          <a-alert v-if="status.web_auth?.error" :type="status.web_auth.needs_reauth ? 'error' : 'warning'">{{ status.web_auth.error.message }}（{{ status.web_auth.error.code }}）</a-alert>
          <a-button @click="$emit('web-auth')">Web 正文扫码授权</a-button>
        </div>
      </div>
    </a-spin>
    <a-alert v-if="status" :type="status.automatic_for_new_feeds ? 'success' : 'warning'" class="rule-notice">
      {{ status.automatic_for_new_feeds ? '新增公众号自动分页已启用' : '请先完成有效的移动端授权，以启用新增公众号自动分页' }}。
      {{ status.auto_refresh ? `后台每 ${status.maintenance_interval_minutes} 分钟检查，正常间隔 ${status.refresh_interval_hours} 小时续期；需要重新扫码时会在这里提示。` : '自动续期未启用。' }}
    </a-alert>
    <div class="history-section">
      <h3>历史分页与诊断</h3>
      <p>日常新增无需操作。需要更早文章时，选择已订阅的公众号，每次回补最多两页、每页 5 条；正文由后台分批获取。</p>
      <a-alert v-if="feedsError" type="warning">{{ feedsError }} <a-button type="text" size="mini" @click="loadFeeds">重新加载</a-button></a-alert>
      <a-form :model="form" layout="vertical">
        <div class="history-fields">
          <a-form-item label="公众号" field="mp_id">
            <a-select v-model="form.mp_id" placeholder="选择已订阅的公众号" allow-search :loading="loadingFeeds" :disabled="busy">
              <a-option v-for="feed in feeds" :key="feed.id" :value="feed.id">{{ feed.mp_name }}</a-option>
            </a-select>
          </a-form-item>
          <a-form-item label="起始位置 offset" field="offset"><a-input-number v-model="form.offset" :min="0" :max="100000" :precision="0" :disabled="busy" /></a-form-item>
        </div>
        <a-space wrap>
          <a-button type="primary" :disabled="!canCollect" :loading="collecting" @click="collectHistory">回补两页历史</a-button>
          <a-button :disabled="!canCollect" :loading="verifying" @click="verifyHistory">验证两页列表</a-button>
          <a-button v-if="result?.has_more" :disabled="busy || !canCollect" @click="continueHistory">从 {{ result.next_offset }} 继续回补</a-button>
        </a-space>
      </a-form>
      <a-alert v-if="actionError" type="error" class="result-notice">{{ actionError }}</a-alert>
      <a-alert v-if="result" type="success" class="result-notice">
        本次新增 {{ result.collected }} 篇；下一位置 {{ result.next_offset }}。
        {{ result.has_more ? '接口仍提示有后续页，可继续回补。' : '本次已到接口当前返回的历史窗口末尾。' }} 元数据入库不代表正文已全部获取。
      </a-alert>
      <a-alert v-if="verification" type="success" class="result-notice">{{ verification }}</a-alert>
      <a-alert v-if="selectedFeedError" type="warning" class="result-notice">此公众号最近采集异常：{{ selectedFeedError.message || selectedFeedError.code }}；冷却至 {{ formatTime(selectedFeedError.cooldown_until) }}。</a-alert>
    </div>
    <a-modal v-model:visible="qrVisible" title="微信读书移动端授权" :footer="false" :width="360" @cancel="closeLogin">
      <div class="qr-content">
        <img v-if="login?.qrcode" :src="login.qrcode" alt="微信读书移动端授权二维码" />
        <p>{{ login?.message || '正在申请二维码…' }}</p>
        <a-alert v-if="pollError" type="error">{{ pollError }}</a-alert>
        <a-button v-if="login && ['failed', 'expired'].includes(login.status)" :loading="startingLogin" @click="startLogin">重新申请二维码</a-button>
        <p class="qr-help">用微信扫码并确认。关闭窗口不会撤销已完成的授权。</p>
      </div>
    </a-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { Message } from '@arco-design/web-vue'
import { collectMobileHistory, getMobileFeeds, getMobileStatus, startMobileLogin, verifyMobileHistory } from '@/api/wereadMobile'
import type { FeedOption, HistoryResult, MobileLogin, MobileStatus } from '@/api/wereadMobile'

defineEmits<{ (event: 'web-auth'): void }>()
const status = ref<MobileStatus>()
const loading = ref(false)
const loadError = ref('')
const feeds = ref<FeedOption[]>([])
const loadingFeeds = ref(false)
const feedsError = ref('')
const form = reactive({ mp_id: '', offset: 0 })
const collecting = ref(false)
const verifying = ref(false)
const actionError = ref('')
const verification = ref('')
const result = ref<HistoryResult>()
const startingLogin = ref(false)
const qrVisible = ref(false)
const login = ref<MobileLogin>()
const pollError = ref('')
let disposed = false
let statusTimer: ReturnType<typeof setTimeout> | undefined
let qrTimer: ReturnType<typeof setTimeout> | undefined
const busy = computed(() => collecting.value || verifying.value)
const canCollect = computed(() => Boolean(form.mp_id && status.value?.authorized && !loadError.value && !busy.value))
const selectedFeedError = computed(() => status.value?.feed_errors?.[form.mp_id])
const mobileLabel = computed(() => !status.value ? '等待检查' : status.value.needs_reauth ? '需要重新扫码' : status.value.refresh_error ? '续期异常' : status.value.authorized ? '已授权' : '尚未授权')
const mobileColor = computed(() => status.value?.needs_reauth ? 'red' : status.value?.refresh_error ? 'orange' : status.value?.authorized ? 'green' : 'gray')
const webLabel = computed(() => status.value?.web_auth?.needs_reauth ? '需要重新扫码' : status.value?.web_auth?.error ? '续期异常' : status.value?.web_auth?.valid ? '最近检查有效' : '等待检查')
const webColor = computed(() => status.value?.web_auth?.needs_reauth ? 'red' : status.value?.web_auth?.error ? 'orange' : status.value?.web_auth?.valid ? 'green' : 'gray')

function message(error: unknown) { return error instanceof Error ? error.message : typeof error === 'string' ? error : '请求失败，请稍后重试' }
function formatTime(value?: number) { return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '暂无记录' }
async function refreshStatus() {
  if (loading.value || disposed) return
  loading.value = true
  try { status.value = await getMobileStatus(); loadError.value = '' }
  catch (error: unknown) { loadError.value = message(error) }
  finally { loading.value = false }
}
async function loadFeeds() {
  loadingFeeds.value = true
  feedsError.value = ''
  try {
    const all: FeedOption[] = []
    let offset = 0
    while (!disposed) {
      const page = await getMobileFeeds(offset)
      all.push(...page.list.filter(feed => /^MP_WXS_\d+$/.test(feed.id)))
      offset += page.list.length
      if (!page.list.length || offset >= page.total) break
    }
    feeds.value = all
  } catch (error: unknown) { feedsError.value = message(error) }
  finally { loadingFeeds.value = false }
}
async function startLogin() {
  if (startingLogin.value) return
  startingLogin.value = true
  qrVisible.value = true
  pollError.value = ''
  clearTimeout(qrTimer)
  try {
    login.value = await startMobileLogin()
    if (!disposed && qrVisible.value) qrTimer = setTimeout(pollLogin, 3000)
  } catch (error: unknown) { login.value = { status: 'failed', message: message(error), qrcode: '' } }
  finally { startingLogin.value = false }
}
async function pollLogin() {
  if (disposed || !qrVisible.value) return
  try {
    const current = await getMobileStatus()
    status.value = current
    login.value = current.login
    pollError.value = ''
    if (current.login.status === 'success') { Message.success('移动端授权成功，自动分页已启用'); closeLogin(); return }
    if (['failed', 'expired'].includes(current.login.status)) return
  } catch (error: unknown) { pollError.value = message(error) }
  if (!disposed && qrVisible.value) qrTimer = setTimeout(pollLogin, 3000)
}
function closeLogin() { qrVisible.value = false; clearTimeout(qrTimer) }
async function collectHistory() {
  if (!canCollect.value) return
  collecting.value = true
  actionError.value = ''; verification.value = ''; result.value = undefined
  try { result.value = await collectMobileHistory(form.mp_id, form.offset) }
  catch (error: unknown) { actionError.value = message(error) }
  finally { collecting.value = false; await refreshStatus() }
}
function continueHistory() { if (result.value) { form.offset = result.value.next_offset; void collectHistory() } }
async function verifyHistory() {
  if (!canCollect.value) return
  verifying.value = true
  actionError.value = ''; verification.value = ''
  try { const data = await verifyMobileHistory(form.mp_id); verification.value = `跨页验证通过：第一页 ${data.first.items.length} 篇，第二页 ${data.second.items.length} 篇，存在不同文章。` }
  catch (error: unknown) { actionError.value = message(error) }
  finally { verifying.value = false; await refreshStatus() }
}
watch(() => form.mp_id, () => { form.offset = 0; result.value = undefined; actionError.value = ''; verification.value = '' })
async function tick() { await refreshStatus(); if (!disposed) statusTimer = setTimeout(tick, 30000) }
onMounted(() => { void tick(); void loadFeeds() })
onUnmounted(() => { disposed = true; clearTimeout(statusTimer); clearTimeout(qrTimer) })
</script>

<style scoped>
.mobile-panel { margin-bottom: 24px; }
.panel-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 12px; }
h3 { margin: 0 0 8px; color: var(--color-text-1); }
p { color: var(--color-text-2); line-height: 1.7; margin: 6px 0 12px; }
.auth-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.auth-card { border: 1px solid var(--color-border-2); border-radius: 8px; padding: 20px; }
.auth-title { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-bottom: 16px; }
.auth-card .arco-btn { margin-top: 10px; }
.rule-notice, .result-notice { margin-top: 16px; }
.history-section { margin-top: 24px; }
.history-fields { display: grid; grid-template-columns: minmax(220px, 2fr) minmax(160px, 1fr); gap: 16px; max-width: 760px; }
.qr-content { text-align: center; }
.qr-content img { width: 240px; max-width: 100%; }
.qr-help { font-size: 13px; margin-top: 16px; }
@media (max-width: 700px) { .auth-grid, .history-fields { grid-template-columns: 1fr; } .auth-card { padding: 16px; } }
</style>
