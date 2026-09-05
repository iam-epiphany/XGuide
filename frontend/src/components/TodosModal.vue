<script setup>
/**
 * 待办 / DDL / 考试弹窗。从 App.vue 抽出：
 * 新增、完成/恢复、删除与列表加载全部内聚；打开时自动拉取列表。
 */
import { ref, watch } from 'vue'
import { addTodo, completeTodo, deleteTodo, getTodos } from '../lib/backends'

const props = defineProps({
  open: { type: Boolean, default: false },
  apiSettings: { type: Object, required: true }
})

const emit = defineEmits(['close'])

const busy = ref(false)
const newTodoContent = ref('')
const newTodoKind = ref('todo')
const newTodoDue = ref('')
const todoMsg = ref('')
const todos = ref([])

watch(
  () => props.open,
  (open) => {
    if (open) {
      todoMsg.value = ''
      loadTodos()
    }
  }
)

async function loadTodos() {
  try {
    const data = await getTodos(props.apiSettings, 'all')
    todos.value = data.todos || []
  } catch (error) {
    todoMsg.value = error?.detail || error?.message || '请求失败'
  }
}

function kindLabel(kind) {
  return { todo: '待办', ddl: 'DDL', exam: '考试' }[kind] || kind
}

async function doAddTodo() {
  const content = newTodoContent.value.trim()
  if (!content) return
  busy.value = true
  try {
    await addTodo(props.apiSettings, content, newTodoKind.value, newTodoDue.value)
    newTodoContent.value = ''
    newTodoDue.value = ''
    todoMsg.value = '已添加'
    await loadTodos()
  } catch (error) {
    todoMsg.value = error?.detail || error?.message || '请求失败'
  } finally {
    busy.value = false
  }
}

async function doCompleteTodo(t) {
  try {
    await completeTodo(props.apiSettings, t.id, !t.done)
    await loadTodos()
  } catch (error) {
    todoMsg.value = error?.detail || error?.message || '请求失败'
  }
}

async function doDeleteTodo(t) {
  try {
    await deleteTodo(props.apiSettings, t.id)
    await loadTodos()
  } catch (error) {
    todoMsg.value = error?.detail || error?.message || '请求失败'
  }
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h2>待办 / DDL / 考试</h2>
        <button class="modal-close" @click="emit('close')">✕</button>
      </div>

      <section class="kb-section">
        <div class="panel-heading"><h3>新增</h3></div>
        <div class="todo-form">
          <input v-model="newTodoContent" placeholder="事项内容，如：交实验报告" @keydown.enter.prevent="doAddTodo" />
          <select v-model="newTodoKind">
            <option value="todo">待办</option>
            <option value="ddl">截止任务</option>
            <option value="exam">考试</option>
          </select>
          <input v-model="newTodoDue" type="date" title="截止日期（可选）" />
          <button @click="doAddTodo" :disabled="busy || !newTodoContent.trim()">添加</button>
        </div>
        <p v-if="todoMsg" class="kb-status">{{ todoMsg }}</p>
      </section>

      <section class="kb-section">
        <div class="panel-heading">
          <h3>列表</h3>
          <span class="pill soft">{{ todos.length }} 条</span>
        </div>
        <div class="result-list">
          <article v-for="t in todos" :key="t.id" :class="['todo-item', { done: t.done }]">
            <div class="todo-main">
              <strong>{{ t.content }}</strong>
              <span class="pill">{{ kindLabel(t.kind) }}</span>
              <small v-if="t.due_at">截止 {{ t.due_at }}</small>
              <a v-if="t.source_url" class="todo-source" :href="t.source_url" target="_blank" rel="noreferrer">查看来源通知</a>
            </div>
            <div class="todo-actions">
              <button class="mini" @click="doCompleteTodo(t)">{{ t.done ? '恢复' : '完成' }}</button>
              <button class="mini danger-button" @click="doDeleteTodo(t)">删除</button>
            </div>
          </article>
          <p v-if="todos.length === 0" class="no-result">暂无待办，从聊天里说「帮我记个待办」也可以添加</p>
        </div>
      </section>
    </div>
  </div>
</template>
