// [AttributionTable] - 描述: 归因表格组件（PRD §14.5 阶段三：子范围贡献表）
// 字段：子范围名称/类型/关系/贡献值（带正负贡献条）/排名/coverage
// 归因不得仅按涨幅排序；保留正贡献和负贡献；按绝对贡献排序（后端已排序）
import type { ReviewAttribution } from './types'
import ReviewDataQualityBadge from './ReviewDataQualityBadge'
import styles from './review.module.scss'

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return n.toFixed(digits)
}

/** 贡献条：以中点为基准，正值向右（红/上涨色），负值向左（绿/下跌色） */
function ContributionBar({ value }: { value: number | null }) {
  if (value === null || Number.isNaN(value)) {
    return <span className={styles.metricUnavailable}>-</span>
  }
  // 归一化到 0-50% 宽度（假设 |贡献| 上界 1.0，超出按满格）
  const pct = Math.min(50, Math.abs(value) * 50)
  return (
    <span className={styles.contributionBar}>
      <span className={styles.contributionTrack}>
        <span className={styles.contributionMidline} />
        {value >= 0 ? (
          <span
            className={styles.contributionFillPos}
            style={{ width: `${pct}%` }}
          />
        ) : (
          <span
            className={styles.contributionFillNeg}
            style={{ width: `${pct}%` }}
          />
        )}
      </span>
      <span className={value >= 0 ? styles.up : styles.down}>
        {value >= 0 ? '+' : ''}
        {fmtNum(value)}
      </span>
    </span>
  )
}

export interface AttributionTableProps {
  items: ReviewAttribution[]
  /** 点击子范围行回调（下钻到子范围） */
  onRowClick?: (attr: ReviewAttribution) => void
}

export default function AttributionTable({
  items,
  onRowClick,
}: AttributionTableProps) {
  if (items.length === 0) {
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>无可归因子范围</div>
        <div className={styles.stateDesc}>
          该信号未生成子范围归因，可能原因：父范围无直接子范围，或子范围 coverage 不足
        </div>
      </div>
    )
  }
  return (
    <div className={styles.panelSection}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>子范围</th>
            <th>类型</th>
            <th>关系</th>
            <th>贡献</th>
            <th className={styles.numCell}>排名</th>
            <th className={styles.numCell}>coverage</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {items.map((attr) => (
            <tr
              key={attr.id}
              onClick={() => onRowClick?.(attr)}
            >
              <td>{attr.childScopeName}</td>
              <td>{attr.childScopeType}</td>
              <td>{attr.relationType ?? '-'}</td>
              <td><ContributionBar value={attr.contributionValue} /></td>
              <td className={styles.numCell}>{attr.contributionRank ?? '-'}</td>
              <td className={styles.numCell}>
                {attr.coverageRatio !== null
                  ? `${(attr.coverageRatio * 100).toFixed(1)}%`
                  : '-'}
              </td>
              <td>
                <ReviewDataQualityBadge
                  status={attr.coverageRatio !== null && attr.coverageRatio >= 0.95 ? 'ready' : 'partial'}
                  coverage={attr.coverageRatio}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
