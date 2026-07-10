// 盘后流水线 5 个面向用户的真实业务阶段配置（与后端 _PHASE_KEYS 严格对齐）。
//
// 该模块刻意不依赖 React/JSX，便于在无 DOM 环境（node --test）下单测：
// - PIPELINE_PHASE_KEYS：5 个阶段 key 固定顺序
// - PIPELINE_PHASE_LABELS：中文标签
// - buildPipelineSteps()：供 AdminAfterClosePipelinePage 渲染时间线
// - findPhaseStartedAt()：从后端 steps 取某阶段 started_at（feature_snapshot 进度 ETA 用）
//
// 历史 8 步骤（refreshing_daily/checking_coverage/creating_dsa/waiting_dsa_worker/
// quality_gate/feature_snapshot/publishing/watchlist_ready）已废弃：
// 内部细状态由后端归并，watchlist_ready 仅为发布门禁(gate)，不作为执行步骤。

import type { PipelinePhaseKey } from '@/api/endpoints'

/** 5 个面向用户的真实业务阶段 key（固定顺序）。 */
export const PIPELINE_PHASE_KEYS: PipelinePhaseKey[] = [
  'market_prep',
  'dsa_compute',
  'quality_gate',
  'feature_snapshot',
  'publishing',
]

/** 阶段 key → 中文标签。 */
export const PIPELINE_PHASE_LABELS: Record<PipelinePhaseKey, string> = {
  market_prep: '行情准备',
  dsa_compute: 'DSA计算',
  quality_gate: '质量校验',
  feature_snapshot: '特征快照',
  publishing: '发布结果',
}

/** 单条时间线步骤定义。 */
export interface PipelineStepDef {
  key: PipelinePhaseKey
  label: string
}

/** 构建时间线步骤列表（顺序与 PIPELINE_PHASE_KEYS 一致）。 */
export function buildPipelineSteps(): PipelineStepDef[] {
  return PIPELINE_PHASE_KEYS.map((key) => ({ key, label: PIPELINE_PHASE_LABELS[key] }))
}

/** 从后端返回的 steps 中查找指定阶段的开始时间（用于 feature_snapshot 进度 ETA）。 */
export function findPhaseStartedAt(
  steps: { step: string; started_at: string | null }[] | undefined,
  key: PipelinePhaseKey,
): string | null {
  if (!steps) return null
  const hit = steps.find((s) => s.step === key)
  return hit?.started_at ?? null
}
