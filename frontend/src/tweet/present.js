export const STATUS_LABELS = {
  queued: '排队',
  preparing: '准备',
  running: '运行中',
  reporting: '写报告',
  complete: '完成',
  degraded: '降级',
  failed: '失败',
  cancelled: '已取消',
}

export const TIER_LABELS = {
  low: '低',
  medium: '中',
  high: '高',
  insufficient_data: '数据不足',
}

export const RANK_LABELS = {
  simulated_like_rate: '按模拟点赞率',
  unvalidated_candidate: '尚未验证的候选',
}

const ACTIVE = new Set(['queued', 'preparing', 'running', 'reporting'])

export function statusLabel(status) {
  return STATUS_LABELS[status] || '未知状态'
}

export function isActiveStatus(status) {
  return ACTIVE.has(status)
}

export function formatWall(ms) {
  if (ms === null || ms === undefined || ms === '') return '暂无'
  const value = Number(ms)
  if (!Number.isFinite(value)) return '暂无'
  const seconds = value / 1000
  return `${seconds.toFixed(1)} 秒（${value} 毫秒）`
}

export function failureText(run, report) {
  const parts = []
  if (run?.error_message) parts.push(String(run.error_message))
  const reasons = report?.degradation_reasons
  if (Array.isArray(reasons)) {
    for (const reason of reasons) {
      if (reason && !parts.includes(reason)) parts.push(String(reason))
    }
  }
  return parts.length ? parts.join(' ') : '无'
}
