// [ScopeDynamicsPanel] - 描述: Dynamics 研究面板（Slice E correction）
//
// 硬契约（prompt §2、§3、§4）：
// - 图表数据源 ONLY composition.historical_dynamics.scope_dynamics.historical_dynamics
//   （position/ema5/ema20/velocity/signal/acceleration/persistence 各自 fact-object 日期序列）。
// - 绝不前端重算 EMA/Velocity/Acceleration/Persistence/Phase。
// - 缺失观测 = whitespace 缺口（gap preservation），不填 0、不插值、不 carry。
// - Position 图固定 0–100 y 域；Velocity/Acceleration 含可见 0 参考线。
// - 当前事实来自末尾 dynamics_phase observation（persisted），绝不反推 chart series。
// - ready + phase=null → 显示 "—"，不是第七个 phase。
// - 三张图均有显式标题（Position / Velocity / Acceleration），neutral analytic 线色。
//
// [SLICE 4 / Price] 三图 renderer 已抽出为共享组件 ScopeDynamicsCharts（窄块抽取），
// 供本面板与 ScopePriceAnalysisPanel 复用；本面板行为完全不变，不复制整套 chart engine。
import { useMemo, type ReactNode } from 'react'
import type { ScopeDynamicsParsed } from './scopeDetailContract'
import type { ScopePhaseFact } from './types'
import {
  buildSharedTradingDates,
  alignToSharedDomain,
} from './scopeDynamicsChart'
import { currentPhaseFact } from './scopeDetailContract'
import {
  NULL_DISPLAY,
  formatNumberNullable,
  formatPercentNullable,
  formatPhaseLabel,
  formatPosition,
  formatReadiness,
} from './reviewFormat'
import ReviewTerm from './ReviewTerm'
import DynamicsCharts, { type DynamicsChartConfig } from './ScopeDynamicsCharts'
import styles from './review.module.scss'

function FactRow({ label, value }: { label: ReactNode; value: string }) {
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{label}</span>
      <span className={styles.factValue}>{value}</span>
    </div>
  )
}

function CurrentFactStrip({ phaseFact }: { phaseFact: ScopePhaseFact | null }) {
  const f = phaseFact
  return (
    <div className={styles.factStrip}>
      {/* [REVIEW-PRODUCT-CLOSURE-01 Phase G] 状态经 formatReadiness 中文化：
          ready → 可用，不展示原始 "ready" */}
      <FactRow label={<ReviewTerm termKey="status" compact />} value={formatReadiness(f?.status)} />
      <FactRow label={<ReviewTerm termKey="phaseCurrent" compact />} value={formatPhaseLabel(f?.phase)} />
      <FactRow label={<ReviewTerm termKey="position" compact />} value={formatPosition(f?.position)} />
      <FactRow label={<ReviewTerm termKey="velocity" compact />} value={formatNumberNullable(f?.velocity)} />
      <FactRow label={<ReviewTerm termKey="acceleration" compact />} value={formatNumberNullable(f?.acceleration)} />
      <FactRow
        label={<ReviewTerm termKey="upperOccupancy" compact />}
        value={f?.upper_occupancy === null || f?.upper_occupancy === undefined ? NULL_DISPLAY : formatPercentNullable(f.upper_occupancy)}
      />
      <FactRow
        label={<ReviewTerm termKey="lowerOccupancy" compact />}
        value={f?.lower_occupancy === null || f?.lower_occupancy === undefined ? NULL_DISPLAY : formatPercentNullable(f.lower_occupancy)}
      />
    </div>
  )
}

export default function ScopeDynamicsPanel({ dynamics }: { dynamics: ScopeDynamicsParsed | null }) {
  // [Dynamics shared timeline] 三图共用同一 trading-date domain：
  // 各 series 仍保留自己的 fact-object 日期与缺失语义，仅把 X 轴位置统一到并集 domain；
  // 缺失日期在该图上仍是 whitespace gap（不填 0 / 不插值 / 不 carry）。
  // Hooks 必须无条件调用（react-hooks/rules-of-hooks），即使 dynamics 为 null。
  const sharedDates = useMemo(
    () =>
      buildSharedTradingDates(
        dynamics?.positionDates ?? [],
        dynamics?.velocityDates ?? [],
        dynamics?.accelerationDates ?? [],
      ),
    [dynamics?.positionDates, dynamics?.velocityDates, dynamics?.accelerationDates],
  )
  const positionData = useMemo(
    () => alignToSharedDomain(sharedDates, dynamics?.positionDates ?? [], dynamics?.position ?? []),
    [sharedDates, dynamics?.positionDates, dynamics?.position],
  )
  const velocityData = useMemo(
    () => alignToSharedDomain(sharedDates, dynamics?.velocityDates ?? [], dynamics?.velocity ?? []),
    [sharedDates, dynamics?.velocityDates, dynamics?.velocity],
  )
  const accelerationData = useMemo(
    () => alignToSharedDomain(sharedDates, dynamics?.accelerationDates ?? [], dynamics?.acceleration ?? []),
    [sharedDates, dynamics?.accelerationDates, dynamics?.acceleration],
  )
  if (!dynamics) {
    return <div className={styles.panelUnavailable}>本期暂无等权涨跌动态数据</div>
  }
  const current = currentPhaseFact(dynamics)
  const configs: DynamicsChartConfig[] = [
    { key: 'position', title: <ReviewTerm termKey="position" compact />, data: positionData, kind: 'position', showZeroLine: false, showTimeAxis: false },
    { key: 'velocity', title: <ReviewTerm termKey="velocity" compact />, data: velocityData, kind: 'offset', showZeroLine: true, showTimeAxis: false },
    { key: 'acceleration', title: <ReviewTerm termKey="acceleration" compact />, data: accelerationData, kind: 'offset', showZeroLine: true, showTimeAxis: true },
  ]
  return (
    <div className={styles.panel} data-panel="dynamics">
      <DynamicsCharts configs={configs} />
      <CurrentFactStrip phaseFact={current} />
      {/* P1-F: TradingView Lightweight Charts 许可归属（图表 logo 已关闭，归属信息保留于此非干扰区域）。
          必须是真实 <a> 链接，不能仅为纯字符串；attributionLogo 仍保持 false（P0-3）。 */}
      <div className={styles.chartAttribution}>
        <a
          href="https://www.tradingview.com/"
          target="_blank"
          rel="noopener noreferrer"
          className={styles.chartAttributionLink}
        >
          Charts by TradingView Lightweight Charts
        </a>
      </div>
    </div>
  )
}
