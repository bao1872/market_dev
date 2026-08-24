// [ScopeCurrentSnapshotPanel] - 描述: Current Scope Snapshot 面板（R1 projection layer）
//
// 硬契约（prompt §3–§15）：
// - 单一解析 owner：所有值来自 parseCurrentSnapshot(...)，组件绝不散落 `payload.xxx as SomeType`。
// - 纯 projection：phase/score/信号/HHI/集中度/广度/capital tilt/freshness/decay 全部来自 persisted，
//   前端不重算（capital tilt 不重算 AW-EW；top3/top5 不重算百分比；HHI 不取 price/amount normalized）。
// - null != 0：缺失事实显示 "—"，不可把 null 当 0；today_count=0 是有效零事件，原样展示。
// - A-share 方向色 ONLY 用于市场方向事实：EW Return / AW Return / Capital Tilt（positive=red、negative=green）。
//   其它（Velocity/Acceleration/HHI/Freshness density/Dispersion/leader gap/event count）一律中性，绝不上色。
// - 不引入 "Board Analysis" 上游产品概念；board_ready_member_count 等 persisted fact 名仅作事实标签。
// - 不替用户下结论：无 bullish/bearish/机会/风险/健康 文本分类。
// - 单一 detail owner（useReviewScopeDetail）已在上层加载；本面板不发任何请求，无 N+1。
import type { ReactNode } from 'react'
import type { ScopeCurrentSnapshot } from './scopeDetailContract'
import type {
  ScopeObservationCurrentState,
  ScopeFreshnessFacts,
  ScopeLatestEventPair,
} from './types'
import {
  NULL_DISPLAY,
  formatPercentNullable,
  formatNumberNullable,
  formatPosition,
  formatPhaseLabel,
  formatContributionFraction,
} from './reviewFormat'
import { directionClass } from './ScopeInternalStructurePanel'
import styles from './review.module.scss'

function FactRow({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className={styles.factRow} title={title}>
      <span className={styles.factLabel}>{label}</span>
      <span className={styles.factValue}>{value}</span>
    </div>
  )
}

function MetricCell({ label, value, dir, title }: { label: string; value: ReactNode; dir?: string; title?: string }) {
  return (
    <div className={styles.metricCell}>
      <span className={styles.metricLabel}>{label}</span>
      <span className={`${styles.metricValue} ${dir ?? ''}`} title={title}>{value}</span>
    </div>
  )
}

/** 方向色只用于市场方向事实（EW/AW/Capital Tilt）；其余中性 */
function DirPct({ value }: { value: number | null }) {
  return (
    <span className={directionClass(value)}>{formatPercentNullable(value, 2)}</span>
  )
}
function DirNum({ value }: { value: number | null }) {
  return (
    <span className={directionClass(value)}>{formatNumberNullable(value, 3)}</span>
  )
}

function pairStr(pair: ScopeLatestEventPair | null): string {
  if (!pair) return NULL_DISPLAY
  return `${formatNumberNullable(pair.up, 0)}↑ / ${formatNumberNullable(pair.down, 0)}↓`
}

// ============================================================
// A. Current Regime
// ============================================================
function RegimeGroup({ regime }: { regime: ScopeCurrentSnapshot['regime'] }) {
  if (!regime) {
    return <div className={styles.panelUnavailable}>当前 Regime 不可用（无 persisted dynamics_phase fact）</div>
  }
  return (
    <dl className={styles.metricGroup}>
      <dt className={styles.metricHeading}>Current Regime</dt>
      <dd className={styles.factStrip}>
        <FactRow label="Status" value={regime.status ?? NULL_DISPLAY} title="来自末尾 persisted dynamics_phase fact" />
        <FactRow
          label="Phase"
          value={formatPhaseLabel(regime.phase)}
          title="ready + phase=null → —（不发明第 7 个 phase）"
        />
        <FactRow label="Position" value={formatPosition(regime.position)} title="persisted position（0–100 历史百分位；0 是有效值）" />
        <FactRow label="Velocity" value={formatNumberNullable(regime.velocity)} title="中性分析值，不上色" />
        <FactRow label="Acceleration" value={formatNumberNullable(regime.acceleration)} title="中性分析值，不上色" />
        <FactRow
          label="Upper Occ"
          value={regime.upperOccupancy === null ? NULL_DISPLAY : formatPercentNullable(regime.upperOccupancy)}
        />
        <FactRow
          label="Lower Occ"
          value={regime.lowerOccupancy === null ? NULL_DISPLAY : formatPercentNullable(regime.lowerOccupancy)}
        />
      </dd>
    </dl>
  )
}

// ============================================================
// B. Breadth & Participation
// ============================================================
function ParticipationGroup({ current }: { current: ScopeCurrentSnapshot }) {
  const p = current.participation
  const id = current.identity
  return (
    <dl className={styles.metricGroup}>
      <dt className={styles.metricHeading}>Breadth &amp; Participation</dt>
      <dd className={styles.metricGrid}>
        <MetricCell label="Eligible" value={id ? formatNumberNullable(id.eligibleCount, 0) : NULL_DISPLAY} />
        <MetricCell label="Provided" value={id ? formatNumberNullable(id.providedCount, 0) : NULL_DISPLAY} />
        <MetricCell
          label="Coverage"
          value={id && id.coverageRatio !== null ? formatPercentNullable(id.coverageRatio) : NULL_DISPLAY}
        />
        {p ? (
          <>
            <MetricCell label="EW Return" value={<DirPct value={p.equalWeightReturn} />} />
            <MetricCell label="AW Return" value={<DirPct value={p.amountWeightedReturn} />} />
            <MetricCell label="Capital Tilt" value={<DirNum value={p.capitalTilt} />} />
            <MetricCell label="Advance" value={formatPercentNullable(p.advanceRatio)} title="persisted 比率，不重归一化" />
            <MetricCell label="Decline" value={formatPercentNullable(p.declineRatio)} title="persisted 比率，不重归一化" />
            <MetricCell label="Unchanged" value={formatPercentNullable(p.unchangedRatio)} title="persisted 比率，不重归一化" />
            <MetricCell label="Ret Disp" value={formatNumberNullable(p.returnDispersion, 3)} title="中性分析值，不上色" />
          </>
        ) : (
          <div className={styles.metricEmpty}>Participation/Breadth 不可用（无 internal_structure_facts）</div>
        )}
      </dd>
    </dl>
  )
}

// ============================================================
// C. Current Technical State
// ============================================================
function TechnicalGroup({ currentState }: { currentState: ScopeObservationCurrentState | null }) {
  if (!currentState) {
    return <div className={styles.panelUnavailable}>Current Technical State 不可用（observation 无 structure.current_state）</div>
  }
  const tech = currentState.technical_state
  const conc = tech?.concentration ?? null
  const disp = tech?.dispersion ?? null
  const le = currentState.latest_events
  return (
    <>
      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Current Technical State</dt>
        <dd className={styles.metricGrid}>
          <MetricCell label="Tech-ready" value={formatNumberNullable(currentState.board_ready_member_count, 0)} title="persisted board_ready_member_count（历史 fact key 命名，非 Board runtime owner）" />
          <MetricCell label="Active OB mean" value={formatNumberNullable(currentState.mean_active_orderblock_count, 2)} />
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Technical Concentration</dt>
        <dd className={styles.metricGrid}>
          {conc ? (
            <>
              <MetricCell label="Top3" value={formatContributionFraction(conc.top3_contribution)} title="persisted 分数，绝不重算百分比" />
              <MetricCell label="Top5" value={formatContributionFraction(conc.top5_contribution)} title="denominator=0 → 显示 —，不伪造 0%" />
              <MetricCell label="HHI" value={formatNumberNullable(conc.hhi, 3)} title="technical_state.concentration.hhi，非 price/amount normalized HHI" />
              <MetricCell label="Leader" value={conc.leader_symbol ?? NULL_DISPLAY} />
              <MetricCell label="Leader Mag" value={formatNumberNullable(conc.leader_magnitude, 2)} />
              <MetricCell label="Median Mag" value={formatNumberNullable(conc.median_magnitude, 2)} />
              <MetricCell label="Leader−Median" value={formatNumberNullable(conc.leader_median_gap, 2)} title="中性分析值，不上色" />
              <MetricCell label="Count" value={formatNumberNullable(conc.count, 0)} />
            </>
          ) : (
            <div className={styles.metricEmpty}>Concentration 不可用</div>
          )}
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Technical Dispersion</dt>
        <dd className={styles.metricGrid}>
          {disp ? (
            <>
              <MetricCell label="Count" value={formatNumberNullable(disp.count, 0)} />
              <MetricCell label="Mean" value={formatNumberNullable(disp.mean, 3)} />
              <MetricCell label="Std" value={formatNumberNullable(disp.std, 3)} />
              <MetricCell label="CV" value={formatNumberNullable(disp.cv, 3)} />
              <MetricCell label="P25" value={formatNumberNullable(disp.p25, 3)} />
              <MetricCell label="P50" value={formatNumberNullable(disp.p50, 3)} />
              <MetricCell label="P75" value={formatNumberNullable(disp.p75, 3)} />
              <MetricCell label="IQR" value={formatNumberNullable(disp.iqr, 3)} />
              <MetricCell label="Range" value={formatNumberNullable(disp.range, 3)} />
            </>
          ) : (
            <div className={styles.metricEmpty}>Dispersion 不可用</div>
          )}
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Latest Structural Events</dt>
        <dd className={styles.metricGrid}>
          {le ? (
            <>
              <MetricCell label="BOS up/down" value={pairStr(le.bos)} />
              <MetricCell label="CHOCH up/down" value={pairStr(le.choch)} />
              <MetricCell label="OB up/down" value={pairStr(le.ob)} />
              <MetricCell label="EQH" value={formatNumberNullable(le.eqh, 0)} />
              <MetricCell label="EQL" value={formatNumberNullable(le.eql, 0)} />
            </>
          ) : (
            <div className={styles.metricEmpty}>Latest events 不可用</div>
          )}
        </dd>
      </dl>
    </>
  )
}

// ============================================================
// D. Event Freshness
// ============================================================
function FreshnessGroup({ freshness }: { freshness: ScopeFreshnessFacts | null }) {
  if (!freshness) {
    return <div className={styles.panelUnavailable}>Event Freshness 不可用（observation 无 freshness）</div>
  }
  const density = (key: 'trend' | 'structure' | 'momentum' | 'chip'): string => {
    const d = freshness.by_dimension?.[key]
    return formatNumberNullable(d?.density, 3)
  }
  return (
    <dl className={styles.metricGroup}>
      <dt className={styles.metricHeading}>Event Freshness</dt>
      <dd className={styles.metricGrid}>
        <MetricCell label="Today" value={formatNumberNullable(freshness.today_count, 0)} title="today_count=0 是有效零事件" />
        <MetricCell label="5D" value={formatNumberNullable(freshness.last_5d_count, 0)} />
        <MetricCell label="10D" value={formatNumberNullable(freshness.last_10d_count, 0)} />
        <MetricCell label="20D" value={formatNumberNullable(freshness.last_20d_count, 0)} />
        <MetricCell label="Instruments" value={formatNumberNullable(freshness.instrument_count, 0)} />
        <MetricCell label="Decay ρ" value={formatNumberNullable(freshness.decay_weighted_density, 3)} title="中性分析值，不上色" />
        <MetricCell label="Trend ρ" value={density('trend')} />
        <MetricCell label="Structure ρ" value={density('structure')} />
        <MetricCell label="Momentum ρ" value={density('momentum')} />
        <MetricCell label="Chip ρ" value={density('chip')} />
        <MetricCell label="Trend n" value={formatNumberNullable(freshness.by_dimension?.trend?.event_count, 0)} />
        <MetricCell label="Structure n" value={formatNumberNullable(freshness.by_dimension?.structure?.event_count, 0)} />
        <MetricCell label="Momentum n" value={formatNumberNullable(freshness.by_dimension?.momentum?.event_count, 0)} />
        <MetricCell label="Chip n" value={formatNumberNullable(freshness.by_dimension?.chip?.event_count, 0)} />
      </dd>
    </dl>
  )
}

export default function ScopeCurrentSnapshotPanel({ current }: { current: ScopeCurrentSnapshot }) {
  return (
    <div className={styles.panel} data-panel="current">
      <RegimeGroup regime={current.regime} />
      <ParticipationGroup current={current} />
      <TechnicalGroup currentState={current.currentState} />
      <FreshnessGroup freshness={current.freshness} />
    </div>
  )
}
