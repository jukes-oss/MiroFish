<template>
  <div class="studio">
    <header class="studio-bar">
      <a href="/">推文预演</a>
      <a :href="`/tweet/runs/${runId}`">返回运行</a>
    </header>
    <main class="studio-main">
      <h1>报告</h1>
      <p v-if="waitingText" class="notice" role="status">{{ waitingText }}</p>
      <p v-if="errorText" class="alert" role="alert">{{ errorText }}</p>
      <TweetReportDocument
        v-if="loaded"
        :run="run"
        :report="report"
        :allow-delete="true"
        @delete="remove"
      />
    </main>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import TweetReportDocument from '../components/TweetReportDocument.vue'
import { deleteRun, fetchReport, fetchRun } from '../api/tweet'
import { isActiveStatus } from '../tweet/present'

const route = useRoute()
const router = useRouter()
const runId = route.params.runId
const run = ref(null)
const report = ref(null)
const errorText = ref('')
const waitingText = ref('')
const loaded = ref(false)
let timer = 0

function stopPolling() {
  if (timer) {
    window.clearInterval(timer)
    timer = 0
  }
}

async function load() {
  try {
    const [runBody, reportBody] = await Promise.all([
      fetchRun(runId),
      fetchReport(runId),
    ])
    run.value = runBody.run
    report.value = reportBody.report
    const stillRunning = !reportBody.report && isActiveStatus(runBody.run?.status)
    if (stillRunning) {
      errorText.value = ''
      waitingText.value = '报告还在生成。'
      if (!timer) timer = window.setInterval(load, 1000)
    } else {
      stopPolling()
      waitingText.value = ''
      errorText.value = reportBody.message && !reportBody.report ? reportBody.message : ''
    }
    loaded.value = true
  } catch (error) {
    stopPolling()
    waitingText.value = ''
    errorText.value = error.message || '找不到报告'
    loaded.value = true
  }
}

async function remove() {
  try {
    await deleteRun(runId)
    await router.push('/')
  } catch (error) {
    errorText.value = error.message || '删除没有完成'
  }
}

onMounted(load)
onUnmounted(stopPolling)
</script>

<style scoped src="../tweet/studio.css"></style>
