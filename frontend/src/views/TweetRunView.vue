<template>
  <div class="studio">
    <header class="studio-bar">
      <a href="/">推文预演</a>
      <span>{{ statusLabel(run?.status) }}</span>
    </header>
    <main class="studio-main">
      <h1>运行</h1>
      <p v-if="errorText" class="alert" role="alert">{{ errorText }}</p>
      <section v-if="run" class="card">
        <dl class="facts">
          <dt>状态</dt>
          <dd>{{ statusLabel(run.status) }}</dd>
          <dt>人数</dt>
          <dd>{{ run.agent_count }}</dd>
          <dt>波次</dt>
          <dd>{{ run.round_count }}</dd>
          <dt>配置</dt>
          <dd>{{ run.execution_profile === 'subscription-only' ? '仅订阅' : '混合' }}</dd>
          <dt>限流或失败原因</dt>
          <dd class="plain">{{ run.error_message || '无' }}</dd>
        </dl>
        <h2>草稿</h2>
        <p class="plain">{{ run.draft_text }}</p>
        <div class="actions">
          <button v-if="isActiveStatus(run.status)" type="button" :disabled="cancelling" @click="cancel">取消</button>
          <a class="button-link secondary" :href="`/tweet/runs/${run.run_id}/report`">打开报告</a>
          <a class="button-link secondary" href="/">返回</a>
        </div>
        <p class="hint">用量、缺失和模拟备忘写在报告页。运行结束后可以打开。</p>
      </section>
    </main>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { cancelRun, fetchRun } from '../api/tweet'
import { isActiveStatus, statusLabel } from '../tweet/present'

const route = useRoute()
const run = ref(null)
const errorText = ref('')
const cancelling = ref(false)
let timer = 0

async function refresh() {
  try {
    const data = await fetchRun(route.params.runId)
    run.value = data.run
    errorText.value = ''
    if (!isActiveStatus(data.run?.status) && timer) {
      window.clearInterval(timer)
      timer = 0
    }
  } catch (error) {
    errorText.value = error.message || '找不到这次运行'
  }
}

async function cancel() {
  cancelling.value = true
  try {
    const data = await cancelRun(route.params.runId)
    run.value = data.run
  } catch (error) {
    errorText.value = error.message || '取消没有完成'
  } finally {
    cancelling.value = false
    await refresh()
  }
}

onMounted(async () => {
  await refresh()
  timer = window.setInterval(refresh, 1000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped src="../tweet/studio.css"></style>
<style scoped>
h2 { font-size: 1rem; margin: 16px 0 8px; }
</style>
