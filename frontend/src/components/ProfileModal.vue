<script setup>
/**
 * 学生画像弹窗（Inbox 的确定性筛选条件）。从 App.vue 抽出：
 * 打开时拉取当前画像，保存后 emit('saved', 是否已具备筛选条件)，
 * 由父级刷新 Inbox（组件不感知 Inbox 状态）。
 */
import { reactive, ref, watch } from 'vue'
import { getStudentProfile, saveStudentProfile } from '../lib/backends'

const props = defineProps({
  open: { type: Boolean, default: false },
  apiSettings: { type: Object, required: true }
})

const emit = defineEmits(['close', 'saved'])

const busy = ref(false)
const profileMsg = ref('')
const profileInterests = ['保研', '奖学金', '竞赛', '就业', '考研', '出国']
const studentProfile = reactive({ college: '', major: '', grade: '', education: '', interests: [] })

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    profileMsg.value = ''
    try {
      const profile = await getStudentProfile(props.apiSettings)
      Object.assign(studentProfile, { college: '', major: '', grade: '', education: '', interests: [], ...profile })
    } catch (error) {
      profileMsg.value = error?.detail || error?.message || '画像读取失败'
    }
  }
)

async function saveProfile() {
  busy.value = true
  profileMsg.value = ''
  try {
    await saveStudentProfile(props.apiSettings, studentProfile)
    profileMsg.value = '已保存，接下来会按这些条件筛选公开通知。'
    emit('saved', Boolean(studentProfile.education || studentProfile.interests.length))
  } catch (error) {
    profileMsg.value = error?.detail || error?.message || '保存失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="open" class="modal-mask" @click.self="emit('close')">
    <div class="modal profile-modal">
      <div class="modal-head"><h2>我的通知筛选条件</h2><button class="modal-close" @click="emit('close')">✕</button></div>
      <p class="schedule-tip">这些信息只用于在本地筛选公开通知，不会发送给校园网站。</p>
      <div class="profile-form">
        <label><span>学院</span><input v-model.trim="studentProfile.college" placeholder="如：计算机科学与技术学院" /></label>
        <label><span>专业</span><input v-model.trim="studentProfile.major" placeholder="如：软件工程" /></label>
        <label><span>年级 / 届别</span><input v-model.trim="studentProfile.grade" placeholder="如：2027届" /></label>
        <label><span>学历层次</span><select v-model="studentProfile.education"><option value="">暂不填写</option><option value="本科生">本科生</option><option value="研究生">研究生</option></select></label>
        <fieldset><legend>关注方向</legend><label v-for="interest in profileInterests" :key="interest" class="interest-option"><input v-model="studentProfile.interests" type="checkbox" :value="interest" />{{ interest }}</label></fieldset>
        <p v-if="profileMsg" class="kb-status">{{ profileMsg }}</p><button @click="saveProfile" :disabled="busy">保存筛选条件</button>
      </div>
    </div>
  </div>
</template>
