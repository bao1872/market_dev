// [ScopeExplorerTable] - 描述: Scope Explorer 表格视图
// [SLICE 5 / Explorer] 默认 compare-first 11 列（一屏横向比较）。
//
// 默认可见列（严格 11 列）：
//   Scope / DSA Strength / DSA Duration / DSA VWAP Dev / SMC Event /
//   Momentum / Vol20 / EW / Breadth / Capital Tilt / Migration
//
// 默认**不再**展示（canonical owner 仍存在，display 替换 != 删除）：
//   Phase / Position / Velocity / Acceleration / Decline / Unchanged /
//   Coverage / Freshness / HHI / Top5 / Leader Gap / Leader Symbol
//   （旧 canonical owner 与旧 sort key 保留，清理放 Slice 6）
//
// 硬规则：
// - 一切展示值来自 row.compareFacts（backend 单一 batch read-model），
//   绝不读取 raw Observation、绝不调用 detail、绝不 N+1。
// - 单位/方向色/null 文案一律由 scopeExplorerContract（唯一 typed owner）提供；
//   本组件只 render 与处理排序交互。
// - 缺失显示 '—'，绝不填 0、不插值、不 carry。
// - 无综合评分、无加权排序、无机会/风险判断（仅展示与排序 canonical compare 事实）。
// - 表头经 reviewCopy + ReviewTerm 中文化（无裸英文 header）。
import type { ReviewScopeListItem } from './types'
import {
  DIRECTION_COLOR,
  buildExplorerRowVM,
  directionTone,
} from './scopeExplorerContract'
import { UNNAMED_SCOPE_LABEL } from './reviewFormat'
import ReviewTerm from './ReviewTerm'
import { parseReviewSort, reviewSortToggle, type ReviewSort, type ReviewSortKey } from './urlState'
import type { ReviewTermKey } from './reviewCopy'
import styles from './review.module.scss'

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

  /** 可排序 compare 列表头（表头走 ReviewTerm，中文短 label + 完整 tooltip） */
  const renderCompareHeader = (key: ReviewSortKey, termKey: ReviewTermKey) => {
    const active = activeKey === key
    const arrow = active ? (activeDir === 'desc' ? ' ↓' : ' ↑') : ''
    return (
      <th
        className={`${styles.numCell} ${styles.sortableHeader}`}
        aria-sort={active ? (activeDir === 'desc' ? 'descending' : 'ascending') : 'none'}
      >
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
            <th className={styles.scopeColSticky}><ReviewTerm termKey="scope" /></th>
            {renderCompareHeader('dsa_strength', 'dsaStrength')}
            {renderCompareHeader('dsa_duration', 'dsaDuration')}
            {renderCompareHeader('dsa_vwap_dev', 'dsaVwapDev')}
            {renderCompareHeader('smc_member_ratio', 'smcEvent')}
            {renderCompareHeader('momentum_enhancing', 'momentumChange')}
            {renderCompareHeader('volume_ratio20', 'volumeRatio20')}
            {renderCompareHeader('equal_weight_return', 'equalWeightReturn')}
            {renderCompareHeader('advance_ratio', 'advanceRatio')}
            {renderCompareHeader('capital_tilt', 'capitalTilt')}
            {renderCompareHeader('migration', 'leadershipMigration')}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = row.scopeKey === selectedScopeKey
            const vm = buildExplorerRowVM(row.compareFacts ?? null)
            const ewTone = directionTone(vm.equalWeightReturn)
            const tiltTone = directionTone(vm.capitalTilt)
            const vwapTone = directionTone(vm.dsaVwapDev)
            // SMC 方向色来自 typed owner 的 smc.tone（scopeExplorerContract），
            // 不再从展示字符串反推方向。
            const smcTone = vm.smcTone
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
                {/* [Slice A] Scope UUID 不再作为正常产品 UI 展示 */}
                <td className={styles.scopeColSticky}>
                  <div className={styles.explorerPrimary}>{row.scopeName ?? UNNAMED_SCOPE_LABEL}</div>
                </td>
                {/* DSA Strength + peer percentile（secondary text） */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary}>{vm.dsaStrengthText}</div>
                  <div className={styles.explorerSecondary}>{vm.dsaPeerText}</div>
                </td>
                {/* DSA Duration */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary}>{vm.dsaDurationText}</div>
                </td>
                {/* DSA VWAP Dev（已是 percentage points，不 ×100） */}
                <td className={styles.numCell} style={{ color: DIRECTION_COLOR[vwapTone] }}>
                  <div className={styles.explorerPrimary}>{vm.dsaVwapDevText}</div>
                </td>
                {/* SMC Event（compact） */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary} style={{ color: DIRECTION_COLOR[smcTone] }}>
                    {vm.smcPrimaryText}
                  </div>
                  <div className={styles.explorerSecondary}>{vm.smcSecondaryText}</div>
                </td>
                {/* Momentum：增强 / 减弱 两侧 */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary}>{vm.momentumText}</div>
                  <div className={styles.explorerSecondary}>{vm.momentumSecondaryText}</div>
                </td>
                {/* Vol20 */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary}>{vm.volumeRatio20Text}</div>
                </td>
                {/* EW + peer percentile */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary} style={{ color: DIRECTION_COLOR[ewTone] }}>
                    {vm.equalWeightReturnText}
                  </div>
                  <div className={styles.explorerSecondary}>{vm.ewPeerText}</div>
                </td>
                {/* Breadth */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary}>{vm.advanceRatioText}</div>
                </td>
                {/* Capital Tilt（persisted fact） */}
                <td className={styles.numCell} style={{ color: DIRECTION_COLOR[tiltTone] }}>
                  <div className={styles.explorerPrimary}>{vm.capitalTiltText}</div>
                </td>
                {/* Migration（persisted fact，raw 0–1） */}
                <td className={styles.numCell}>
                  <div className={styles.explorerPrimary}>{vm.migrationText}</div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
