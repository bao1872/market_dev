// [KeyRangeCard] - 描述: 普通用户关键价格区间卡片
// 从 structural 数据中提取关键价格区间，以人类可读方式展示。
// 不显示内部字段名（如 POC/VAL/VAH），改为"最密集成交价""上方关键位""下方关键位"。
import type { UseQueryResult } from '@tanstack/react-query'
import type { StructuralFactorResponse } from '@/api/endpoints'
import type { ResearchContextStyles } from './ResearchContextPanel'

interface KeyRangeCardProps {
  structuralQuery: UseQueryResult<StructuralFactorResponse, Error>
  styles: ResearchContextStyles
}

function fmtPrice(v: unknown): string {
  if (v === null || v === undefined || typeof v !== 'number' || !isFinite(v)) return '-'
  return v.toFixed(2)
}

export function KeyRangeCard({ structuralQuery, styles }: KeyRangeCardProps) {
  if (structuralQuery.isLoading) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>关键价格区间</div>
        <div className={styles.loading}>加载中…</div>
      </div>
    )
  }

  const structural = structuralQuery.data
  if (!structural) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>关键价格区间</div>
        <div className={structuralQuery.isError ? styles.error : styles.empty}>
          {structuralQuery.isError ? '数据加载失败，请稍后重试。' : '暂无关键价格区间数据。'}
        </div>
      </div>
    )
  }

  // 从 primary[timeframe] 提取 cost_position 组
  const primaryTf = Object.values(structural.primary).find((v) => v !== null)
  const costPosition = primaryTf
    ? (primaryTf as Record<string, unknown>).cost_position as Record<string, unknown> | undefined
    : undefined

  if (!costPosition) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>关键价格区间</div>
        <div className={styles.empty}>暂无关键价格区间数据。</div>
      </div>
    )
  }

  const poc = fmtPrice(costPosition.poc_price)
  const upperNode = costPosition.nearest_upper_node as { price_mid?: number } | null | undefined
  const lowerNode = costPosition.nearest_lower_node as { price_mid?: number } | null | undefined
  const upperPrice = fmtPrice(upperNode?.price_mid)
  const lowerPrice = fmtPrice(lowerNode?.price_mid)

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>关键价格区间</div>
      <div className={styles.summaryCard}>
        <div className={styles.summaryGroup}>
          <div className={styles.summaryRow}>
            <span>最密集成交价</span>
            <span>{poc}</span>
          </div>
          <div className={styles.summaryRow}>
            <span>上方关键位</span>
            <span>{upperPrice}</span>
          </div>
          <div className={styles.summaryRow}>
            <span>下方关键位</span>
            <span>{lowerPrice}</span>
          </div>
        </div>
      </div>
    </div>
  )
}
