// [ScopeExplorerTable] - 描述: Scope Explorer 表格视图（Slice D + R2B）
// 精确用户可见列：Scope / Phase / Position / Velocity / Acceleration /
// EW Return / Capital Tilt / Breadth / Leadership Migration / Coverage /
// Freshness / Technical。
// Position 为 0–100 percentile，直接展示原值（绝不乘 100）。
// Breadth 只展示持久化的 advance/decline/unchanged 三分量，不计算 composite score。
// A股红涨绿跌：EW Return / Capital Tilt 正=红 负=绿；品牌色仅用于选中行。
// R2B Freshness / Technical 为中性 analytics，不使用方向色（§15）。
// REVIEW-UX-CN-01：表头经 reviewCopy + ReviewTerm 中文化并带 tooltip；
// 单元格计算与 canonical 字段一律不变。
import type { ReviewScopeListItem } from './types'
import {
  NULL_DISPLAY,
  formatPercentNullable,
  formatNumberNullable,
  formatPosition,
  formatPhaseLabel,
  formatContributionFraction,
} from './reviewFormat'
import ReviewTerm from './ReviewTerm'
import { parseReviewSort, reviewSortToggle, type ReviewSort, type ReviewSortKey } from './urlState'
import type { ReviewTermKey } from './reviewCopy'
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
      <span className={styles.mono}>密度 {rhoStr}</span>
      {'  '}
      <span className={styles.mono}>今日 {tStr}</span>
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
  // P1-E：普通表格使用用户可理解的中文简称；完整解释见列头 ReviewTerm tooltip。
  // 不改变任何 canonical 数据与算法，仅展示标签中文化。
  const parts = [
    hhi === null || hhi === undefined ? null : { label: '集中度', value: hhi.toFixed(3) },
    top5 === NULL_DISPLAY ? null : { label: '头部贡献', value: top5 },
    gap === null || gap === undefined ? null : { label: '主导-中位差', value: gap.toFixed(2) },
    leader === null || leader === undefined ? null : { label: '主导成员', value: leader },
  ].filter((p): p is { label: string; value: string } => p !== null)
  if (parts.length === 0) return <span className={styles.neutral}>{NULL_DISPLAY}</span>
  return (
    <span className={styles.neutral}>
      {parts.map((p, i) => (
        <span key={i}>
          <span className={styles.mono}>{p.label} {p.value}</span>
          {i < parts.length - 1 ? '  ' : ''}
        </span>
      ))}
    </span>
  )
}

export interface ScopeExplorerTableProps {
  rows: ReviewScopeListItem[]
  selectedScopeKey: string | null
  sort: ReviewSort
  onSortChange: (sort: ReviewSort) => void
  onSelectScope: (scopeKey: string) => void
}

export default function ScopeExplorerTable({ rows, selectedScopeKey, sort, onSortChange, onSelectScope }: ScopeExplorerTableProps) {
  const { key: activeKey, dir: activeDir } = parseReviewSort(sort)

  // P0-2：表头点击排序。第一次点击→降序 ↓；第二次点击→升序 ↑（无第三态）。
  const handleSortClick = (key: ReviewSortKey) => {
    onSortChange(reviewSortToggle(key, sort))
  }

  const renderSortableHeader = (key: ReviewSortKey, termKey: ReviewTermKey) => {
    const active = activeKey === key
    const arrow = active ? (activeDir === 'desc' ? ' ↓' : ' ↑') : ''
    return (
      <th className={`${styles.numCell} ${styles.sortableHeader}`} aria-sort={active ? (activeDir === 'desc' ? 'descending' : 'ascending') : 'none'}>
        <button
          type="button"
          className={styles.sortHeaderBtn}
          onClick={() => handleSortClick(key)}
          title="点击按此列排序"
          aria-label={`按${termKey}排序`}
        >
          <ReviewTerm termKey={termKey} />
          <span className={styles.sortArrow} aria-hidden="true">{arrow}</span>
        </button>
      </th>
    )
  }

  return (
    <div className={styles.explorerTableWrap}>
      <table className={styles.explorerScopeTable}>
        <thead>
          <tr>
            <th><ReviewTerm termKey="scope" /></th>
            {renderSortableHeader('phase', 'phase')}
            {renderSortableHeader('position', 'position')}
            {renderSortableHeader('velocity', 'velocity')}
            {renderSortableHeader('acceleration', 'acceleration')}
            <th className={styles.numCell}><ReviewTerm termKey="equalWeightReturn" /></th>
            <th className={styles.numCell}><ReviewTerm termKey="capitalTilt" /></th>
            <th><ReviewTerm termKey="breadth" /></th>
            <th className={styles.numCell}><ReviewTerm termKey="leadershipMigration" /></th>
            <th className={styles.numCell}><ReviewTerm termKey="coverage" /></th>
            <th><ReviewTerm termKey="freshness" /></th>
            <th><ReviewTerm termKey="technical" /></th>
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
                aria-label={`选择板块 ${row.scopeName ?? row.scopeKey}`}
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
