<script setup>
/**
 * 知识库弹窗（开发者工具）。从 App.vue 抽出：
 * 检索 / 手动导入 / 文件上传，打开时刷新片段统计。
 * 管理员门禁留在父级（openKb），组件只管自身数据与请求。
 */
import { ref, watch } from 'vue'
import { addKnowledge, requestKnowledgeStats, requestSearch, uploadKnowledge } from '../lib/backends'

const props = defineProps({
  open: { type: Boolean, default: false },
  apiSettings: { type: Object, required: true }
})

const emit = defineEmits(['close'])

const busy = ref(false)
const searchQuery = ref('')
const docTitle = ref('')
const docContent = ref('')
const searchResults = ref([])
const searched = ref(false)
const statusText = ref('')
const knowledgeCount = ref('-')

watch(
  () => props.open,
  (open) => {
    if (open) loadStats()
  }
)

async function loadStats() {
  try {
    const stats = await requestKnowledgeStats(props.apiSettings)
    knowledgeCount.value = stats.total_chunks ?? stats.totalChunks ?? '-'
  } catch {
    // 后端不可用时保持现状
  }
}

async function searchKnowledge() {
  busy.value = true
  searched.value = true
  try {
    const data = await requestSearch(props.apiSettings, searchQuery.value, 5)
    searchResults.value = data.results || []
  } catch (error) {
    statusText.value = error?.detail || error?.message || '请求失败'
  } finally {
    busy.value = false
  }
}

async function submitKnowledge() {
  busy.value = true
  try {
    const data = await addKnowledge(props.apiSettings, [
      { title: docTitle.value.trim(), content: docContent.value.trim() }
    ])
    statusText.value = data.message || JSON.stringify(data)
    docTitle.value = ''
    docContent.value = ''
    await loadStats()
  } catch (error) {
    statusText.value = error?.detail || error?.message || '请求失败'
  } finally {
    busy.value = false
  }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const data = await uploadKnowledge(props.apiSettings, file)
    statusText.value = data.message || JSON.stringify(data)
    await loadStats()
  } catch (error) {
    statusText.value = error?.detail || error?.message || '请求失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h2>知识库</h2>
        <button class="modal-close" @click="emit('close')">✕</button>
      </div>

      <section class="kb-section">
        <div class="panel-heading">
          <h3>检索</h3>
          <span class="pill soft">{{ knowledgeCount }} 个片段</span>
        </div>
        <div class="inline-form">
          <input v-model="searchQuery" placeholder="输入关键词，如：校车、选课" @keydown.enter.prevent="searchKnowledge" />
          <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">检索</button>
        </div>
        <div class="result-list">
          <article v-for="item in searchResults" :key="item.id || item.title" class="result-item">
            <strong>{{ item.title || '未命名结果' }}</strong>
            <span>相关度 {{ item.score ?? '-' }}</span>
            <p>{{ item.content }}</p>
          </article>
          <p v-if="searched && searchResults.length === 0" class="no-result">没有检索到相关内容</p>
        </div>
      </section>

      <section class="kb-section">
        <div class="panel-heading">
          <h3>导入知识</h3>
        </div>
        <label>
          <span>标题</span>
          <input v-model="docTitle" placeholder="如：校车时刻说明" />
        </label>
        <label>
          <span>内容</span>
          <textarea v-model="docContent" rows="4" placeholder="输入知识库内容"></textarea>
        </label>
        <div class="actions">
          <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
          <label class="file-button">
            上传文件
            <input type="file" accept=".txt,.md,.json" @change="handleUpload" />
          </label>
        </div>
      </section>

      <p v-if="statusText" class="kb-status">{{ statusText }}</p>
    </div>
  </div>
</template>
