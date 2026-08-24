// [R3D] G5/G6 — Structure formal Observation renderer.
// Consumes parsed ViewModels. No recomputation, no score/health/bullish conclusion.
// member_ratio is the PRIMARY product fact; event_count is only evidence.
// Direction color applies ONLY to the canonical direction token (R3D §12).

import { type FC } from 'react'
import styles from './review.module.scss'
import type {
  StructureBreakTurnVM,
  StructureEvolutionPositionVM,
  LeveledStructureEventCell,
  ExtremeStructureEventCell,
  StructureAlignmentVM,
  StructureDistanceVM,
} from './scopeStructureContract'
import { directionTone, formatMemberRatioNullable, formatTrailingDistanceNullable, type DirectionTone } from './scopeStructureContract'

// ---------------------------------------------------------------------------
// Event row (leveled canonical cell)
// ---------------------------------------------------------------------------

function EventRow({ cell, denominator }: { cell: LeveledStructureEventCell; denominator: number | null }) {
  const tone: DirectionTone = directionTone(cell.direction)
  const dirToken = cell.direction ?? '—'
  const level = cell.structureLevel ?? '—'
  const ratio = formatMemberRatioNullable(cell.memberRatio)
  // member_count / denominator is DISPLAY COMPOSITION ONLY; ratio stays persisted.
  const membersComposite =
    cell.memberCount === null || denominator === null
      ? '—'
      : `${cell.memberCount} / ${denominator}`
  const events = cell.eventCount === null ? '—' : cell.eventCount

  if (cell.malformed) {
    return (
      <div className={styles.structEventRow}>
        <span className={`${styles.structEventType} ${styles.neutral}`}>{cell.eventType}</span>
        <span className={styles.structEventInvalid}>结构事实合同异常</span>
      </div>
    )
  }

  return (
    <div className={styles.structEventRow}>
      <div className={styles.structEventHead}>
        <span className={`${styles.structEventType} ${styles.neutral}`}>{cell.eventType}</span>
        <span className={`${styles.structEventDir} ${styles[tone]}`}>{dirToken}</span>
        <span className={`${styles.structEventLevel} ${styles.neutral}`}>{level}</span>
      </div>
      <div className={styles.structEventPrimary}>
        <span className={styles.structMetricLabel}>Member Ratio</span>
        <span className={`${styles.structMetricValue} ${styles.neutral}`}>{ratio}</span>
      </div>
      <div className={styles.structEventSupporting}>
        <span className={styles.structMetricLabel}>Members</span>
        <span className={`${styles.structMetricValue} ${styles.neutral}`}>{membersComposite}</span>
        <span className={styles.structMetricLabel}>Events</span>
        <span className={`${styles.structMetricValue} ${styles.neutral}`}>{events}</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// G5 — Structure Break / Turn
// ---------------------------------------------------------------------------

function BreakTurnSection({ vm }: { vm: StructureBreakTurnVM }) {
  return (
    <div className={styles.structSection}>
      <div className={styles.structSectionTitle}>结构破位 / 转折</div>
      {vm.contractInvalid ? (
        <div className={styles.structContractInvalid}>结构事件合同异常（status/denominator 不可用或非法）</div>
      ) : vm.availability === 'unavailable' ? (
        <div className={styles.structUnavailable}>结构事件覆盖不可用（无有效覆盖成员）</div>
      ) : vm.zeroEventToday ? (
        <div className={styles.structNeutral}>
          覆盖有效；今日未观察到该组结构事件
          {vm.denominator !== null && <span className={styles.structDenominator}>n = {vm.denominator}</span>}
        </div>
      ) : (
        <div className={styles.structEventList}>
          {vm.leveled.map((cell) => (
            <EventRow key={cell.cellKey} cell={cell} denominator={vm.denominator} />
          ))}
          {vm.denominator !== null && (
            <div className={styles.structDenominator}>n = {vm.denominator}</div>
          )}
        </div>
      )}
      {vm.hasContractInvalidity && (
        <div className={styles.structContractInvalid}>检测到非 G5 结构事件类型，已按合同失效处理</div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Extreme row (EQH / EQL) — no direction / no structure level invented
// ---------------------------------------------------------------------------

function ExtremeRow({ cell }: { cell: ExtremeStructureEventCell }) {
  return (
    <div className={styles.structEventRow}>
      <div className={styles.structEventHead}>
        <span className={`${styles.structEventType} ${styles.neutral}`}>{cell.eventType}</span>
      </div>
      <div className={styles.structEventPrimary}>
        <span className={styles.structMetricLabel}>Member Ratio</span>
        <span className={`${styles.structMetricValue} ${styles.neutral}`}>{formatMemberRatioNullable(cell.memberRatio)}</span>
      </div>
      <div className={styles.structEventSupporting}>
        <span className={styles.structMetricLabel}>Members</span>
        <span className={`${styles.structMetricValue} ${styles.neutral}`}>{cell.memberCount === null ? '—' : cell.memberCount}</span>
        <span className={styles.structMetricLabel}>Events</span>
        <span className={`${styles.structMetricValue} ${styles.neutral}`}>{cell.eventCount === null ? '—' : cell.eventCount}</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Alignment (categorical Member Ratio, neutral)
// ---------------------------------------------------------------------------

function AlignmentBlock({ vm }: { vm: StructureAlignmentVM | null }) {
  if (!vm) {
    return (
      <div className={styles.structBlock}>
        <div className={styles.structBlockTitle}>结构对齐</div>
        <div className={styles.structUnavailable}>结构对齐不可用</div>
      </div>
    )
  }
  if (vm.zeroDenominator) {
    return (
      <div className={styles.structBlock}>
        <div className={styles.structBlockTitle}>结构对齐</div>
        <div className={styles.structUnavailable}>对齐覆盖已就绪，但当前无可比较对齐成员</div>
      </div>
    )
  }
  return (
    <div className={styles.structBlock}>
      <div className={styles.structBlockTitle}>结构对齐</div>
      <div className={styles.structAlignGrid}>
        <AlignCell label="Aligned" ratio={vm.alignedRatio} count={vm.alignedCount} />
        <AlignCell label="Divergent" ratio={vm.divergentRatio} count={vm.divergentCount} />
        {vm.denominator !== null && (
          <div className={styles.structDenominator}>n = {vm.denominator}</div>
        )}
      </div>
    </div>
  )
}

function AlignCell({ label, ratio, count }: { label: string; ratio: number | null; count: number | null }) {
  return (
    <div className={styles.structAlignCell}>
      <span className={styles.structMetricLabel}>{label}</span>
      <span className={`${styles.structMetricValue} ${styles.neutral}`}>{formatMemberRatioNullable(ratio)}</span>
      <span className={styles.structMetricSub}>{count === null ? '—' : count}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Trailing distance (current-only distribution, neutral, NO x100)
// ---------------------------------------------------------------------------

function DistanceBlock({ title, vm }: { title: string; vm: StructureDistanceVM | null }) {
  if (!vm) {
    return (
      <div className={styles.structBlock}>
        <div className={styles.structBlockTitle}>{title}</div>
        <div className={styles.structUnavailable}>Trailing 距离不可用</div>
      </div>
    )
  }
  if (vm.unavailable) {
    return (
      <div className={styles.structBlock}>
        <div className={styles.structBlockTitle}>{title}</div>
        <div className={styles.structUnavailable}>Trailing 距离不可用于当前覆盖范围</div>
      </div>
    )
  }
  return (
    <div className={styles.structBlock}>
      <div className={styles.structBlockTitle}>{title}</div>
      <div className={styles.structDistanceGrid}>
        <DistCell label="Median" value={vm.median} />
        <DistCell label="P25" value={vm.p25} />
        <DistCell label="P75" value={vm.p75} />
        {vm.validCount !== null && (
          <div className={styles.structDenominator}>valid = {vm.validCount}</div>
        )}
        {vm.denominator !== null && (
          <div className={styles.structDenominator}>n = {vm.denominator}</div>
        )}
      </div>
    </div>
  )
}

function DistCell({ label, value }: { label: string; value: number | null }) {
  return (
    <div className={styles.structDistCell}>
      <span className={styles.structMetricLabel}>{label}</span>
      <span className={`${styles.structMetricValue} ${styles.neutral}`}>{formatTrailingDistanceNullable(value)}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// G6 — Structure Evolution / Position
// ---------------------------------------------------------------------------

function EvolutionSection({ vm }: { vm: StructureEvolutionPositionVM }) {
  return (
    <div className={styles.structSection}>
      <div className={styles.structSectionTitle}>结构演化 / 位置</div>

      {/* events (independent fact) */}
      <div className={styles.structSubBlock}>
        <div className={styles.structBlockTitle}>结构事件</div>
        {!vm.events ? (
          <div className={styles.structUnavailable}>结构事件不可用</div>
        ) : vm.events.contractInvalid ? (
          <div className={styles.structContractInvalid}>结构事件合同异常（status/denominator 不可用或非法）</div>
        ) : vm.events.availability === 'unavailable' ? (
          <div className={styles.structUnavailable}>结构事件覆盖不可用（无有效覆盖成员）</div>
        ) : vm.events.zeroEventToday ? (
          <div className={styles.structNeutral}>
            覆盖有效；今日未观察到该组结构事件
            {vm.events.denominator !== null && <span className={styles.structDenominator}>n = {vm.events.denominator}</span>}
          </div>
        ) : (
          (() => {
            const ev = vm.events!
            return (
              <div className={styles.structEventList}>
                {ev.leveled.map((cell) => (
                  <EventRow key={cell.cellKey} cell={cell} denominator={ev.denominator} />
                ))}
                {ev.extreme.map((cell) => (
                  <ExtremeRow key={cell.eventType} cell={cell} />
                ))}
                {ev.denominator !== null && (
                  <div className={styles.structDenominator}>n = {ev.denominator}</div>
                )}
              </div>
            )
          })()
        )}
        {vm.eventsMalformed && (
          <div className={styles.structContractInvalid}>存在结构事实合同异常事件单元</div>
        )}
      </div>

      {/* alignment (independent fact) */}
      <AlignmentBlock vm={vm.alignment} />

      {/* trailing distances (independent facts, current-only) */}
      <DistanceBlock title="Distance to Trailing Top" vm={vm.distanceTop} />
      <DistanceBlock title="Distance to Trailing Bottom" vm={vm.distanceBottom} />

      {vm.hasContractInvalidity && (
        <div className={styles.structContractInvalid}>检测到非 G6 结构事件类型（如 SQZ_RELEASE），已按合同失效处理</div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Public component (no fetch; typed parsed facts only)
// ---------------------------------------------------------------------------

interface StructureProps {
  breakTurn?: StructureBreakTurnVM | null
  evolution?: StructureEvolutionPositionVM | null
}

const ScopeStructureObservation: FC<StructureProps> = ({ breakTurn, evolution }) => {
  return (
    <div className={styles.structRoot}>
      {breakTurn && <BreakTurnSection vm={breakTurn} />}
      {evolution && <EvolutionSection vm={evolution} />}
    </div>
  )
}

export default ScopeStructureObservation
