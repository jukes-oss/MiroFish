<template>
  <article class="report-sheet">
    <p class="memo">模拟备忘</p>
    <p class="hint">未校准结果只描述这次模拟样本，不把互动写成对真实平台的判断。</p>

    <h2>用量与状态</h2>
    <dl class="facts">
      <dt>调用次数</dt>
      <dd>{{ callLine }}</dd>
      <dt>墙钟</dt>
      <dd>{{ wallLine }}</dd>
      <dt>通道与模型</dt>
      <dd>
        <p v-if="channels.length === 0" class="plain">无</p>
        <p v-for="item in channels" :key="item.key" class="plain">{{ item.text }}</p>
      </dd>
      <dt>限流或失败原因</dt>
      <dd class="plain">{{ failure }}</dd>
      <dt>缺失</dt>
      <dd class="plain">{{ missingLine }}</dd>
    </dl>

    <h2>草稿</h2>
    <p class="plain draft">{{ run?.draft_text || '无' }}</p>
    <h2>作者背景</h2>
    <p class="plain">{{ run?.author_context || '无' }}</p>

    <template v-if="report">
      <h2>互动档位</h2>
      <p class="plain">{{ tierLine }}</p>
      <p class="plain">{{ report.engagement?.meaning }}</p>
      <h2>改写</h2>
      <p v-if="!rewrites.length" class="plain">没有改写。降级结果不会为了凑数编造句子。</p>
      <article v-for="(item, index) in rewrites" :key="index">
        <p class="plain">{{ item.text }}</p>
        <p class="plain">{{ item.what_changed }}</p>
      </article>
    </template>
    <p v-else class="notice">报告正文还没有生成。</p>

    <div class="actions">
      <button type="button" class="secondary" @click="evidenceOpen = !evidenceOpen">
        {{ evidenceOpen ? '收起证据' : '查看证据' }}
      </button>
      <button type="button" class="secondary" @click="exportDocument">导出</button>
      <button v-if="allowDelete" type="button" class="secondary" @click="askDelete = true">删除</button>
    </div>
    <p v-if="exported" class="notice" role="status">已导出报告文件。</p>
    <div v-if="askDelete" class="alert" role="alert">
      <p>删除后，这次运行和报告都会从本机记录里去掉。</p>
      <div class="actions">
        <button type="button" @click="$emit('delete')">确认删除</button>
        <button type="button" class="secondary" @click="askDelete = false">留下</button>
      </div>
    </div>

    <section v-if="evidenceOpen" class="evidence" aria-label="证据">
      <h2>证据</h2>
      <p v-if="!hasEvidence" class="plain">这次没有可展示的证据条目。</p>
      <article v-for="item in topReplies" :key="item.action_id">
        <p class="plain">{{ item.text }}</p>
        <p class="plain">
          编号 {{ item.action_id }} · 圈层 {{ (item.group_ids || []).join('、') }} ·
          模拟赞 {{ item.simulated_likes }} · 后续曝光 {{ item.shown_to }} ·
          {{ rankLabel(item.rank_basis) }}
        </p>
        <p class="plain">{{ item.limitation }}</p>
      </article>
      <article v-for="(item, index) in triggerLines" :key="`t-${index}`">
        <p class="plain">触发片段：{{ item.span?.text }}</p>
        <p class="plain">{{ item.explanation }}</p>
        <p class="plain">证据编号 {{ (item.evidence_ids || []).join('、') }}</p>
      </article>
      <article v-for="(item, index) in issues" :key="`i-${index}`">
        <p class="plain">{{ item.reason }}</p>
        <p class="plain">证据编号 {{ (item.evidence_ids || []).join('、') }}</p>
      </article>
    </section>
  </article>
</template>

<script setup>
import { computed, ref } from 'vue'
import { failureText, formatWall, TIER_LABELS, RANK_LABELS } from '../tweet/present'

const props = defineProps({
  run: { type: Object, default: null },
  report: { type: Object, default: null },
  allowDelete: { type: Boolean, default: false },
})

defineEmits(['delete'])

const evidenceOpen = ref(false)
const exported = ref(false)
const askDelete = ref(false)

const usage = computed(() => props.report?.usage || null)
const channels = computed(() => {
  const rows = usage.value?.channels
  if (!Array.isArray(rows) || rows.length === 0) return []
  return rows.map((item, index) => {
    const model = item.model_id || '未知'
    const roles = Array.isArray(item.roles)
      ? item.roles.map((role) => ({
          persona: '人设',
          agent: '反应',
          report: '报告',
          ontology: '本体',
          extractor: '抽取',
          config: '配置',
          interview: '访谈',
        }[role] || role)).join('、')
      : ''
    return {
      key: `${item.channel}-${index}`,
      text: `${item.channel} · ${model} · 角色 ${roles} · ${item.requests} 次 · ${formatWall(item.elapsed_ms)}`,
    }
  })
})
const callLine = computed(() => {
  if (!usage.value) return '暂无用量记录'
  return `已发出 ${usage.value.physical_requests} 次，预留 ${usage.value.reserved_requests} 次，未知 ${usage.value.uncertain_requests} 次，上限 ${usage.value.request_limit} 次`
})
const wallLine = computed(() => {
  if (!usage.value) return '暂无'
  return `已用 ${formatWall(usage.value.wall_elapsed_ms)}，上限 ${usage.value.wall_limit_seconds} 秒`
})
const failure = computed(() => failureText(props.run, props.report))
const missingLine = computed(() => {
  const missing = props.report?.metrics?.missing_agents
  if (missing === null || missing === undefined) return '暂无'
  return `${missing} 个账号没有落成动作，没有补记为划走`
})
const tierLine = computed(() => {
  const tier = props.report?.engagement?.tier
  const label = TIER_LABELS[tier] || '未知'
  return `${label}。${props.report?.engagement?.tier_reason || ''}`
})
const rewrites = computed(() => props.report?.rewrites || [])
const topReplies = computed(() => props.report?.top_replies || [])
const triggerLines = computed(() => props.report?.trigger_lines || [])
const issues = computed(() => props.report?.backlash_risk?.issues || [])
const hasEvidence = computed(() => topReplies.value.length + triggerLines.value.length + issues.value.length > 0)

function rankLabel(value) {
  return RANK_LABELS[value] || value || '无'
}

function exportDocument() {
  const payload = {
    run: props.run,
    report: props.report,
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  const name = props.run?.run_id || 'tweet-report'
  link.href = url
  link.download = `tweet-report-${name}.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
  exported.value = true
}
</script>

<style scoped src="../tweet/studio.css"></style>
<style scoped>
.report-sheet {
  min-width: 0;
  max-width: 100%;
}
h2 {
  font-size: 1rem;
  margin: 18px 0 8px;
}
</style>
