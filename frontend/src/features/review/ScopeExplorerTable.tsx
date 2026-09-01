// [ScopeExplorerTable] - 描述: Scope Explorer 表格视图（Slice D + R2B + Slice C 原子化）
//
// 精确用户可见列 —— 全部原子化，不再有复合 cell：
//   Scope / Phase / Position / Velocity / Acceleration /
//   EW Return / Capital Tilt /
//   上涨占比 / 下跌占比 / 平盘占比 /
//   Leadership Migration / Coverage /
//   事件密度 / 今日事件数 /
//   技术集中度 / 前5强度占比 / 最高-中位强度差 / 最高强度成员
//
// 规则：
// - Position 为 0–100 percentile，直接展示原值（绝不乘 100）。
// - 每个「可见 scalar numeric 列」都可点击 asc/desc；最高强度成员是字符串，不排序。
// - 缺失（null）显示 '—'，绝不填 0、不插值、不 carry。
// - Freshness / Technical 为中性 analytics，不使用方向色（§15）。
// - A股红涨绿跌：EW Return / Capital Tilt 正=红 负=绿；品牌色仅用于选中行。
// - 研究工具宁愿横向滚动，也不把不同指标塞回一个 cell。
// - 表头经 reviewCopy + ReviewTerm 中文化；单元格不重算任何 canonical 字段。
import type { ReviewScopeListItem } from './types'
import {
  NULL_DISPLAY,
  UNNAMED_SCOPE_LABEL,
  formatPercentNullable,
  formatNumberNullable,
  formatPosition,
  formatPhaseLabel,
} from './reviewFormat'
import { technicalTop5Ratio } from './scopeExplorerViewModel'
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

export interface ScopeExplorerTableProps {
  rows: ReviewScopeListItem[]
  selectedScopeKey: string | null
  sort: ReviewSort
  onSortChange: (sort: ReviewSort) => void
  onSelectScope: (scopeKey: string) => void
}

export default function ScopeExplorerTable({ rows, selectedScopeKey, sort, onSortChange, onSelectScope }: ScopeExplorerTableProps) {
  const { key: activeKey, dir: activeDir } = parseReviewSort(sort)

  // 表头点击排序：第一次→降序 ↓；第二次同一列→升序 ↑（无第三态）。
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

  /** 非排序列表头（字符串列：最高强度成员） */
  const renderPlainHeader = (termKey: ReviewTermKey) => (
    <th>
      <ReviewTerm termKey={termKey} />
    </th>
  )

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
            {renderSortableHeader('equal_weight_return', 'equalWeightReturn')}
            {renderSortableHeader('capital_tilt', 'capitalTilt')}
            {renderSortableHeader('advance_ratio', 'advanceRatio')}
            {renderSortableHeader('decline_ratio', 'declineRatio')}
            {renderSortableHeader('unchanged_ratio', 'unchangedRatio')}
            {renderSortableHeader('migration', 'leadershipMigration')}
            {renderSortableHeader('coverage', 'coverage')}
            {renderSortableHeader('freshness_density', 'freshnessDensity')}
            {renderSortableHeader('freshness_today', 'freshnessTodayCount')}
            {renderSortableHeader('technical_hhi', 'technicalHhi')}
            {renderSortableHeader('technical_top5_ratio', 'technicalTop5Ratio')}
            {renderSortableHeader('leader_median_gap', 'technicalLeaderMedianGap')}
            {renderPlainHeader('technicalLeaderSymbol')}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = row.scopeKey === selectedScopeKey
            const s = row.summary
            const obs = row.observationSummary
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
                aria-label={`选择板块 ${row.scopeName ?? UNNAMED_SCOPE_LABEL}`}
              >
                {/* [Slice A] Scope UUID 不再作为正常产品 UI 展示；
                    身份仍由 key={row.scopeKey} / onSelectScope / URL scopeKey 承载。 */}
                <td>
                  <div className={styles.scopeCellName}>{row.scopeName ?? NULL_DISPLAY}</div>
                </td>
                <td>{formatPhaseLabel(s?.phase)}</td>
                <td className={styles.numCell}>{formatPosition(s?.position)}</td>
                <td className={styles.numCell}>{formatNumberNullable(s?.velocity)}</td>
                <td className={styles.numCell}>{formatNumberNullable(s?.acceleration)}</td>
                <td className={`${styles.numCell} ${directionClass(s?.equalWeightReturn)}`}>
                  {formatPercentNullable(s?.equalWeightReturn, 2)}
                </td>
                <td className={`${styles.numCell} ${directionClass(s?.capitalTilt)}`}>
                  {formatPercentNullable(s?.capitalTilt, 2)}
                </td>
                {/* Breadth 原子化：三分量各自独立成列，不再合成一格 */}
                <td className={styles.numCell}>{formatPercentNullable(s?.advanceRatio, 2)}</td>
                <td className={styles.numCell}>{formatPercentNullable(s?.declineRatio, 2)}</td>
                <td className={styles.numCell}>{formatPercentNullable(s?.unchangedRatio, 2)}</td>
                <td className={styles.numCell}>{formatNumberNullable(s?.migration)}</td>
                <td className={styles.numCell}>
                  {row.coverageRatio !== null && row.coverageRatio !== undefined
                    ? formatPercentNullable(row.coverageRatio, 2)
                    : NULL_DISPLAY}
                </td>
                {/* Freshness 原子化：密度 / 今日事件数各自独立成列 */}
                <td className={styles.numCell}>{formatNumberNullable(obs?.freshnessDecayWeightedDensity, 3)}</td>
                <td className={styles.numCell}>{formatNumberNullable(obs?.freshnessTodayCount, 0)}</td>
                {/* Technical 原子化：HHI / Top5 占比 / 差值 / 最高成员各自独立成列 */}
                <td className={styles.numCell}>{formatNumberNullable(obs?.technicalHhi, 3)}</td>
                <td className={styles.numCell}>{formatPercentNullable(technicalTop5Ratio(obs), 2)}</td>
                <td className={styles.numCell}>{formatNumberNullable(obs?.technicalLeaderMedianGap, 2)}</td>
                <td>{obs?.technicalLeaderSymbol ?? NULL_DISPLAY}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
