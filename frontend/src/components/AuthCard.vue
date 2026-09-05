<script setup>
/**
 * 登录 / 注册弹窗（从 App.vue 抽出的第一个业务组件）。
 *
 * 组件自治：mode / busy / error / 表单状态与提交逻辑全部内聚；
 * 父组件只管开关（open）与结果（@authenticated），登录后的
 * 会话状态迁移（settings / messages / persist / loadToday）留在父级。
 * 样式沿用全局 styles.css 的 auth-* 类。
 */
import { reactive, ref, watch } from 'vue'
import { loginUser, registerUser } from '../lib/backends'

const props = defineProps({
  open: { type: Boolean, default: false },
  apiSettings: { type: Object, required: true }
})

const emit = defineEmits(['close', 'authenticated'])

const mode = ref('login')
const busy = ref(false)
const error = ref('')
const form = reactive({ username: '', password: '' })

// 每次打开都重置：残留的表单密码/错误文案不该跨会话显示
watch(
  () => props.open,
  (open) => {
    if (open) {
      mode.value = 'login'
      error.value = ''
      form.password = ''
    }
  }
)

function toggleMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
}

async function submit() {
  busy.value = true
  error.value = ''
  try {
    const action = mode.value === 'login' ? loginUser : registerUser
    const data = await action(props.apiSettings, form.username, form.password)
    form.password = ''
    emit('authenticated', data.user)
  } catch (err) {
    error.value = err?.detail || err?.message || '请求失败，请稍后重试'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="open" class="auth-mask" @click.self="emit('close')">
    <section class="auth-card" role="dialog" aria-modal="true" aria-labelledby="auth-title">
      <div class="auth-ticket">
        <span>XD</span>
        <small>ECHO PASS</small>
      </div>
      <div class="auth-content">
        <button class="auth-close" aria-label="关闭" @click="emit('close')">✕</button>
        <p class="auth-eyebrow">西电校园助手 · 个人空间</p>
        <h2 id="auth-title">{{ mode === 'login' ? '登录校园通行证' : '创建校园通行证' }}</h2>
        <p class="auth-note">登录后，课表、待办和对话记忆只属于你。</p>
        <form class="auth-form" @submit.prevent="submit">
          <label>
            <span>用户名</span>
            <input v-model.trim="form.username" autocomplete="username" minlength="3" maxlength="32" required autofocus />
          </label>
          <label>
            <span>密码</span>
            <input v-model="form.password" type="password" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" minlength="6" maxlength="128" required />
          </label>
          <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
          <button class="auth-submit" :disabled="busy">
            {{ busy ? '处理中…' : mode === 'login' ? '登录' : '注册并登录' }}
          </button>
        </form>
        <button class="auth-switch" @click="toggleMode">
          {{ mode === 'login' ? '还没有账号？立即注册' : '已有账号？返回登录' }}
        </button>
      </div>
    </section>
  </div>
</template>
