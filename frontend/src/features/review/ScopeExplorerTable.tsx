// [ScopeExplorerTable] - 描述: Scope Explorer 表格视图（Slice D）
// 精确用户可见列：Scope / Phase / Position / Velocity / Acceleration /
// EW Return / Capital Tilt / Breadth / Leadership Migration / Coverage。
// Position 为 0–100 percentile，直接展示原值（绝不乘 100）。
// Breadth 只展示持久化的 advance/decline/unchanged 三分量，不计算 composite score。
// A股红涨绿跌：EW Return / Capital Tilt 正=红 负=绿；品牌色仅用于选中行。
import type { ReviewScopeListItem } from './types'
import {
  NULL_DISPLAY,
  formatPercentNullable,
  formatNumberNullable,
  formatPosition,
  formatPhaseLabel,
} from './reviewFormat'
import styles from './review.module.scss'

function directionClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return styles.neutral
  if (value > 0) return styles.up
  if (value < 0) return styles.down
  return styles.neutral
}

function BreadthCell({ summary }: { summary: ReviewScopeListItem['summary'] }) {
  if (!summary) {
    return <span className={styles.metricUnavailable}>{NULL_DISPLAY}</span>
  }
  const { advanceRatio, declineRatio, unchangedRatio } = summary
  const parts: Array<{ label: string; value: number | null | undefined; cls: string }> = [
    { label: '↑', value: advanceRatio, cls: styles.up },
    { label: '↓', value: declineRatio, cls: styles.down },
    { label: '—', value: unchangedRatio, cls: styles.neutral },
  ]
  const rendered = parts.map((p) => {
    if (p.value === null || p.value === undefined) return null
    return (
      <span key={p.label} className={`${styles.breadthPart} ${p.cls}`} title={`${p.label === '↑' ? '上涨' : p.label === '↓' ? '下跌' : '平盘'} ${formatPercentNullable(p.value)}`}>
        {p.label}
        {formatPercentNullable(p.value)}
      </span>
    )
  })
  if (rendered.every((r) => r === null)) {
    return <span className={styles.metricUnavailable}>{NULL_DISPLAY}</span>
  }
  return <span className={styles.breadthCell}>{rendered}</span>
}

export interface ScopeExplorerTableProps {
  rows: ReviewScopeListItem[]
  selectedScopeKey: string | null
  onSelectScope: (scopeKey: string) => void
}

export default function ScopeExplorerTable({ rows, selectedScopeKey, onSelectScope }: ScopeExplorerTableProps) {
  return (
    <div className={styles.explorerTableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Scope</th>
            <th>Phase</th>
            <th className={styles.numCell}>Position</th>
            <th className={styles.numCell}>Velocity</th>
            <th className={styles.numCell}>Acceleration</th>
            <th className={styles.numCell}>EW Return</th>
            <th className={styles.numCell}>Capital Tilt</th>
            <th>Breadth</th>
            <th className={styles.numCell}>Leadership Migration</th>
            <th className={styles.numCell}>Coverage</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = row.scopeKey === selectedScopeKey
            const s = row.summary
            return (
              <tr
                key={row.scopeKey}
                className={selected ? styles.explorerRowSelected : undefined}
                onClick={() => onSelectScope(row.scopeKey)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelectScope(row.scopeKey)
                  }
                }}
                tabIndex={0}
                role="button"
                aria-selected={selected}
                aria-label={`选择 Scope ${row.scopeName ?? row.scopeKey}`}
              >
                <td>
                  <div className={styles.scopeCellName}>{row.scopeName ?? NULL_DISPLAY}</div>
                  <div className={styles.scopeCellKey}>{row.scopeKey}</div>
                </td>
                <td>{formatPhaseLabel(s?.phase)}</td>
                <td className={styles.numCell}>{formatPosition(s?.position)}</td>
                <td className={styles.numCell}>{formatNumberNullable(s?.velocity)}</td>
                <td className={styles.numCell}>{formatNumberNullable(s?.acceleration)}</td>
                <td className={`${styles.numCell} ${directionClass(s?.equalWeightReturn)}`}>
                  {formatPercentNullable(s?.equalWeightReturn)}
                </td>
                <td className={`${styles.numCell} ${directionClass(s?.capitalTilt)}`}>
                  {formatPercentNullable(s?.capitalTilt)}
                </td>
                <td>
                  <BreadthCell summary={s} />
                </td>
                <td className={styles.numCell}>{formatNumberNullable(s?.migration)}</td>
                <td className={styles.numCell}>
                  {row.coverageRatio !== null && row.coverageRatio !== undefined
                    ? formatPercentNullable(row.coverageRatio)
                    : NULL_DISPLAY}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
