/** Replayable report states. These are local examples, not a model run. */

const usage = (physical, reserved, uncertain, channels) => ({
  execution_profile: 'mixed',
  request_limit: 10,
  physical_requests: physical,
  reserved_requests: reserved,
  uncertain_requests: uncertain,
  wall_limit_seconds: 600,
  wall_elapsed_ms: 1800,
  channels,
})

const baseRun = (overrides) => ({
  run_id: 'example',
  status: 'complete',
  draft_text: '所有人都该用这段草稿。',
  author_context: '',
  audience_version: 'zh_x_v1',
  agent_count: 120,
  round_count: 3,
  execution_profile: 'mixed',
  subscription_cli: 'grok',
  schema_version: '2.0',
  error_code: null,
  error_message: null,
  cancel_requested: false,
  ...overrides,
})

function shell(status, metrics, extra = {}) {
  return {
    schema_version: '2.0',
    run_id: extra.run_id || 'example',
    status,
    scope: {
      kind: 'simulation_only',
      audience_version: 'zh_x_v1',
      behavior_prior_version: 'silent_v1',
      seed: 1,
      planned_rounds: 3,
      completed_rounds: extra.completed_rounds ?? 3,
      calibrated: false,
      evidence_truncated: Boolean(extra.truncated),
    },
    metrics,
    usage: extra.usage,
    engagement: {
      tier: extra.tier || 'low',
      tier_reason: extra.tier_reason || '只描述这一次模拟样本。',
      rate_interval: { lower: 0, upper: 0 },
      meaning: '仅描述当前模拟样本的互动，不预测真实浏览量或点赞数。',
    },
    trigger_lines: extra.trigger_lines || [],
    top_replies: extra.top_replies || [],
    backlash_risk: {
      level: extra.risk || 'low',
      rule_version: 'risk_rules_v1',
      issues: extra.issues || [],
      limitations: ['没有观察到负面发言，不代表真实发布没有风险。'],
    },
    disagreement: {
      views: extra.views || [],
      silent_agents_are_not_supporters: true,
      note: '沉默账号不是支持者。未校准结果是模拟备忘。',
    },
    rewrites: extra.rewrites || [],
    confidence: {
      level: 'low',
      calibration_status: 'uncalibrated',
      basis: '未校准的模拟备忘。',
      uncertainties: ['受众比例是工程假设。', '真实环境未知。', '样本不能代表总体。'],
    },
    limitations: ['未校准结果标记为模拟备忘。'],
    degradation_reasons: extra.reasons || [],
  }
}

const grok = {
  channel: 'grok_cli',
  model_id: 'grok-4.7',
  model_id_status: 'configured_only',
  roles: ['persona', 'report'],
  requests: 2,
  elapsed_ms: 400,
}

const ollama = {
  channel: 'ollama',
  model_id: 'qwen3.8:27b-mxfp8',
  model_id_status: 'configured_only',
  roles: ['agent'],
  requests: 3,
  elapsed_ms: 1400,
}

const counts = (none, like, reply, repost, quote) => ({ none, like, reply, repost, quote })

export const REPLAYS = {
  complete: {
    title: '完成',
    run: baseRun({ run_id: 'example-complete', status: 'complete' }),
    report: shell('complete', {
      planned_agents: 120,
      exposed_agents: 120,
      evaluated_agents: 120,
      missing_agents: 0,
      action_counts: counts(120, 0, 0, 0, 0),
      root_engaged_agents: 0,
      none_rate: 1,
      engagement_rate: 0,
    }, {
      run_id: 'example-complete',
      usage: usage(5, 1, 0, [grok, ollama]),
      rewrites: [
        {
          variant: 'preserve_claim',
          text: '我想先自己试试这段写法。',
          what_changed: '保留原意，改成个人说法。',
          changed_spans: [{ start: 0, end: 3, text: '所有人' }],
          expected_effect: { hypothesis: '口气更弱。', tradeoff: '号召变少。', simulation_verified: false },
        },
        {
          variant: 'add_boundaries',
          text: '在一些草稿里可以试试这段写法。',
          what_changed: '把适用范围收窄。',
          changed_spans: [{ start: 0, end: 3, text: '所有人' }],
          expected_effect: { hypothesis: '范围更清楚。', tradeoff: '不再绝对。', simulation_verified: false },
        },
      ],
    }),
  },
  degraded: {
    title: '降级',
    run: baseRun({
      run_id: 'example-degraded',
      status: 'degraded',
      error_code: 'loop_degraded',
      error_message: '模拟循环已结束。未校准结果是模拟备忘。',
      draft_text: '<img src=x onerror=alert(1)>不应变成图片',
      author_context: '<script>alert(1)</script>作者背景只是文本',
    }),
    report: shell('degraded', {
      planned_agents: 12,
      exposed_agents: 8,
      evaluated_agents: 6,
      missing_agents: 2,
      action_counts: counts(5, 1, 0, 0, 0),
      root_engaged_agents: 1,
      none_rate: 0.625,
      engagement_rate: 0.125,
    }, {
      run_id: 'example-degraded',
      completed_rounds: 2,
      truncated: true,
      usage: usage(4, 0, 1, [grok, { ...ollama, requests: 2, elapsed_ms: 900 }]),
      reasons: ['有账号的反应缺失，没有补记为划走。', '有波次没有开始，没有补写曝光。'],
      rewrites: [
        {
          variant: 'change_style',
          text: '<b>这行标签必须按纯文本显示</b>',
          what_changed: '只改语气。',
          changed_spans: [{ start: 0, end: 1, text: '<' }],
          expected_effect: { hypothesis: '无。', tradeoff: '无。', simulation_verified: false },
        },
      ],
    }),
  },
  limited: {
    title: '限流',
    run: baseRun({
      run_id: 'example-limited',
      status: 'failed',
      error_code: 'day_cap',
      error_message: '本机今日共用调用次数已达到上限 50，不能再发送请求。',
      agent_count: 4,
      round_count: 1,
    }),
    report: shell('failed', {
      planned_agents: 4,
      exposed_agents: 0,
      evaluated_agents: 0,
      missing_agents: 0,
      action_counts: counts(0, 0, 0, 0, 0),
      root_engaged_agents: 0,
      none_rate: null,
      engagement_rate: null,
    }, {
      run_id: 'example-limited',
      completed_rounds: 0,
      truncated: true,
      tier: 'insufficient_data',
      risk: 'insufficient_data',
      tier_reason: '没有有效曝光，不能划分互动档位。',
      usage: usage(10, 0, 0, [{ ...grok, requests: 10, elapsed_ms: 2000, roles: ['persona'] }]),
      reasons: ['已到达每日调用上限，没有继续发送。'],
    }),
  },
  missing: {
    title: '缺失',
    run: baseRun({
      run_id: 'example-missing',
      status: 'degraded',
      error_message: '有账号的反应缺失，没有补记为划走。',
    }),
    report: shell('degraded', {
      planned_agents: 4,
      exposed_agents: 4,
      evaluated_agents: 3,
      missing_agents: 1,
      action_counts: counts(2, 0, 1, 0, 0),
      root_engaged_agents: 1,
      none_rate: 0.5,
      engagement_rate: 0.25,
    }, {
      run_id: 'example-missing',
      usage: usage(3, 1, 0, [grok, { ...ollama, requests: 1 }]),
      reasons: ['有账号的反应缺失，没有补记为划走。'],
      top_replies: [
        {
          action_id: 'act-example-r1-a001',
          agent_id: 'a001',
          text: '这是一条模拟回复，只在样本里排序。',
          group_ids: ['tech'],
          simulated_likes: 1,
          shown_to: 2,
          rank_basis: 'simulated_like_rate',
          why_it_might_resonate: '只在当前模拟样本里出现。',
          limitation: '不是真实点赞。',
        },
      ],
      trigger_lines: [
        {
          span: { start: 0, end: 3, text: '所有人' },
          triggered_groups: ['tech'],
          reaction_types: ['skepticism'],
          explanation: '样本里有人针对这个片段开口。',
          evidence_ids: ['act-example-r1-a001'],
        },
      ],
    }),
  },
  cancelled: {
    title: '已取消',
    run: baseRun({
      run_id: 'example-cancelled',
      status: 'cancelled',
      cancel_requested: true,
      error_code: 'cancelled',
      error_message: '已取消，未发出新的模型请求。',
    }),
    report: shell('degraded', {
      planned_agents: 120,
      exposed_agents: 0,
      evaluated_agents: 0,
      missing_agents: 0,
      action_counts: counts(0, 0, 0, 0, 0),
      root_engaged_agents: 0,
      none_rate: null,
      engagement_rate: null,
    }, {
      run_id: 'example-cancelled',
      completed_rounds: 0,
      truncated: true,
      tier: 'insufficient_data',
      risk: 'insufficient_data',
      usage: usage(0, 0, 0, []),
      reasons: ['已取消，未发出新的模型请求。'],
    }),
  },
}
