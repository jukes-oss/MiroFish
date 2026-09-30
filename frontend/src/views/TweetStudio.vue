<template>
  <div class="studio">
    <header class="studio-bar">
      <a href="/">推文预演</a>
      <span>本地样本</span>
    </header>
    <main class="studio-main">
      <h1>粘贴草稿，先看预估，再开始</h1>
      <p class="lede">结果是模拟备忘。打开这个页面不需要模型通道。</p>

      <div class="layout">
        <section class="card">
          <h2>草稿</h2>
          <label for="draft">推文草稿</label>
          <textarea id="draft" v-model="draft" rows="6" placeholder="粘贴要预演的草稿"></textarea>
          <p class="hint">{{ draft.length }} / {{ draftMax }} 个码点</p>
          <label for="author">作者背景</label>
          <textarea id="author" v-model="author" rows="3" placeholder="可选"></textarea>
          <p class="hint">{{ author.length }} / {{ authorMax }} 个码点</p>
          <div class="row">
            <div>
              <label for="agents">人数</label>
              <input id="agents" v-model="agentCount" type="number" min="1" max="240" />
            </div>
            <div>
              <label for="rounds">波次</label>
              <input id="rounds" v-model="roundCount" type="number" min="1" max="4" />
            </div>
          </div>
          <label for="profile">运行配置</label>
          <select id="profile" v-model="profile">
            <option value="mixed">混合</option>
            <option value="subscription-only">仅订阅</option>
          </select>
          <label for="audience">受众模板</label>
          <select id="audience" v-model="audience">
            <option value="zh_x_v1">zh_x_v1</option>
          </select>
          <div class="actions">
            <button type="button" :disabled="submitting" @click="startRun">开始预演</button>
          </div>
          <p v-if="errorText" class="alert" role="alert">{{ errorText }}</p>
        </section>

        <section class="card" aria-label="预估">
          <h2>预估</h2>
          <dl class="facts">
            <dt>人数</dt>
            <dd>{{ estimate.agents }}</dd>
            <dt>波次</dt>
            <dd>{{ estimate.rounds }}</dd>
            <dt>预计调用</dt>
            <dd>人设这次不启动，波次也不会开始。报告 1 次。上限 {{ estimate.callLimit }} 次。</dd>
            <dt>墙钟上限</dt>
            <dd>{{ estimate.wallLimitSeconds }} 秒</dd>
            <dt>通道</dt>
            <dd>
              <p v-for="item in estimate.channels" :key="item.role" class="plain">{{ item.role }}：{{ item.detail }}</p>
            </dd>
          </dl>
          <p class="hint">配额是虚构假设。真正发出的次数以运行后的用量为准。</p>
        </section>
      </div>

      <section class="card" style="margin-top: 16px">
        <h2>通用模式</h2>
        <p class="hint">本地图谱还没有完成。这个入口不会改去云端建图。</p>
        <button type="button" class="secondary" @click="showGeneric">查看通用模式</button>
        <p v-if="genericVisible" class="notice" role="status">{{ genericReason }}</p>
      </section>

      <section class="card" style="margin-top: 16px">
        <h2>运行记录</h2>
        <p v-if="historyError" class="alert">{{ historyError }}</p>
        <p v-else-if="runs.length === 0" class="hint">还没有运行。</p>
        <ul v-else class="history">
          <li v-for="run in runs" :key="run.run_id">
            <span>{{ statusLabel(run.status) }}</span>
            <span class="plain">{{ run.draft_preview }}</span>
            <a class="button-link secondary" :href="`/tweet/runs/${run.run_id}`">打开</a>
            <a class="button-link secondary" :href="`/tweet/runs/${run.run_id}/report`">报告</a>
            <button
              v-if="isActiveStatus(run.status)"
              type="button"
              class="secondary"
              @click="cancelFromList(run.run_id)"
            >取消</button>
          </li>
        </ul>
      </section>

      <section class="card" style="margin-top: 16px">
        <h2>示例状态</h2>
        <p class="hint">这些页面用本地示例回放，不调用模型，也不能当成一次真实预演。</p>
        <div class="actions">
          <a v-for="(item, key) in replays" :key="key" class="button-link secondary" :href="`/tweet/replay/${key}`">{{ item.title }}</a>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { createRun, cancelRun, fetchHealth, listRuns } from '../api/tweet'
import { AUTHOR_MAX, DRAFT_MAX, estimateRun } from '../tweet/estimate'
import { REPLAYS } from '../tweet/fixtures'
import { isActiveStatus, statusLabel } from '../tweet/present'

const router = useRouter()
const draft = ref('')
const author = ref('')
const agentCount = ref(120)
const roundCount = ref(3)
const profile = ref('mixed')
const audience = ref('zh_x_v1')
const submitting = ref(false)
const errorText = ref('')
const runs = ref([])
const historyError = ref('')
const genericVisible = ref(false)
const genericReason = ref('通用本地图谱尚未完成，此模式不可用，不会改用云端图谱。')
const draftMax = DRAFT_MAX
const authorMax = AUTHOR_MAX
const replays = REPLAYS
let timer = 0

const estimate = computed(() => estimateRun({
  agentCount: agentCount.value,
  roundCount: roundCount.value,
  profile: profile.value,
}))

async function refresh() {
  try {
    const data = await listRuns()
    runs.value = data.runs || []
    historyError.value = ''
  } catch (error) {
    historyError.value = error.message || '暂时读不到历史，页面仍然可以打开。'
  }
}

async function startRun() {
  errorText.value = ''
  if (!draft.value.trim()) {
    errorText.value = '请先粘贴草稿。'
    return
  }
  if (draft.value.length > DRAFT_MAX) {
    errorText.value = '草稿不能超过 2000 个码点。'
    return
  }
  if (author.value.length > AUTHOR_MAX) {
    errorText.value = '作者背景不能超过 500 个码点。'
    return
  }
  submitting.value = true
  try {
    const created = await createRun({
      idempotency_key: `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      draft_text: draft.value,
      author_context: author.value,
      audience_version: audience.value,
      agent_count: estimate.value.agents,
      round_count: estimate.value.rounds,
      execution_profile: estimate.value.profile,
    })
    await router.push(`/tweet/runs/${created.run_id}`)
  } catch (error) {
    errorText.value = error.message || '无法开始运行'
  } finally {
    submitting.value = false
  }
}

async function cancelFromList(runId) {
  errorText.value = ''
  try {
    await cancelRun(runId)
    await refresh()
  } catch (error) {
    errorText.value = error.message || '取消没有完成'
  }
}

function showGeneric() {
  genericVisible.value = true
}

onMounted(async () => {
  try {
    const health = await fetchHealth()
    if (health.generic_mode?.reason) genericReason.value = health.generic_mode.reason
  } catch {
    genericReason.value = '通用本地图谱尚未完成，此模式不可用，不会改用云端图谱。'
  }
  await refresh()
  timer = window.setInterval(refresh, 2000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped src="../tweet/studio.css"></style>
