<script setup>
/**
 * 我的课表弹窗。从 App.vue 抽出：
 * .ics/.json 导入、清空、周视图课表（像素布局与配色内聚在本组件）。
 * 登录门禁留在父级（openSchedule），组件只负责加载与展示。
 */
import { computed, ref, watch } from 'vue'
import { clearSchedule, getSchedule, importScheduleFile, importScheduleText } from '../lib/backends'

const props = defineProps({
  open: { type: Boolean, default: false },
  apiSettings: { type: Object, required: true }
})

const emit = defineEmits(['close'])

const busy = ref(false)
const scheduleMsg = ref('')
const scheduleCourses = ref([])
const scheduleWeekNum = ref('-')
const scheduleInSemester = ref(true)
const scheduleLoaded = ref(false)
// 粘贴文本导入（LLM 解析 + 规则兜底）
const scheduleText = ref('')
const textBusy = ref(false)

const scheduleDays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const timetableHours = Array.from({ length: 14 }, (_, index) => `${String(index + 8).padStart(2, '0')}:00`)
// 与 styles.css 的 .course-block CSS 变量约定配套（--course-bg/accent/ink）
const timetablePalette = [
  ['#eef4ff', '#3f6db8', '#22406e'], ['#f0fbf1', '#3f9e54', '#1f5e2c'],
  ['#fff7e8', '#c98a2b', '#7a5314'], ['#f3efff', '#7a5ec9', '#46328a'],
  ['#fff0f3', '#c95f79', '#8c3450'], ['#eaf8f7', '#218f8d', '#16615f'],
]

const scheduleByDay = computed(() => {
  const groups = Object.fromEntries(scheduleDays.map((d) => [d, []]))
  for (const c of scheduleCourses.value) {
    const name = scheduleDays[c.day_of_week] || '周一'
    if (groups[name]) groups[name].push(c)
  }
  return groups
})

watch(
  () => props.open,
  (open) => {
    if (open) loadSchedule()
  }
)

async function loadSchedule() {
  busy.value = true
  scheduleLoaded.value = false
  try {
    const data = await getSchedule(props.apiSettings)
    scheduleCourses.value = data.courses || []
    scheduleWeekNum.value = data.week_num ?? '-'
    scheduleInSemester.value = data.in_semester !== false
    scheduleLoaded.value = true
  } catch (error) {
    scheduleMsg.value = error?.detail || error?.message || '请求失败'
    scheduleLoaded.value = true
  } finally {
    busy.value = false
  }
}

async function handleScheduleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  scheduleMsg.value = '导入中…'
  try {
    const data = await importScheduleFile(props.apiSettings, file)
    scheduleMsg.value = data.message || JSON.stringify(data)
    await loadSchedule()
  } catch (error) {
    scheduleMsg.value = error?.detail || error?.message || '请求失败'
  } finally {
    busy.value = false
  }
}

async function handleScheduleTextImport() {
  const text = scheduleText.value.trim()
  if (!text) return
  textBusy.value = true
  scheduleMsg.value = '正在解析课表文本…'
  try {
    const data = await importScheduleText(props.apiSettings, text)
    scheduleMsg.value = data.message || JSON.stringify(data)
    scheduleText.value = ''
    await loadSchedule()
  } catch (error) {
    scheduleMsg.value = error?.detail || error?.message || '请求失败'
  } finally {
    textBusy.value = false
  }
}

async function doClearSchedule() {
  if (!confirm('确定清空课表吗？清空后需要重新导入。')) return
  busy.value = true
  try {
    const data = await clearSchedule(props.apiSettings)
    scheduleMsg.value = data.message || '已清空'
    scheduleCourses.value = []
  } catch (error) {
    scheduleMsg.value = error?.detail || error?.message || '请求失败'
  } finally {
    busy.value = false
  }
}

function courseBlockStyle(course) {
  const toMinutes = (value) => { const [hour, minute] = String(value || '08:00').split(':').map(Number); return hour * 60 + minute }
  const start = toMinutes(course.start_time)
  const end = toMinutes(course.end_time)
  const top = Math.max(0, (start - 8 * 60) / 60 * 58)
  const height = Math.max(42, (end - start) / 60 * 58 - 5)
  const hash = [...String(course.course || '')].reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0)
  const [background, accent, ink] = timetablePalette[hash % timetablePalette.length]
  return { top: `${top}px`, height: `${height}px`, '--course-bg': background, '--course-accent': accent, '--course-ink': ink }
}

function weeksLabel(weeks) {
  return weeks ? `第 ${weeks} 周` : '全周'
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="emit('close')">
    <div class="modal modal-wide">
      <div class="modal-head">
        <h2>我的课表</h2>
        <button class="modal-close" @click="emit('close')">✕</button>
      </div>

      <section class="kb-section">
        <div class="panel-heading">
          <h3>导入课表</h3>
        </div>
        <p class="schedule-tip">
          教务系统导出 <code>.ics</code> 日历文件（选课 → 导出课表）或 <code>.json</code> 课表上传，
          导入后即可问「今天有什么课？」
        </p>
        <div class="actions">
          <label class="file-button">
            上传 .ics / .json
            <input type="file" accept=".ics,.json" @change="handleScheduleUpload" />
          </label>
          <button class="danger-button" v-if="scheduleCourses.length" @click="doClearSchedule" :disabled="busy">清空课表</button>
        </div>
        <p class="schedule-tip" style="margin-top: 14px;">
          也可以把教务系统网页上的课表<b>整段复制粘贴</b>到下面，由 AI 解析（无法识别的行会跳过）：
        </p>
        <textarea
          v-model="scheduleText"
          rows="5"
          placeholder="例如：高等数学 周一 08:00-09:40 B栋101 …"
          style="width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; font: inherit; resize: vertical;"
        ></textarea>
        <div class="actions" style="margin-top: 10px;">
          <button @click="handleScheduleTextImport" :disabled="textBusy || !scheduleText.trim()">
            {{ textBusy ? '解析中…' : '解析并导入' }}
          </button>
        </div>
        <p v-if="scheduleMsg" class="kb-status">{{ scheduleMsg }}</p>
      </section>

      <section class="kb-section" v-if="scheduleCourses.length">
        <div class="panel-heading">
          <div><h3>课表总览</h3><p class="schedule-caption">{{ scheduleInSemester ? '第 ' + scheduleWeekNum + ' 周进行中；完整课表按课程周次标注。' : '当前不在教学周；仍保留并展示已导入的完整课表。' }}</p></div>
          <span class="pill soft">{{ scheduleCourses.length }} 节课</span>
        </div>
        <div class="timetable-shell" aria-label="每周课表">
          <div class="timetable">
            <div class="timetable-head"><span>时间</span><span v-for="day in scheduleDays" :key="day">{{ day }}</span></div>
            <div class="timetable-body">
              <div class="timetable-times"><span v-for="time in timetableHours" :key="time">{{ time }}</span></div>
              <div v-for="day in scheduleDays" :key="day" class="timetable-day">
                <article v-for="c in scheduleByDay[day]" :key="c.id || c.course + c.day_of_week + c.start_time" class="course-block" :style="courseBlockStyle(c)">
                  <b>{{ c.course }}</b><span>{{ c.start_time }}–{{ c.end_time }}</span><small>{{ c.location || '地点未填' }}</small><em>{{ weeksLabel(c.weeks) }}</em>
                </article>
              </div>
            </div>
          </div>
        </div>
      </section>

      <p v-if="!scheduleCourses.length && scheduleLoaded" class="kb-status">
        还没有课程。上传教务系统导出的 .ics 文件即可导入课表。
      </p>
    </div>
  </div>
</template>
