// [ScopeMetricsTable] - 描述: 范围指标表格组件（PRD §14.3 阶段一主表）
// 字段：范围名称 / 范围类型 / P/Q/U/C/V（值+方向+历史分位细条）/ 命中数量 / coverage / 数据状态
// 每个变量单元格：值 + 方向箭头 + 历史分位细条；不使用雷达图
// 点击一行：更新 URL scope，进入该范围信号列表，不直接跳转个股
// 前端不计算聚合变量，只展示后端返回的 payload
import type { ReviewScopeMetrics, ReviewMetricPayload, MetricKey } from './types'
import ReviewDataQualityBadge, { isMetricAvailable } from './ReviewDataQualityBadge'
import styles from './review.module.scss'

const METRIC_KEYS: MetricKey[] = ['p', 'q', 'u', 'c', 'v']
const METRIC_LABELS: Record<MetricKey, string> = {
  p: 'P',
  q: 'Q',
  u: 'U',
  c: 'C',
  v: 'V',
}

/** 数字格式化：保留 1 位小数；null 显示 - */
function fmt(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return n.toFixed(digits)
}

/** 变化值方向 class */
function deltaClass(delta: number | null): string {
  if (delta === null || delta === 0) return styles.neutral
  return delta > 0 ? styles.up : styles.down
}

/** 方向箭头 */
function arrow(delta: number | null): string {
  if (delta === null || delta === 0) return ''
  return delta > 0 ? '▲' : '▼'
}

/** 历史分位细条颜色：高位红/低位绿/中性蓝（A股语义不套用到分位，这里用信息色区分） */
function percentileClass(p: number | null): string {
  if (p === null) return styles.percentileFill
  if (p >= 70) return styles.percentileFillHigh
  if (p <= 30) return styles.percentileFillLow
  return styles.percentileFill
}

export interface ScopeMetricsTableProps {
  items: ReviewScopeMetrics[]
  /** 当前选中范围（scopeKey），高亮对应行 */
  activeScopeKey?: string | null
  /** 点击行回调（更新 URL scope） */
  onRowClick?: (scope: ReviewScopeMetrics) => void
}

/** 单个指标单元格：值 + 方向箭头 + 历史分位细条 */
function MetricCell({ payload }: { payload: ReviewMetricPayload | null }) {
  if (!payload || !isMetricAvailable(payload)) {
    const reason = !payload
      ? '未计算'
      : payload.status === 'insufficient_history'
        ? '历史不足'
        : payload.status === 'unavailable'
          ? '不可用'
          : payload.status
    return <span className={styles.metricUnavailable}>{reason}</span>
  }
  const pct = payload.historyPercentile120d
  return (
    <span className={styles.metricCell}>
      <span className={styles.metricValue}>{fmt(payload.value)}</span>
      <span className={`${styles.metricDelta} ${deltaClass(payload.delta1d)}`}>
        {arrow(payload.delta1d)}
        {payload.delta1d !== null ? fmt(payload.delta1d) : ''}
      </span>
      {pct !== null && (
        <span
          className={styles.percentileBar}
          title={`120日分位 ${fmt(pct)}`}
        >
          <span
            className={`${styles.percentileFill} ${percentileClass(pct)}`}
            style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
          />
        </span>
      )}
    </span>
  )
}

export default function ScopeMetricsTable({
  items,
  activeScopeKey,
  onRowClick,
}: ScopeMetricsTableProps) {
  if (items.length === 0) {
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>暂无范围数据</div>
        <div className={styles.stateDesc}>当日复盘未包含任何范围扫描结果</div>
      </div>
    )
  }
  return (
    <div className={styles.panelSection}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>范围名称</th>
            <th>类型</th>
            {METRIC_KEYS.map((k) => (
              <th key={k} className={styles.numCell}>{METRIC_LABELS[k]}</th>
            ))}
            <th className={styles.numCell}>命中</th>
            <th className={styles.numCell}>coverage</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {items.map((scope) => {
            const active = activeScopeKey === scope.scopeKey
            return (
              <tr
                key={`${scope.scopeType}:${scope.scopeKey}`}
                className={active ? styles.tableRowActive : undefined}
                onClick={() => onRowClick?.(scope)}
              >
                <td>{scope.scopeName}</td>
                <td>{scope.scopeType}</td>
                {METRIC_KEYS.map((k) => (
                  <td key={k} className={styles.numCell}>
                    <MetricCell payload={scope[k]} />
                  </td>
                ))}
                <td className={styles.numCell}>{scope.signalCount}</td>
                <td className={styles.numCell}>{(scope.coverageRatio * 100).toFixed(1)}%</td>
                <td>
                  <ReviewDataQualityBadge
                    status={scope.status}
                    coverage={scope.coverageRatio}
                  />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// 导出指标单元格与格式化工具，供 EvidenceDrawer 等复用
export { MetricCell, fmt, deltaClass, arrow, percentileClass, METRIC_KEYS, METRIC_LABELS }
