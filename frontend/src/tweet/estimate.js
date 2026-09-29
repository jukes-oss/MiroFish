/** Client-side estimate. The server still clamps and enforces the same caps. */

export const AGENT_DEFAULT = 120
export const AGENT_MAX = 240
export const ROUND_DEFAULT = 3
export const ROUND_MAX = 4
export const CALL_LIMIT = 10
export const WALL_LIMIT_SECONDS = 600
export const DRAFT_MAX = 2000
export const AUTHOR_MAX = 500

export function clampCount(value, fallback, low, high) {
  const number = Number.parseInt(value, 10)
  if (!Number.isFinite(number)) return fallback
  return Math.min(high, Math.max(low, number))
}

export function estimateRun({ agentCount, roundCount, profile }) {
  const agents = clampCount(agentCount, AGENT_DEFAULT, 1, AGENT_MAX)
  const rounds = clampCount(roundCount, ROUND_DEFAULT, 1, ROUND_MAX)
  const selected = profile === 'subscription-only' ? 'subscription-only' : 'mixed'
  const expectedCalls = 1 + rounds + 1
  return {
    agents,
    rounds,
    profile: selected,
    expectedCalls,
    callLimit: CALL_LIMIT,
    wallLimitSeconds: WALL_LIMIT_SECONDS,
    channels: selected === 'subscription-only'
      ? [
          { role: '人设、反应、报告', detail: '所选订阅通道' },
        ]
      : [
          { role: '人设、报告', detail: '所选订阅通道' },
          { role: '每波反应', detail: 'ollama · qwen3.8:27b-mxfp8' },
        ],
  }
}
