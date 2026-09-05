<template>
  <main class="app-shell">
    <!-- ── 顶部品牌条 ──────────────────────────────────────────────────────── -->
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">西电</div>
        <div class="brand-text">
          <h1>XGuide</h1>
          <p>你的校园日程与重要通知</p>
        </div>
      </div>
      <div class="topbar-actions">
        <nav class="product-nav" aria-label="主导航">
          <button :class="['nav-tab', { active: activeTab === 'today' }]" @click="openToday">Today</button>
          <button :class="['nav-tab', { active: activeTab === 'inbox' }]" @click="openInbox">Inbox <span v-if="inboxNewCount" class="inbox-count">{{ inboxNewCount }}</span></button>
          <button :class="['nav-tab', { active: activeTab === 'chat' }]" @click="activeTab = 'chat'">Chat</button>
        </nav>
        <button class="kb-button utility-button" @click="openSchedule"><span class="kb-icon">📅</span>课表</button>
        <button class="kb-button utility-button" @click="openTodos"><span class="kb-icon">✅</span>事项</button>
        <button v-if="debugMode" class="kb-button utility-button" @click="openObs"><span class="kb-icon">📡</span>监控</button>
        <button v-if="authUser?.role === 'admin'" class="kb-button" @click="openKb">
          <span class="kb-icon">📚</span> 知识库
        </button>
        <div v-if="authUser" class="account-chip">
          <span class="account-dot"></span>
          <span>{{ authUser.username }}</span>
          <small v-if="authUser.role === 'admin'">管理员</small>
          <button class="account-action" @click="doLogout">退出</button>
        </div>
        <button v-else class="login-button" @click="openAuth()">登录</button>
      </div>
    </header>

    <!-- ── Today：打开产品后的第一屏 ─────────────────────────────────────── -->
    <section v-if="activeTab === 'today'" class="product-page today-page">
      <div v-if="!authUser" class="product-empty">
        <p class="page-kicker">校园个人 Agent</p><h2>把今天过得明白一点。</h2>
        <p>登录后，课表、待办、DDL 和考试会在这里汇成一页。</p>
        <button @click="openAuth()">登录并建立个人日程</button>
      </div>
      <template v-else>
        <header class="today-hero">
          <div><p class="page-kicker">{{ todayDateLabel }}</p><h2>今天，{{ authUser.username }}。</h2><p>{{ todayIntro }}</p></div>
          <button class="quiet-action" @click="loadToday" :disabled="todayBusy">{{ todayBusy ? '更新中…' : '刷新日程' }}</button>
        </header>
        <p v-if="todayError" class="page-error">{{ todayError }}</p>
        <div v-if="todayData" class="today-grid">
          <article class="today-card briefing-card">
            <div class="card-head"><p class="card-label">AI 今日简报</p><span v-if="todayBriefing?.cached" class="briefing-flag">已缓存</span></div>
            <p v-if="briefingBusy" class="empty-copy">正在生成今日简报…</p>
            <p v-else-if="todayBriefing?.text" class="briefing-text">{{ todayBriefing.text }}</p>
            <p v-else class="empty-copy">简报暂未生成（配置 LLM 后可用）。</p>
            <template v-if="freeAdvice?.suggestions?.length">
              <p class="card-label advice-label">空档建议</p>
              <ul class="simple-list">
                <li v-for="s in freeAdvice.suggestions" :key="s.todo_id">
                  <span><b>{{ s.start }}–{{ s.end }}</b> {{ s.content }}<small v-if="s.why">{{ s.why }}</small></span>
                </li>
              </ul>
            </template>
          </article>
          <article class="next-course-card">
            <p class="card-label">下一节课</p>
            <template v-if="todayData.next_course"><h3>{{ todayData.next_course.course }}</h3><p>{{ todayData.next_course.start_time }}–{{ todayData.next_course.end_time }} · {{ todayData.next_course.location || '地点未填写' }}</p></template>
            <template v-else><h3>今天没有下一节课</h3><p>{{ todayData.has_schedule ? '留一点空白给自己。' : '导入课表后，这里会显示下一节课。' }}</p></template>
          </article>
          <article class="today-card courses-card"><div class="card-head"><p class="card-label">今日课程</p><button class="text-action" @click="openSchedule">管理课表</button></div><ul v-if="todayData.courses?.length" class="timeline-list"><li v-for="course in todayData.courses" :key="course.course + course.start_time"><b>{{ course.start_time }}</b><span>{{ course.course }}<small>{{ course.location || '地点未填写' }}</small></span></li></ul><p v-else class="empty-copy">今天没有课程安排。</p></article>
          <article class="today-card"><div class="card-head"><p class="card-label">今天要做</p><button class="text-action" @click="openTodos">管理事项</button></div><ul v-if="todayData.todos?.length" class="simple-list"><li v-for="todo in todayData.todos" :key="todo.id"><button class="check-button" @click="finishTodayTodo(todo)" aria-label="完成事项">○</button><span>{{ todo.content }}</span></li></ul><p v-else class="empty-copy">没有标记为今天的待办。</p></article>
          <article class="today-card deadline-card"><p class="card-label">近期 DDL 与考试</p><ul v-if="todayData.upcoming?.length" class="simple-list"><li v-for="item in todayData.upcoming" :key="item.id"><span><b>{{ item.content }}</b><small>{{ item.due_at }}</small></span><em :class="{ overdue: item.days_left < 0 }">{{ item.status }}</em></li></ul><p v-else class="empty-copy">未来 7 天没有 DDL 或考试。</p></article>
          <article v-if="todayData.reminders?.length" class="today-card reminders-card"><p class="card-label">需要留意</p><ul class="simple-list"><li v-for="item in todayData.reminders" :key="item.id"><span>{{ item.label }} · {{ item.content }}</span><em>{{ item.due_at }}</em></li></ul></article>
        </div>
      </template>
    </section>

    <!-- ── Inbox：只展示画像判断后值得看的公开通知 ───────────────────────── -->
    <section v-else-if="activeTab === 'inbox'" class="product-page inbox-page">
      <div v-if="!authUser" class="product-empty"><h2>先登录，再筛选与你有关的校园通知。</h2><button @click="openAuth()">登录</button></div>
      <template v-else>
        <header class="inbox-hero"><div><p class="page-kicker">XGUIDE · PERSONAL ATTENTION CENTER</p><h2>Focus First.<br><em>Act Next.</em></h2><p>校园信息被整理为与你有关的事件、时机与下一步行动。</p></div><div class="inbox-hero-actions"><button class="quiet-action" @click="openProfile">{{ inboxProfileComplete ? '调整我的画像' : '完善我的画像' }}</button><button class="sync-action" @click="syncInbox" :disabled="inboxBusy">{{ inboxBusy ? '正在感知校园…' : '同步校园动态' }}</button></div></header>
        <div class="profile-strip"><div><b>{{ inboxProfileComplete ? '个人筛选已启用' : '公开通知模式' }}</b><span>{{ inboxProfileComplete ? '排序已结合你的身份、关注方向和事务时效。' : '完成画像后，XGuide 才能判断哪些事项真正与你有关。' }} 新推送保留 {{ inboxTtlHours }} 小时。</span></div><button class="text-action" @click="openProfile">{{ inboxProfileComplete ? '编辑' : '去设置' }} →</button></div>
        <p v-if="inboxError" class="page-error">{{ inboxError }}</p>
        <template v-if="inboxEvents.length">
          <section v-if="inboxNarrative?.text || narrativeBusy" class="narrative-section">
            <p class="narrative-copy">
              <span class="narrative-flag">AI 摘要</span>
              {{ narrativeBusy ? '正在生成收件箱摘要…' : inboxNarrative.text }}
            </p>
          </section>
          <section class="focus-section">
            <div class="section-head"><div><p class="section-index">01 / NOW</p><h3>今日关注</h3><p>现在最值得处理的 {{ inboxBriefing.today_focus?.length || 0 }} 件事</p></div><span class="focus-pulse">● LIVE</span></div>
            <div v-if="inboxBriefing.today_focus?.length" class="focus-grid"><article v-for="event in inboxBriefing.today_focus" :key="event.id" class="focus-card"><div class="focus-top"><span class="category-chip">需要处理</span><strong>{{ event.attention_score }}</strong></div><h4>{{ event.title }}</h4><p class="deadline-copy" v-if="event.deadline">截止 {{ event.deadline }}</p><p class="reason-copy">{{ event.reason }}</p><ul class="reason-list"><li v-for="(score, label) in event.attention_factors" :key="label" v-if="score">{{ label }} {{ score }}</li></ul><div class="event-actions"><a :href="event.source_url" target="_blank" rel="noreferrer">官方原文 ↗</a><button @click="planFromInbox(event)">生成行动清单</button></div></article></div>
            <div v-else class="focus-empty">今天没有迫切事项。同步校园动态或完善画像后，这里会优先呈现你的下一步。</div>
          </section>

          <section class="recommend-section"><div class="section-head"><div><p class="section-index">02 / FOR YOU</p><h3>推荐给你</h3><p>相关但不需要立刻处理的信息</p></div></div><div class="recommend-list"><article v-for="event in inboxBriefing.recommended" :key="event.id" class="recommend-card"><span :class="['category-dot', `dot-${event.category}`]"></span><div><p class="event-overline">{{ categoryLabel(event.category) }} · 关注度 {{ event.attention_score }}</p><h4>{{ event.title }}</h4><p>{{ event.summary || event.reason }}</p><div class="event-mini"><span v-if="event.deadline">截至 {{ event.deadline }}</span><span>为什么：{{ event.reason }}</span></div></div><button class="text-action" @click="planFromInbox(event)">加入计划 →</button></article></div></section>

          <section class="browse-section"><div class="section-head"><div><p class="section-index">03 / EXPLORE</p><h3>分类浏览</h3><p>按你在校园里要完成的事情来组织</p></div></div><div class="category-grid"><button v-for="group in inboxBriefing.categories" :key="group.key" class="category-card" @click="selectedCategory = selectedCategory === group.key ? '' : group.key"><span>{{ categoryIcon(group.key) }}</span><b>{{ categoryLabel(group.key) }}</b><strong>{{ group.count }}</strong><small>{{ selectedCategory === group.key ? '收起事件' : '查看事件' }}</small></button></div><div v-if="selectedCategory" class="category-events"><article v-for="event in selectedCategoryEvents" :key="event.id" class="inbox-event"><div class="event-meta"><span>{{ event.source_name }}</span><time>{{ event.published_at || '最新采集' }}</time></div><h3>{{ event.title }}</h3><p>{{ event.summary || '查看官方原文了解详情。' }}</p><details v-if="event.notification_count > 1" class="event-timeline"><summary>该事件已聚合 {{ event.notification_count }} 条通知</summary><ol><li v-for="item in event.timeline" :key="item.id"><time>{{ item.date || '更新' }}</time><a :href="item.source_url" target="_blank" rel="noreferrer">{{ item.title }}</a></li></ol></details><div class="event-actions"><a :href="event.source_url" target="_blank" rel="noreferrer">官方原文 ↗</a><button class="text-action" @click="markInbox(event, 'ignored')">不再关注</button><button @click="planFromInbox(event)">加入个人计划</button></div></article></div></section>

          <section class="other-section"><div><p class="section-index">04 / ARCHIVE</p><h3>其他校园动态 <span>{{ inboxBriefing.other?.length || 0 }}</span></h3></div><button class="text-action" @click="showArchive = !showArchive">{{ showArchive ? '收起' : '展开' }} →</button></section><div v-if="showArchive" class="category-events archive-list"><article v-for="event in inboxBriefing.other" :key="event.id" class="inbox-event"><h3>{{ event.title }}</h3><p>{{ event.summary || '查看官方原文了解详情。' }}</p><a :href="event.source_url" target="_blank" rel="noreferrer">官方原文 ↗</a></article></div>
        </template>
        <div v-else class="product-empty compact"><h2>{{ inboxBusy ? '正在同步通知…' : '暂时没有可展示的通知。' }}</h2><p>{{ inboxProfileComplete ? '稍后同步，或调整你的关注方向。' : '最近 24 小时没有同步到公开通知；稍后再试或完善画像。' }}</p></div>
      </template>
    </section>

    <!-- ── Chat：产品的一个交互入口 ──────────────────────────────────────── -->
    <section v-else class="chat-area" ref="chatArea">
      <!-- 欢迎页（无对话时） -->
      <div v-if="messages.length === 0" class="welcome">
        <div class="welcome-logo">西电</div>
        <h2>你好，同学！</h2>
        <p class="welcome-sub">选课、校车、食堂、奖学金、教务系统……校园问题都可以问我</p>
        <div class="topic-grid">
          <button
            v-for="topic in topics"
            :key="topic.title"
            class="topic-card"
            @click="askTopic(topic.question)"
          >
            <span class="topic-icon">{{ topic.icon }}</span>
            <strong>{{ topic.title }}</strong>
            <small>{{ topic.desc }}</small>
          </button>
        </div>
        <p class="welcome-tip">💡 点击上面的问题可以直接提问，也可以自己输入</p>
      </div>

      <!-- 消息流（有对话时） -->
      <div v-else class="messages" ref="messageList">
        <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
          <div class="message-meta">
            <span class="message-role">{{ item.role === 'user' ? '你' : '西电校园助手' }}</span>
            <small v-if="item.meta">{{ item.meta }}</small>
          </div>
          <!-- 工具调用过程徽标（Agentic RAG 可视化） -->
          <div v-if="item.toolStatus" class="tool-badge">{{ item.toolStatus }}</div>
          <span class="message-text" v-html="renderMarkdownMemo(item.content)"></span><span v-if="item.streaming" class="stream-cursor">▍</span>
          <details v-if="debugMode && item.execution" class="execution-details" open>
            <summary>执行详情</summary>
            <div class="execution-grid">
              <span><b>路径</b>{{ item.execution.mode || '-' }}</span>
              <span><b>配置</b>{{ item.execution.profile || '-' }}</span>
              <span><b>分类</b>{{ item.execution.classifier_stage || '-' }}</span>
              <span><b>模型</b>{{ item.execution.model || '-' }}</span>
              <span><b>Agent</b>{{ (item.execution.agents || []).join(' → ') || '-' }}</span>
              <span><b>工具</b>{{ (item.execution.tools || []).join(', ') || '-' }}</span>
              <span><b>Token</b>{{ item.execution.input_tokens || 0 }} in / {{ item.execution.output_tokens || 0 }} out</span>
              <span><b>Trace</b>{{ item.execution.trace_id || '-' }}</span>
            </div>
            <p class="execution-reason">{{ item.execution.complexity_reason }}</p>
            <ol v-if="item.execution.tasks?.length" class="task-list">
              <li v-for="task in item.execution.tasks" :key="task.id">
                <b>{{ task.id }}</b> · {{ task.domain }} · {{ task.action }} ·
                {{ task.profile || '-' }} · {{ task.status }} ·
                {{ task.duration_ms || 0 }} ms · {{ (task.input_tokens || 0) + (task.output_tokens || 0) }} tok
                <small v-if="task.depends_on?.length">依赖 {{ task.depends_on.join(', ') }}</small>
                <small v-if="task.tools?.length">工具 {{ task.tools.join(', ') }}</small>
              </li>
            </ol>
          </details>
        </article>
        <div v-if="busy && !streamingMessage" class="message assistant typing">
          <div class="typing-dots"><i></i><i></i><i></i></div>
        </div>
      </div>
    </section>

    <!-- ── 底部输入区 ──────────────────────────────────────────────────────── -->
    <footer v-if="activeTab === 'chat'" class="composer-wrap">
      <form class="composer" @submit.prevent="sendMessage">
        <textarea
          v-model="draft"
          rows="2"
          placeholder="输入问题，例如：这学期选课什么时候开始？"
          @keydown.enter.exact.prevent="sendMessage"
        ></textarea>
        <button v-if="!busy" type="submit" :disabled="!draft.trim()">发送</button>
        <button v-else type="button" @click="stopChat">停止</button>
      </form>
      <p class="composer-hint">内容基于校园公开信息整理，具体事项请以学校官方通知为准</p>
    </footer>

    <KnowledgeModal :open="showKb" :api-settings="settings" @close="closeKb" />

    <ScheduleModal :open="showSchedule" :api-settings="settings" @close="closeSchedule" />

    <TodosModal :open="showTodos" :api-settings="settings" @close="closeTodos" />

    <ProfileModal :open="showProfile" :api-settings="settings" @close="showProfile = false" @saved="onProfileSaved" />

    <ObsModal :open="showObs" :api-settings="settings" @close="closeObs" />

    <AuthCard :open="showAuth" :api-settings="settings" @close="closeAuth" @authenticated="onAuthenticated" />
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import AuthCard from './components/AuthCard.vue'
import KnowledgeModal from './components/KnowledgeModal.vue'
import ScheduleModal from './components/ScheduleModal.vue'
import TodosModal from './components/TodosModal.vue'
import ProfileModal from './components/ProfileModal.vue'
import ObsModal from './components/ObsModal.vue'
import { renderMarkdown } from './lib/markdown'
import { ApiError, setUnauthorizedHandler } from './lib/backends'
import {
  addKnowledge,
  addTodo,
  backendMeta,
  clearSchedule,
  completeTodo,
  createInitialSettings,
  deleteTodo,
  deleteInbox,
  getFreeTimeAdvice,
  getInboxBriefing,
  getInboxNarrative,
  getStudentProfile,
  getToday,
  getTodayBriefing,
  getSchedule,
  getTodos,
  getCurrentUser,
  importScheduleFile,
  importScheduleText,
  logoutUser,
  requestChatStream,
  requestKnowledgeStats,
  requestMonitorSummary,
  requestSearch,
  requestTraceDetail,
  requestTraces,
  refreshInbox,
  saveStudentProfile,
  saveSettings,
  setInboxStatus,
  addInboxToPlan
} from './lib/backends'

const settings = reactive(createInitialSettings())
const activeTab = ref('today')
const messages = ref([])
const draft = ref('')
const busy = ref(false)        // 对话请求进行中（发送按钮/typing 指示器）
const chatController = ref(null)  // 当前流式请求的 AbortController（支持停止生成）
const busyData = ref(false)    // 数据操作进行中（知识库/课表/待办），与对话互不阻塞
const showKb = ref(false)
const messageList = ref(null)
const streamingMessage = ref(null)
const debugMode = new URLSearchParams(window.location.search).get('debug') === '1'
const authUser = ref(null)
const showAuth = ref(false)

// 可观测性：Agent 统计 / 告警 / Trace
const showObs = ref(false)

// 个人数据中心：课表
const showSchedule = ref(false)

// 个人数据中心：待办 / DDL / 考试
const showTodos = ref(false)

// P0 Today
const todayData = ref(null)
const todayBusy = ref(false)
const todayError = ref('')
const todayDateLabel = ref('TODAY')
// LLM 简报：Today 晨报 + 空档建议（与主数据并行加载，失败静默降级）
const todayBriefing = ref(null)
const freeAdvice = ref(null)
const briefingBusy = ref(false)

// P1 Inbox 与稳定学生画像
const inboxEvents = ref([])
const inboxBriefing = reactive({ today_focus: [], recommended: [], categories: [], other: [] })
const inboxBusy = ref(false)
const inboxError = ref('')
const inboxProfileComplete = ref(false)
const inboxTtlHours = ref(48)
const inboxNarrative = ref(null)
const narrativeBusy = ref(false)
const selectedInboxIds = ref([])
const selectedCategory = ref('')
const showArchive = ref(false)
const showProfile = ref(false)
const inboxNewCount = computed(() => inboxEvents.value.filter((event) => event.status === 'new').length)
const selectedCategoryEvents = computed(() => inboxBriefing.categories.find((group) => group.key === selectedCategory.value)?.events || [])
const todayIntro = computed(() => {
  if (!todayData.value) return '正在整理你的课表、事项和提醒。'
  const n = todayData.value.next_course
  return n ? `下一节是 ${n.start_time} 的 ${n.course}。` : '看看近期安排，把重要的事情安排好。'
})

function categoryLabel(key) {
  return { action: '需要处理', opportunity: '机会', academic: '学业', campus_life: '校园生活', announcement: '普通通知' }[key] || '校园动态'
}

function categoryIcon(key) {
  return { action: '✓', opportunity: '✦', academic: '⌁', campus_life: '○' }[key] || '·'
}

// 校园话题推荐（欢迎页卡片）
const topics = [
  { icon: '📅', title: '我的课表', desc: '今天有什么课？', question: '今天有什么课？' },
  { icon: '☀️', title: '天气', desc: '明天要带伞吗？', question: '明天南校区天气怎么样？' },
  { icon: '📖', title: '选课指南', desc: '什么时候选课？怎么选？', question: '这学期选课什么时候开始？' },
  { icon: '🚌', title: '校车时刻', desc: '下一班校车几点？', question: '下一班从南校区到北校区的校车几点？' },
  { icon: '🍜', title: '食堂信息', desc: '几点开门、几点关门？', question: '南校区食堂几点关门？' },
  { icon: '🎓', title: '考试安排', desc: '最近有什么考试？', question: '我最近的考试安排是什么？' },
  { icon: '🏠', title: '宿舍生活', desc: '报修、水电、门禁？', question: '宿舍设施坏了怎么报修？' },
  { icon: '🖥️', title: '教务系统', desc: '登录不上怎么办？', question: '教务系统登录不上怎么办？' },
]


watch(
  () => settings.conversationId,
  () => persist()
)

onMounted(async () => {
  // 会话过期（任何接口返回 401）→ 统一清登录态并弹登录卡
  setUnauthorizedHandler(() => {
    authUser.value = null
    settings.userId = 'anonymous'
    openAuth()
  })
  await restoreSession()
  if (authUser.value) loadToday()
})

async function restoreSession() {
  try {
    const data = await getCurrentUser(settings)
    authUser.value = data.authenticated ? data.user : null
    settings.userId = authUser.value?.id || 'anonymous'
  } catch {
    authUser.value = null
    settings.userId = 'anonymous'
  }
}

function openAuth() {
  showAuth.value = true
}

function closeAuth() {
  showAuth.value = false
}

// AuthCard 提交成功回调：会话状态迁移留在父级（组件只管表单与请求）
async function onAuthenticated(user) {
  authUser.value = user
  settings.userId = user.id
  settings.conversationId = ''
  messages.value = []  // 登录后清空匿名会话消息，避免残留挂在新会话下
  showAuth.value = false
  persist()
  await loadToday()
}

async function doLogout() {
  try {
    await logoutUser(settings)
  } finally {
    authUser.value = null
    settings.userId = 'anonymous'
    settings.conversationId = ''
    messages.value = []
    activeTab.value = 'today'
    todayData.value = null
    persist()
  }
}

function readableError(error) {
  // ApiError：detail 已是后端给的可读文案
  if (error instanceof ApiError && typeof error.detail === 'string' && error.detail.trim()) {
    return error.detail
  }
  const raw = String(error?.message || error)
  try {
    const jsonStart = raw.indexOf('{')
    if (jsonStart >= 0) {
      const detail = JSON.parse(raw.slice(jsonStart)).detail
      // 只有 detail 是非空字符串时才采用，避免误切正文或返回 undefined
      if (typeof detail === 'string' && detail.trim()) return detail
    }
  } catch {
    // 保留原始错误
  }
  return raw
}

// ── P0 Today / Reminder ─────────────────────────────────────────────────────

async function openToday() {
  activeTab.value = 'today'
  if (authUser.value) await loadToday()
}

async function loadToday() {
  if (!authUser.value) return
  todayBusy.value = true
  todayError.value = ''
  try {
    todayData.value = await getToday(settings)
    const date = new Date(`${todayData.value.date}T12:00:00`)
    todayDateLabel.value = Number.isNaN(date.getTime()) ? todayData.value.date : date.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })
  } catch (error) {
    todayError.value = readableError(error)
  } finally {
    todayBusy.value = false
  }
  loadTodayBriefing()
}

// AI 简报与空档建议：主数据加载完成后异步拉取，不阻塞课表/待办渲染
async function loadTodayBriefing() {
  briefingBusy.value = true
  try {
    const [briefing, advice] = await Promise.allSettled([getTodayBriefing(settings), getFreeTimeAdvice(settings)])
    todayBriefing.value = briefing.status === 'fulfilled' && briefing.value.available ? briefing.value : null
    freeAdvice.value = advice.status === 'fulfilled' && advice.value.available ? advice.value : null
  } catch {
    todayBriefing.value = null
    freeAdvice.value = null
  } finally {
    briefingBusy.value = false
  }
}

async function finishTodayTodo(todo) {
  try {
    await completeTodo(settings, todo.id, true)
    await loadToday()
  } catch (error) {
    todayError.value = readableError(error)
  }
}

// ── P1 Inbox / Profile ─────────────────────────────────────────────────────

async function openInbox() {
  activeTab.value = 'inbox'
  if (authUser.value) await loadInbox()
}

async function loadInbox() {
  inboxBusy.value = true
  inboxError.value = ''
  try {
    const data = await getInboxBriefing(settings)
    inboxEvents.value = data.events || []
    Object.assign(inboxBriefing, { today_focus: [], recommended: [], categories: [], other: [], ...data })
    inboxProfileComplete.value = Boolean(data.profile_complete)
    inboxTtlHours.value = data.ttl_hours || 48
    selectedInboxIds.value = selectedInboxIds.value.filter((id) => inboxEvents.value.some((event) => event.id === id))
  } catch (error) {
    inboxError.value = readableError(error)
  } finally {
    inboxBusy.value = false
  }
  // LLM 摘要与主列表并行：只在有内容时展示，失败静默
  if (inboxEvents.value.length) {
    narrativeBusy.value = true
    inboxNarrative.value = null
    try {
      const data = await getInboxNarrative(settings)
      inboxNarrative.value = data.available ? data : null
    } catch {
      inboxNarrative.value = null
    } finally {
      narrativeBusy.value = false
    }
  } else {
    inboxNarrative.value = null
  }
}

async function syncInbox() {
  inboxBusy.value = true
  inboxError.value = ''
  try {
    const result = await refreshInbox(settings)
    if (result.errors?.length) inboxError.value = `部分来源暂不可用：${result.errors.map((item) => item.source).join('、')}`
    await loadInbox()
  } catch (error) {
    inboxError.value = readableError(error)
    inboxBusy.value = false
  }
}

async function markInbox(event, status) {
  try {
    await setInboxStatus(settings, event.id, status)
    await loadInbox()
  } catch (error) {
    inboxError.value = readableError(error)
  }
}

async function planFromInbox(event) {
  try {
    await addInboxToPlan(settings, event.id)
    await loadInbox()
    activeTab.value = 'today'
    await loadToday()
  } catch (error) {
    inboxError.value = readableError(error)
  }
}

function openProfile() {
  if (!authUser.value) return openAuth()
  showProfile.value = true
}

// ProfileModal 保存回调：刷新 Inbox 的筛选开关与内容
async function onProfileSaved(complete) {
  inboxProfileComplete.value = complete
  await loadInbox()
}

// ── Markdown 渲染缓存（流式 delta 每帧全量重渲，用 memo 避免重复计算）────────
const mdCache = new Map()
const MAX_MD_CACHE = 200
function renderMarkdownMemo(text) {
  const key = text || ''
  if (mdCache.has(key)) return mdCache.get(key)
  const html = renderMarkdown(key)
  if (mdCache.size >= MAX_MD_CACHE) {
    mdCache.delete(mdCache.keys().next().value)
  }
  mdCache.set(key, html)
  return html
}

function persist() {
  saveSettings(settings)
}

function createId() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID()
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function askTopic(question) {
  draft.value = question
  sendMessage()
}

function stopChat() {
  chatController.value?.abort()
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content) return
  messages.value.push({ id: createId(), role: 'user', content })
  // 长会话内存保护：只保留最近 100 条
  if (messages.value.length > 100) {
    messages.value = messages.value.slice(-100)
  }
  draft.value = ''
  busy.value = true
  // 新发送中止上一轮仍在进行的流（旧流结果会被新一轮覆盖，继续拉流纯属浪费）
  if (chatController.value) chatController.value.abort()
  chatController.value = new AbortController()

  // 流式消息占位（SSE 逐 token 渲染）
  const assistantMsg = {
    id: createId(),
    role: 'assistant',
    content: '',
    meta: '',
    toolStatus: '',
    execution: null,
    streaming: true
  }
  messages.value.push(assistantMsg)
  streamingMessage.value = assistantMsg

  try {
    const done = await requestChatStream(settings, content, {
      signal: chatController.value.signal,
      onEvent: (ev) => {
        if (ev.type === 'delta') {
          assistantMsg.content += ev.text
        } else if (ev.type === 'tool') {
          if (ev.status === 'start') {
            assistantMsg.toolStatus = `🔍 ${ev.name} · ${ev.input?.query || '检索中…'}`
          } else {
            const titles = (ev.titles || []).slice(0, 3).join(' / ')
            assistantMsg.toolStatus = `✅ 检索完成${titles ? '：' + titles : ''}`
          }
        } else if (ev.type === 'meta') {
          assistantMsg.meta = [ev.domain, ev.action, ev.agent, ev.profile, ev.mode].filter(Boolean).join(' · ')
        }
      }
    })
    if (done.conv_id && !settings.conversationId) {
      settings.conversationId = done.conv_id
      persist()
    }
    assistantMsg.content = done.response
    assistantMsg.execution = done.execution || null
    assistantMsg.streaming = false
    assistantMsg.meta = [
      done.intent,
      done.agent_type,
      done.knowledge_used ? 'RAG' : ''
    ].filter(Boolean).join(' · ')
  } catch (error) {
    if (error?.name === 'AbortError') {
      assistantMsg.content = assistantMsg.content || '（已停止生成）'
      assistantMsg.meta = '已停止'
    } else {
      assistantMsg.content = readableError(error)
      assistantMsg.meta = '请求失败'
    }
    assistantMsg.streaming = false
  } finally {
    busy.value = false
    chatController.value = null
    streamingMessage.value = null
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

// ── 知识库（管理员门禁，数据逻辑在 KnowledgeModal）────────────────────────────

function openKb() {
  if (authUser.value?.role !== 'admin') return
  showKb.value = true
}

function closeKb() {
  showKb.value = false
}

function openObs() {
  showObs.value = true
}

function closeObs() {
  showObs.value = false
}

// ── 我的课表 ─────────────────────────────────────────────────────────────────

async function openSchedule() {
  if (!authUser.value) {
    openAuth()
    return
  }
  showSchedule.value = true
}

function closeSchedule() {
  showSchedule.value = false
}

// ── 待办 / DDL / 考试 ────────────────────────────────────────────────────────

async function openTodos() {
  if (!authUser.value) {
    openAuth()
    return
  }
  showTodos.value = true
}

function closeTodos() {
  showTodos.value = false
}

</script>
