// [StructureSummaryCard] - 描述: 普通用户结构状态人类可读总结卡片
// 无 event_id 时展示最新 structural/temporal 数据的人类可读总结。
// 不显示内部字段名、算法参数、JSON 或商业机密。
// 将 DSA 方向、位置、节点距离等转化为通俗表达。
import type { UseQueryResult } from '@tanstack/react-query'
import type { StructuralFactorResponse, TemporalFeaturesResponse } from '@/api/endpoints'
import type { ResearchContextStyles } from './ResearchContextPanel'

interface StructureSummaryCardProps {
  structuralQuery: UseQueryResult<StructuralFactorResponse, Error>
  temporalQuery: UseQueryResult<TemporalFeaturesResponse, Error>
  styles: ResearchContextStyles
}

// 方向数值/字符串 → 通俗文案
function dirText(dir: number | string | null | undefined): string {
  if (dir === null || dir === undefined) return '-'
  const s = String(dir)
  if (s === '1' || s === 'up' || s === 'bullish') return '上升'
  if (s === '-1' || s === 'down' || s === 'bearish') return '下降'
  return '-'
}

// [0,1] 位置 → 通俗描述
function positionText(pos: number | null | undefined): string {
  if (pos === null || pos === undefined || !isFinite(pos)) return '-'
  const pct = (pos * 100).toFixed(0)
  if (pos > 0.8) return `偏高（${pct}%）`
  if (pos < 0.2) return `偏低（${pct}%）`
  return `中位（${pct}%）`
}

// ATR 距离 → 通俗描述
function atrDistanceText(atr: number | null | undefined): string {
  if (atr === null || atr === undefined || !isFinite(atr)) return '-'
  if (atr > 2) return '较远'
  if (atr > 1) return '适中'
  return '较近'
}

export function StructureSummaryCard({
  structuralQuery,
  temporalQuery,
  styles,
}: StructureSummaryCardProps) {
  const structuralLoading = structuralQuery.isLoading
  const temporalLoading = temporalQuery.isLoading

  if (structuralLoading && temporalLoading) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>结构状态摘要</div>
        <div className={styles.loading}>加载中…</div>
      </div>
    )
  }

  const structural = structuralQuery.data
  const temporal = temporalQuery.data

  if (!structural && !temporal) {
    const hasError = structuralQuery.isError || temporalQuery.isError
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>结构状态摘要</div>
        <div className={hasError ? styles.error : styles.empty}>
          {hasError ? '数据加载失败，请稍后重试。' : '暂无结构状态数据。'}
        </div>
      </div>
    )
  }

  // 降级提示
  const degradedReasons = [
    ...(structural?.meta.degraded_reasons || []),
    ...(temporal?.meta.degraded_reasons || []),
  ]
  const warmupNotes = [
    ...(structural?.meta.warmup_notes || []),
    ...(temporal?.meta.warmup_notes || []),
  ]
  const isDegraded = degradedReasons.length > 0

  // 从 structural.primary 提取关键信息
  const primary = structural?.primary
  const primaryFactors = primary
    ? (Object.values(primary).find((v) => v !== null) as Record<string, unknown> | null)
    : null

  // 从 temporal 提取日线和 15m 上下文
  const daily = temporal?.daily_context
  const m15 = temporal?.m15_response
  const derived = temporal?.derived_relation

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>结构状态摘要</div>
      <div className={styles.summaryCard}>
        {isDegraded && (
          <div className={styles.degradedWarn}>
            当前数据部分降级，部分指标可能不准确。
          </div>
        )}
        {warmupNotes.length > 0 && (
          <div className={styles.warmupNote}>
            数据预热中，部分指标暂不可用。
          </div>
        )}

        {/* 日线结构 */}
        {daily && (
          <div className={styles.summaryGroup}>
            <div className={styles.summaryGroupTitle}>日线结构</div>
            <div className={styles.summaryRow}>
              <span>方向</span>
              <span>{dirText(daily.daily_dsa_dir)}</span>
            </div>
            <div className={styles.summaryRow}>
              <span>段内位置</span>
              <span>{positionText(daily.daily_price_position_in_swing_0_1)}</span>
            </div>
            <div className={styles.summaryRow}>
              <span>距上方节点</span>
              <span>{atrDistanceText(daily.daily_distance_to_node_above_atr)}</span>
            </div>
          </div>
        )}

        {/* 15分钟响应 */}
        {m15 && derived && (
          <div className={styles.summaryGroup}>
            <div className={styles.summaryGroupTitle}>15分钟响应</div>
            <div className={styles.summaryRow}>
              <span>响应方向</span>
              <span>{dirText(derived.m15_response_direction_relative_to_daily)}</span>
            </div>
            <div className={styles.summaryRow}>
              <span>响应强度</span>
              <span>{positionText(derived.m15_response_intensity)}</span>
            </div>
            <div className={styles.summaryRow}>
              <span>区间位置</span>
              <span>{positionText(m15.m15_price_position_in_swing_0_1)}</span>
            </div>
          </div>
        )}

        {/* 成本位置（来自 structural primary） */}
        {primaryFactors && (
          <div className={styles.summaryGroup}>
            <div className={styles.summaryGroupTitle}>成本位置</div>
            <div className={styles.summaryRow}>
              <span>区间位置</span>
              <span>{positionText(primaryFactors.position_0_1 as number)}</span>
            </div>
            <div className={styles.summaryRow}>
              <span>距最密集成交价</span>
              <span>{atrDistanceText(primaryFactors.price_vs_poc_atr as number)}</span>
            </div>
          </div>
        )}

        {!daily && !m15 && !primaryFactors && (
          <div className={styles.empty}>暂无可用的结构状态数据。</div>
        )}
      </div>
    </div>
  )
}
