<script setup>
/**
 * 可观测性弹窗：Agent 实时统计 + 活跃告警 + Trace 瀑布。从 App.vue 抽出。
 * 权限门禁留在父级；阈值内联样式标记（0.9 成功率 / 3000ms 均延迟 / 8000ms P95）。
 */
import { ref, watch } from 'vue'
import { requestMonitorSummary, requestTraceDetail, requestTraces } from '../lib/backends'

const props = defineProps({
  open: { type: Boolean, default: false },
  apiSettings: { type: Object, required: true }
})

const emit = defineEmits(['close'])

const obsBusy = ref(false)
const obsError = ref('')
const obsAgents = ref([])
const obsAlerts = ref([])
const obsSuggestions = ref([])
const obsTraces = ref([])
const expandedTrace = ref(null)
const traceDetail = ref(null)

watch(
  () => props.open,
  (open) => {
    if (open) loadObs()
  }
)

async function loadObs() {
  obsBusy.value = true
  obsError.value = ''
  try {
    const [mon, traces] = await Promise.all([
      requestMonitorSummary(props.apiSettings),
      requestTraces(props.apiSettings, 20)
    ])
    obsAgents.value = Object.entries(mon.agent_stats || {}).map(([key, v]) => ({ key, ...v }))
    obsAlerts.value = mon.active_alerts || []
    obsSuggestions.value = mon.suggestions || []
    obsTraces.value = traces.traces || []
  } catch (err) {
    obsError.value = String(err?.message || err).slice(0, 300)
    obsAgents.value = []
    obsAlerts.value = []
    obsSuggestions.value = []
    obsTraces.value = []
  } finally {
    obsBusy.value = false
  }
}

async function toggleTrace(traceId) {
  if (expandedTrace.value === traceId) {
    expandedTrace.value = null
    traceDetail.value = null
    return
  }
  expandedTrace.value = traceId
  traceDetail.value = null
  try {
    traceDetail.value = await requestTraceDetail(props.apiSettings, traceId)
  } catch (err) {
    obsError.value = String(err?.message || err).slice(0, 300)
  }
}

function fmtTime(ts) {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="emit('close')">
    <div class="modal obs-modal">
      <div class="modal-head">
        <h2>可观测性</h2>
        <button class="modal-close" @click="emit('close')">✕</button>
      </div>
      <p v-if="obsError" class="obs-error">{{ obsError }}</p>

      <section class="kb-section">
        <div class="panel-heading">
          <h3>Agent 实时统计</h3>
          <button class="kb-button mini" @click="loadObs" :disabled="obsBusy">{{ obsBusy ? '加载中…' : '刷新' }}</button>
        </div>
        <table v-if="obsAgents.length" class="obs-table">
          <thead>
            <tr><th>Profile</th><th>请求</th><th>成功率</th><th>均延迟</th><th>P50</th><th>P95</th><th>在途</th></tr>
          </thead>
          <tbody>
            <tr v-for="a in obsAgents" :key="a.key">
              <td>{{ a.key }}</td>
              <td>{{ a.total }}</td>
              <td :class="a.total && a.success_rate < 0.9 ? 'bad' : ''">{{ (a.success_rate * 100).toFixed(0) }}%</td>
              <td :class="a.avg_ms > 3000 ? 'bad' : ''">{{ Math.round(a.avg_ms) }} ms</td>
              <td>{{ a.p50_ms ?? '-' }} ms</td>
              <td :class="a.p95_ms > 8000 ? 'bad' : ''">{{ a.p95_ms ?? '-' }} ms</td>
              <td>{{ a.in_flight }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="no-result">暂无请求记录 —— 先发几条消息再回来刷新</p>

        <template v-if="obsAlerts.length">
          <h4 class="obs-sub">活跃告警</h4>
          <ul class="obs-list">
            <li v-for="(a, i) in obsAlerts" :key="i" class="obs-alert">
              ⚠ [{{ a.severity }}] {{ a.metric }} = {{ a.value }}（阈值 {{ a.threshold }}）{{ a.message }}
            </li>
          </ul>
        </template>
        <template v-if="obsSuggestions.length">
          <h4 class="obs-sub">优化建议</h4>
          <ul class="obs-list">
            <li v-for="(s, i) in obsSuggestions" :key="i">💡 {{ s.title }}</li>
          </ul>
        </template>
      </section>

      <section class="kb-section">
        <div class="panel-heading">
          <h3>最近 Trace（{{ obsTraces.length }} 条，保留 1 小时）</h3>
        </div>
        <ul class="obs-list traces">
          <li v-for="t in obsTraces" :key="t.trace_id">
            <button class="trace-row" @click="toggleTrace(t.trace_id)">
              <span class="trace-id">{{ t.trace_id }}</span>
              <span>{{ t.name }}</span>
              <span>{{ Math.round(t.duration_ms) }} ms</span>
              <span>{{ (t.spans || []).length }} spans</span>
              <span>{{ fmtTime(t.ts) }}</span>
            </button>
            <div v-if="expandedTrace === t.trace_id" class="trace-detail">
              <p v-if="traceDetail" class="muted">tags: {{ JSON.stringify(traceDetail.tags || {}) }}</p>
              <table class="obs-table">
                <thead><tr><th>span</th><th>耗时</th><th>meta</th></tr></thead>
                <tbody>
                  <tr v-for="(s, i) in (traceDetail?.spans || [])" :key="i">
                    <td>{{ s.name }}</td>
                    <td>{{ Math.round(s.duration_ms) }} ms</td>
                    <td class="span-meta">{{ JSON.stringify(s.meta || {}) }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-if="traceDetail && !traceDetail.spans?.length" class="no-result">该 trace 无 span 记录</p>
            </div>
          </li>
        </ul>
        <p v-if="obsTraces.length === 0" class="no-result">暂无 trace —— 发一条消息后自动产生</p>
      </section>
    </div>
  </div>
</template>
