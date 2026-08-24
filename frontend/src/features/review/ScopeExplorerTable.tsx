// [ScopeExplorerTable] - 描述: Scope Explorer 表格视图（Slice D + R2B）
// 精确用户可见列：Scope / Phase / Position / Velocity / Acceleration /
// EW Return / Capital Tilt / Breadth / Leadership Migration / Coverage /
// Freshness / Technical。
// Position 为 0–100 percentile，直接展示原值（绝不乘 100）。
// Breadth 只展示持久化的 advance/decline/unchanged 三分量，不计算 composite score。
// A股红涨绿跌：EW Return / Capital Tilt 正=红 负=绿；品牌色仅用于选中行。
// R2B Freshness / Technical 为中性 analytics，不使用方向色（§15）。
import type { ReviewScopeListItem } from './types'
import {
  NULL_DISPLAY,
  formatPercentNullable,
  formatNumberNullable,
  formatPosition,
  formatPhaseLabel,
  formatContributionFraction,
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

// R2B Observation cells — NEUTRAL analytics only（§15：无方向色、无机会标签）
function FreshnessCell({ obs }: { obs: ReviewScopeListItem['observationSummary'] }) {
  if (!obs) return <span className={styles.neutral}>{NULL_DISPLAY}</span>
  const rho = obs.freshnessDecayWeightedDensity
  const t = obs.freshnessTodayCount
  if (rho === null || rho === undefined) {
    if (t === null || t === undefined) return <span className={styles.neutral}>{NULL_DISPLAY}</span>
  }
  const rhoStr = rho === null || rho === undefined ? NULL_DISPLAY : rho.toFixed(3)
  const tStr = t === null || t === undefined ? NULL_DISPLAY : String(t)
  return (
    <span className={styles.neutral}>
      <span className={styles.mono}>ρ {rhoStr}</span>
      {'  '}
      <span className={styles.mono}>T {tStr}</span>
    </span>
  )
}

function TechnicalCell({ obs }: { obs: ReviewScopeListItem['observationSummary'] }) {
  if (!obs) return <span className={styles.neutral}>{NULL_DISPLAY}</span>
  const hhi = obs.technicalHhi
  const top5 = formatContributionFraction({
    numerator: obs.technicalTop5Numerator,
    denominator: obs.technicalTop5Denominator,
  })
  const gap = obs.technicalLeaderMedianGap
  const leader = obs.technicalLeaderSymbol
  const parts = [
    hhi === null || hhi === undefined ? null : `HHI ${hhi.toFixed(3)}`,
    top5 === NULL_DISPLAY ? null : `Top5 ${top5}`,
    gap === null || gap === undefined ? null : `Gap ${gap.toFixed(2)}`,
    leader === null || leader === undefined ? null : `L ${leader}`,
  ].filter((p): p is string => p !== null)
  if (parts.length === 0) return <span className={styles.neutral}>{NULL_DISPLAY}</span>
  return (
    <span className={styles.neutral}>
      {parts.map((p, i) => (
        <span key={i}>
          <span className={styles.mono}>{p}</span>
          {i < parts.length - 1 ? '  ' : ''}
        </span>
      ))}
    </span>
  )
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
            <th>Freshness</th>
            <th>Technical</th>
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
                <td><FreshnessCell obs={row.observationSummary} /></td>
                <td><TechnicalCell obs={row.observationSummary} /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
