# 评估与回归契约 v1

## 当前交付状态

本次只审查计划，没有用户授权的历史草稿、固定时间窗口的真实互动快照或粉丝基线。**真实固定集尚未建立，真实反响与预测效果未核实**。`examples/` 全部是合成契约样例。不能把网上印象、模型记忆或虚构计数当作已知真实结果。以下是必须实施的数据集与发布门禁，不是已通过的实验报告。

## 固定历史集

初版收集 30 条真实已发布推文：从用户自有或获授权的中文 X 账号导出/手工录入；禁止把任意人的整条时间线抓取作为默认动作。样本涵盖技术/产品、币圈/商业、海外生活、社会议题、幽默及低反应对照；简体、繁体、中英夹杂至少各 5 条；低中高互动各至少 8 条，已知反噬至少 8 条。分层可重叠，找不到足够数据时报告缺口，不能编造补齐。

先按作者及时间聚类，再冻结 `dev=20 / holdout=10`；近重复、同一线程、同一事件的变体不得跨集合。尽量包含低曝光与零回复样本，避免只收翻车和爆款。公开广为人知的推文可能已进入模型训练，须标注并单独报告；优先使用授权且较近期的普通账号历史。

每条记录（JSONL，真实标签仅放独立 labels 文件）：

```json
{
  "case_id": "real_001",
  "source_kind": "owner_export",
  "source_url": null,
  "source_snapshot_sha256": "由采集程序填写",
  "permission_note": "仅用于私有评估，是否允许再分发另行记录",
  "published_at": "ISO-8601",
  "observation_window_hours": 48,
  "collected_at": "ISO-8601",
  "input": {
    "draft_text": "发布时原文",
    "author_context_before_publish": "发布前可知背景",
    "audience_version": "zh_x_v1"
  },
  "labels": {
    "impressions": null,
    "likes": null,
    "replies": null,
    "reposts": null,
    "quotes": null,
    "followers_at_publish": null,
    "author_baseline_post_ids": [],
    "engagement_tier": null,
    "backlash_level": null,
    "trigger_spans": [],
    "top_reply_snapshots": [],
    "annotator_ids": [],
    "adjudication_note": null
  },
  "quality_flags": ["待真实资料填充，不能参加评分"],
  "split": "dev"
}
```

上面的 null 表示缺资料而非零。真实集冻结时必须用数据 schema 校验：URL 可空但本地证据快照哈希、采集时间、原文和已知标签来源不可空；占位内容必须拒绝。原始截图/导出放私有目录，代码仓库只放许可允许的内容与 manifest。去掉无关账号名/隐私，不改触发句；改过原文的样本不得作为同一历史推文评分。

固定 48 小时窗口用于后续可采集数据；已有累计快照若无法还原 48 小时，单独成组，不能假装窗口一致。曝光计数公开不可得时：互动率指标记 NA，改用作者历史同窗口互动中位数归一化的辅助量，并标明不可直接比较。不能用缺失浏览量填 0。

真实互动标签：优先 `unique_engagers/impressions`（若所有者有此数据）；只有公开计数时用 `(likes+replies+reposts+quotes)/impressions`，标为事件率而非独立用户率。dev 集分位数确定 low/medium/high，冻结后应用于 holdout；模拟率不能直接当作现实率，拟合单调映射只用 dev，并与不使用文本的先验基线比较。若无法拟合或作者基线不足，只评风险、触发句与改写，不输出“已校准互动预测”。

风险由两位独立标注者根据真实回复/引用及语境标注：low=无持续负面解读证据；medium=局部明确负面解读；high=多个独立群体的负面解读扩散，有来源支撑。记录分歧并裁决，不能仅根据总评论数判翻车。真实高赞回复保留观察时点赞、采集时间和语义主题；不要求模拟逐字复现真人。

## 三层评估

1. **确定性契约层（每次 PR，零付费）**：用 mock 覆盖全 none、零回复、少数派、全体一致、emoji/繁体原文跨度、提示注入、伪造 ID、quote 混入回复、输入过长、取消、429、超时未知收费、预算并发争抢、重启恢复、缺失 usage、修复再失败。schema/证据/统计/费用不变量通过率必须 100%。不允许用删除失败测试达标。
2. **模型稳定性层**：同输入、同 seed、同人设、同模型快照、同配额各跑 `N=5`，关闭动作和报告结果缓存。档位和风险分别以众数占比计算一致率 `max_count/N`，最低 0.8；low/medium/high 编码 0/1/2，样本方差阈值分别 ≤0.3；不得出现同一样本 low↔high 摆动。`insufficient_data` 单独统计，不作为一致率的成功匹配。至少 80% 可判定样本达标，覆盖率 ≥90%，否则降级为实验功能。另跑 `N=10` 不同 seed 衡量人群抽样变动，报告分布、方差与区间，不强迫达到相同 seed 门槛。
3. **历史对照层**：只用发布前信息做推理；labels 不进入 prompt、检索库或 persona 缓存。报告互动档 macro-F1、ordinal MAE、Spearman 相关（可用时）、风险 macro-F1/high 召回、触发句字符区间 F1/IoU、回复主题 top-3 命中率、人审改写保意与新事实率、token/费用/耗时 p50/p95。样本少时同时报分子/分母、混淆矩阵和 bootstrap 区间，不只给百分比。

## 初版门禁（目标，不是已达成的承诺）

- 候选抽样用 10,000 次规则曝光验证各动作比例偏差 ≤1 个百分点；这是抽样器测试，不要求模型最终执行比例等于先验。活动乘数归一化另测。普通无争议集最终 none 期望 ≥85%，reply+quote 期望 ≤5%；超出仅先诊断，不自动删发言。
- 每次真实完成报告证据有效率 100%，schema 100%；降级报告率 ≤5%，费用闸门不变量 100%；缺 usage 计保守预留，不算免费。
- 同 seed 稳定性采用上一节门槛。跨 seed 方差过大时向用户显示不稳定；不挑选最符合预期的一次。
- dev 调优参考：互动 macro-F1 ≥0.50、ordinal MAE ≤0.60；风险 high 召回 ≥0.80，且 macro-F1 不低于多数类/规则基线；触发跨度 F1 ≥0.60；改写保意人审 ≥90%，编造新事实率为 0。小样本 high 不足 5 个时只报计数，不宣称召回达标。
- holdout 最少 10 条只作先导验证；不能支撑统计显著或真实预测宣传。正式宣传预测性能需扩大独立集，预注册成功标准。未胜过先验/多数类基线时保留“预演备忘”定位。

## 回归流程与产物

每次变更 prompt、模型快照、schema、配额、抽样器、费用表、tokenizer、本地抽取或检索参数时，先跑确定性层，再在单独评估总预算内跑 dev 的固定 seed 对照。报告 `git_sha, dataset_hash, prompt_hash, schema_version, audience_version, seed, provider/model, capability_version, price_version, cache_policy`。候选方案先过证据、费用硬门禁，再看质量；宏 F1 下降 >0.05、ordinal MAE 增加 >0.10、high 召回下降 >0.10 或费用 p95 增加 >20% 必须说明并停止自动晋级。小集出现一条严重证据捏造也阻断。

holdout 只在候选版本冻结后跑一次，失败后回 dev 开新版本；不能无限看 holdout 调 prompt。评估计划预估费用 `样本数 × N × 单次上界`，超总预算就停止，不能把评估脚本变成旁路。拟新增 CLI：`python -m eval.run --suite contract|dev|holdout --manifest ... --budget-usd ...`；**该命令是待实现接口，本交付不声称已可运行**。
