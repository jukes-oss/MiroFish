<template>
  <div class="studio">
    <header class="studio-bar">
      <a href="/">推文预演</a>
      <span>示例</span>
    </header>
    <main class="studio-main">
      <h1>{{ item?.title || '没有这个示例' }}</h1>
      <p class="lede">这是本地示例回放，没有调用模型。它不是一次预演结果。</p>
      <TweetReportDocument
        v-if="item"
        :run="item.run"
        :report="item.report"
        :allow-delete="false"
      />
      <p v-else class="alert">找不到这个示例。</p>
    </main>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import TweetReportDocument from '../components/TweetReportDocument.vue'
import { REPLAYS } from '../tweet/fixtures'

const route = useRoute()
const item = computed(() => REPLAYS[route.params.state] || null)
</script>

<style scoped src="../tweet/studio.css"></style>
