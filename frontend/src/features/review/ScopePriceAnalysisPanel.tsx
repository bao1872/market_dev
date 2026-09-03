// [SLICE 4 / PRICE] 涨跌幅分析页（纯 typed VM 渲染）。
//
// 唯一解析 owner = scopePriceAnalysisContract。本组件：
// - 不自行解析 canonical payload（解析 owner 在 contract）；
// - 不重算 EW/AW/rolling mean/variance/std/zscore/Capital Tilt（不得由 AW 与 EW 相减得到）/migration；
// - 不输出多空评分 / 买卖建议 / 自创综合分 / 阶段标签；
// - 所有单位换算来自 typed formatter（组件不做业务单位换算）；
// - 缺失保持 gap（null 断线，不插值、不 forward-fill）。
import { useMemo, type ReactNode } from 'react'
// 只消费 typed VM（含已格式化的展示串）；单位换算一律在 contract 内，组件不做业务单位换算。
import {
  parsePriceAnalysis,
  type PriceAnalysisVM,
} from './scopePriceAnalysisContract'
import { NULL_DISPLAY, displayMember, type MemberDirectory } from './reviewFormat'
import { splitSeriesByGap } from './scopeDsaContract'
import type { ScopeDynamicsParsed } from './scopeDetailContract'
import type { ScopeInternalParsed } from './scopeDetailContract'
import DynamicsCharts, { type DynamicsChartConfig } from './ScopeDynamicsCharts'
import {
  buildSharedTradingDates,
  alignToSharedDomain,
} from './scopeDynamicsChart'
import type {
  ReviewCrossSectionDTO,
  ReviewScopeHistoryDTO,
} from './types'
import styles from './review.module.scss'

type Json = Record<string, unknown>

interface ScopePriceAnalysisPanelProps {
  observation: Json | null
  history: ReviewScopeHistoryDTO | null
  dynamics: ScopeDynamicsParsed | null
  internal: ScopeInternalParsed | null
  crossSection: ReviewCrossSectionDTO | null
  memberDirectory: MemberDirectory | null
}

const EW_COLOR = '#2563eb'
const MEAN_COLOR = '#98A1B3'
const BAND_COLOR = 'rgba(37,99,235,0.14)'
const AW_COLOR = '#7c3aed'
const TILT_COLOR = '#0891b2'
const DISP_COLOR = '#d97706'
// [P2-B] A 股展示合同：positive 红 / negative 绿；走平 neutral。
const ADV_COLOR = '#dc2626' // 上涨 → 红
const DEC_COLOR = '#16a34a' // 下跌 → 绿
const UNC_COLOR = '#9ca3af' // 走平 → neutral
const JACCARD_COLOR = '#2563eb'
const MIGRATION_COLOR = '#db2777'

function SectionTitle({ children }: { children: ReactNode }) {
  return <div className={styles.panelTitle} style={{ marginTop: 16 }}>{children}</div>
}

function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.mvMetric}>
      <span className={styles.mvMetricLabel}>{label}</span>
      <span className={styles.mvMetricValue}>{value}</span>
    </div>
  )
}

const EMPTY_DATES: string[] = []

/** 仅用于把已确认非 null 的 canonical ratio 收敛到 [0,1] 画高；绝不把 null 当 0。 */
function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(1, v))
}

// ---------------------------------------------------------------------------
// gap-safe SVG renderers（null 断线，复用 DSA splitSeriesByGap）
// ---------------------------------------------------------------------------

function LineChart({
  series,
  height = 120,
}: {
  series: Array<{ values: Array<number | null>; color: string; dashed?: boolean }>
  height?: number
}) {
  const w = 100
  const all = series.flatMap((s) => s.values.filter((v): v is number => v != null))
  if (all.length === 0) {
    return <div style={{ height, fontSize: 11, color: '#94a3b8' }}>无历史</div>
  }
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  const n = Math.max(...series.map((s) => s.values.length))
  const xOf = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * w)
  const yOf = (v: number) => height - ((v - min) / span) * (height - 8) - 4
  return (
    <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height, display: 'block' }}>
      {series.map((s, si) =>
        splitSeriesByGap(s.values).map((seg, gi) => (
          <polyline
            key={`${si}-${gi}`}
            points={seg.map((p) => `${xOf(p.i).toFixed(2)},${yOf(p.v).toFixed(2)}`).join(' ')}
            fill="none"
            stroke={s.color}
            strokeWidth={1.5}
            strokeDasharray={s.dashed ? '3 2' : undefined}
            vectorEffect="non-scaling-stroke"
          />
        )),
      )}
    </svg>
  )
}

/** Mean ± 1σ band（绝不 mean ± variance） */
function BandChart({
  ew,
  mean,
  upper,
  lower,
  height = 160,
}: {
  ew: Array<number | null>
  mean: Array<number | null>
  upper: Array<number | null>
  lower: Array<number | null>
  height?: number
}) {
  const w = 100
  const all = [...ew, ...mean, ...upper, ...lower].filter((v): v is number => v != null)
  if (all.length === 0) {
    return <div style={{ height, fontSize: 11, color: '#94a3b8' }}>无历史</div>
  }
  const min = Math.min(...all)
  const max = Math.max(...all)
  const span = max - min || 1
  const n = ew.length
  const xOf = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * w)
  const yOf = (v: number) => height - ((v - min) / span) * (height - 8) - 4

  // 连续 run：upper 与 lower 同时非 null 才成带
  const bands: string[] = []
  let run: number[] = []
  const flush = () => {
    if (run.length >= 2) {
      const up = run.map((i) => `${xOf(i).toFixed(2)},${yOf(upper[i] as number).toFixed(2)}`)
      const lo = [...run].reverse().map((i) => `${xOf(i).toFixed(2)},${yOf(lower[i] as number).toFixed(2)}`)
      bands.push([...up, ...lo].join(' '))
    }
    run = []
  }
  for (let i = 0; i < n; i++) {
    if (upper[i] != null && lower[i] != null) run.push(i)
    else flush()
  }
  flush()

  return (
    <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height, display: 'block' }}>
      {bands.map((pts, i) => (
        <polygon key={`band-${i}`} points={pts} fill={BAND_COLOR} stroke="none" />
      ))}
      {[upper, lower].map((s, si) =>
        splitSeriesByGap(s).map((seg, gi) => (
          <polyline
            key={`ul-${si}-${gi}`}
            points={seg.map((p) => `${xOf(p.i).toFixed(2)},${yOf(p.v).toFixed(2)}`).join(' ')}
            fill="none"
            stroke={MEAN_COLOR}
            strokeWidth={1}
            strokeDasharray="3 2"
            vectorEffect="non-scaling-stroke"
          />
        )),
      )}
      {splitSeriesByGap(mean).map((seg, gi) => (
        <polyline
          key={`mean-${gi}`}
          points={seg.map((p) => `${xOf(p.i).toFixed(2)},${yOf(p.v).toFixed(2)}`).join(' ')}
          fill="none"
          stroke={MEAN_COLOR}
          strokeWidth={1.5}
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {splitSeriesByGap(ew).map((seg, gi) => (
        <polyline
          key={`ew-${gi}`}
          points={seg.map((p) => `${xOf(p.i).toFixed(2)},${yOf(p.v).toFixed(2)}`).join(' ')}
          fill="none"
          stroke={EW_COLOR}
          strokeWidth={1.8}
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  )
}

/**
 * 100% composition timeline（Breadth：Advance / Decline / Unchanged）。
 *
 * [P2-A] 三项本身就是 canonical ratio，直接使用、不再归一化。
 * 任一项 unavailable → 该日期列显示 gap（不插值、不把 null 当 0、
 * 不得「null→0 后重新归一化剩下两项」）。
 */
function CompositionTimeline({
  points,
  height = 90,
}: {
  points: Array<{ advance: number | null; decline: number | null; unchanged: number | null }>
  height?: number
}) {
  return (
    <div style={{ display: 'flex', gap: 1, height, alignItems: 'stretch', background: '#f1f5f9' }}>
      {points.map((p, i) => {
        const ready = p.advance != null && p.decline != null && p.unchanged != null
        if (!ready) {
          return (
            <div
              key={i}
              style={{ flex: 1, minWidth: 0 }}
              title="该日期 breadth 不可用（保留 gap，不插值）"
              data-breadth-gap="true"
            />
          )
        }
        return (
          <div key={i} style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column-reverse' }}>
            <div style={{ height: `${clamp01(p.advance as number) * 100}%`, background: ADV_COLOR }} />
            <div style={{ height: `${clamp01(p.unchanged as number) * 100}%`, background: UNC_COLOR }} />
            <div style={{ height: `${clamp01(p.decline as number) * 100}%`, background: DEC_COLOR }} />
          </div>
        )
      })}
    </div>
  )
}

function Legend({ items }: { items: Array<{ label: string; color: string }> }) {
  return (
    <div style={{ display: 'flex', gap: 10, fontSize: 11, color: '#475569', marginTop: 4 }}>
      {items.map((it) => (
        <span key={it.label}>
          <b style={{ color: it.color }}>{it.label}</b>
        </span>
      ))}
    </div>
  )
}

export default function ScopePriceAnalysisPanel({
  observation,
  history,
  dynamics,
  internal,
  crossSection,
  memberDirectory,
}: ScopePriceAnalysisPanelProps) {
  // 稳定空数组常量：避免 useMemo 依赖因每次新建 [] 而变化
  const dates = history?.dates ?? EMPTY_DATES
  const currentTilt = internal?.capitalTilt?.capitalTilt ?? null
  const vm: PriceAnalysisVM = useMemo(
    () => parsePriceAnalysis({ dates, observation, history, currentTilt, crossSection }),
    [dates, observation, history, currentTilt, crossSection],
  )

  // Dynamics 三图：只做输入适配（复用 ScopeDynamicsPanel 同一共享 renderer），绝不重算。
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
  const dynamicsConfigs: DynamicsChartConfig[] = [
    { key: 'position', title: 'Position', data: positionData, kind: 'position', showZeroLine: false, showTimeAxis: false },
    { key: 'velocity', title: 'Velocity', data: velocityData, kind: 'offset', showZeroLine: true, showTimeAxis: false },
    { key: 'acceleration', title: 'Acceleration', data: accelerationData, kind: 'offset', showZeroLine: true, showTimeAxis: true },
  ]

  const r = vm.rolling

  return (
    <div className={styles.detailCard}>
      <div className={styles.panelTitle}>涨跌幅分析（Price）</div>

      {/* 第一段：价格运动与历史异常度 */}
      <SectionTitle>价格运动与历史异常度</SectionTitle>
      <BandChart ew={vm.ewChart.ew} mean={vm.ewChart.mean} upper={vm.ewChart.upperBand} lower={vm.ewChart.lowerBand} />
      <Legend
        items={[
          { label: 'EW Raw', color: EW_COLOR },
          { label: '20D Mean', color: MEAN_COLOR },
          { label: 'Mean ± 1σ', color: '#64748b' },
        ]}
      />
      <div className={styles.mvMetricRow} style={{ marginTop: 8 }}>
        <StatRow label="Current EW" value={r?.currentText ?? NULL_DISPLAY} />
        <StatRow label="20D Mean" value={r?.mean20Text ?? NULL_DISPLAY} />
        <StatRow label="20D Variance" value={r?.variance20Text ?? NULL_DISPLAY} />
        <StatRow label="20D Std" value={r?.std20Text ?? NULL_DISPLAY} />
        <StatRow label="Z" value={r?.zscore20Text ?? NULL_DISPLAY} />
        <StatRow label="Baseline n" value={r?.baselineCount == null ? NULL_DISPLAY : String(r.baselineCount)} />
      </div>
      <div className={styles.mvNote}>
        Baseline 为 T 前最多 20 个 observation（不含 T）；Variance 为 decimal-return² 展示为 %²；
        带宽为 Mean ± Std，不是 Mean ± Variance。
      </div>

      {/* 第二段：Historical Position（仅三图，不展示阶段标签） */}
      <SectionTitle>Historical Position</SectionTitle>
      {dynamics ? (
        <>
          <DynamicsCharts configs={dynamicsConfigs} />
          {/* P1-F / P1-3：共享 renderer 关闭了 attributionLogo（false），
              因此每个复用它的页面都必须保留真实 TradingView 许可归属 <a>。
              与 ScopeDynamicsPanel 同一合同，不得省略。 */}
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
        </>
      ) : (
        <div className={styles.mvNeutral}>本期暂无等权涨跌动态数据</div>
      )}
      <div className={styles.mvNote}>
        Position 为自身历史分位（0–100），与下方横截面 peer percentile 是不同的语义，不得混为一个“分位”。
      </div>

      {/* 第三段：内部成员同步 */}
      <SectionTitle>内部成员同步</SectionTitle>
      <div style={{ fontSize: 12, color: '#475569', marginBottom: 4 }}>
        Current：Advance {vm.current.advanceRatioText} · Decline {vm.current.declineRatioText} ·
        Unchanged {vm.current.unchangedRatioText} · Dispersion {vm.current.returnDispersionText}
      </div>
      <CompositionTimeline points={vm.breadth.points} />
      <Legend
        items={[
          { label: 'Advance', color: ADV_COLOR },
          { label: 'Unchanged', color: UNC_COLOR },
          { label: 'Decline', color: DEC_COLOR },
        ]}
      />
      <div style={{ marginTop: 10, fontSize: 12, color: '#475569' }}>Return Dispersion</div>
      <LineChart series={[{ values: vm.dispersion.values, color: DISP_COLOR }]} height={70} />

      {/* 第四段：成交权重影响 */}
      <SectionTitle>成交权重影响</SectionTitle>
      <LineChart
        series={[
          { values: vm.ew.values, color: EW_COLOR },
          { values: vm.aw.values, color: AW_COLOR },
        ]}
        height={110}
      />
      <Legend items={[{ label: 'EW', color: EW_COLOR }, { label: 'AW', color: AW_COLOR }]} />
      <div style={{ marginTop: 10, fontSize: 12, color: '#475569' }}>
        Capital Tilt（persisted Composition fact，非 AW 与 EW 相减所得）
      </div>
      <LineChart series={[{ values: vm.capitalTilt.values, color: TILT_COLOR }]} height={80} />
      <div className={styles.mvMetricRow} style={{ marginTop: 6 }}>
        <StatRow label="Current Tilt" value={vm.capitalTilt.currentText} />
        <StatRow label="Current EW" value={vm.capitalTilt.currentEwText} />
        <StatRow label="Current AW" value={vm.capitalTilt.currentAwText} />
      </div>

      {/* 第五段：Leadership */}
      <SectionTitle>Leadership</SectionTitle>
      <div style={{ fontSize: 12, color: '#475569' }}>Jaccard Stability</div>
      <LineChart
        series={[{ values: vm.leadership.points.map((p) => p.jaccardStability), color: JACCARD_COLOR }]}
        height={70}
      />
      <div style={{ fontSize: 12, color: '#475569', marginTop: 6 }}>Migration</div>
      <LineChart
        series={[{ values: vm.leadership.points.map((p) => p.migration), color: MIGRATION_COLOR }]}
        height={70}
      />
      <div style={{ marginTop: 10, fontSize: 12, color: '#475569' }}>Leader Set Timeline</div>
      <div className={styles.mvSqueezeGrid} style={{ maxWidth: 720 }}>
        {vm.leadership.points.length === 0 && <div className={styles.mvNeutral}>无 Leader 历史</div>}
        {vm.leadership.points.map((p) => (
          <div key={p.date} className={styles.mvSqueezeRow}>
            <span className={styles.mvSqueezeCat}>{p.date}</span>
            {p.unavailable ? (
              <span className={styles.mvSqueezeRatio} title={p.reason ?? undefined}>
                不可用{p.reason ? ` · ${p.reason}` : ''}
              </span>
            ) : (
              <>
                <span className={styles.mvSqueezeRatio}>n={p.currentLeaderCount ?? NULL_DISPLAY}</span>
                <span className={styles.mvSqueezeCount}>
                  {(p.currentLeaderIds ?? []).map((id) => displayMember(id, memberDirectory)).join(', ') || '空'}
                </span>
              </>
            )}
          </div>
        ))}
      </div>

      {/* 横截面（§十二：只允许已正式支持的 EW / AW，且完整消费 status/reason/peer counts） */}
      <SectionTitle>横截面位置（peer percentile）</SectionTitle>
      <div className={styles.mvSqueezeGrid} style={{ maxWidth: 620 }}>
        {vm.crossSection.length === 0 && <div className={styles.mvNeutral}>无横截面位置证据</div>}
        {vm.crossSection.map((c) => (
          <div key={c.field} className={styles.mvSqueezeRow}>
            <span className={styles.mvSqueezeCat}>{c.field}</span>
            <span className={styles.mvSqueezeRatio} title={c.reason ?? undefined}>{c.text}</span>
            <span className={styles.mvSqueezeCount}>
              valid peers {c.validPeerCount == null ? NULL_DISPLAY : c.validPeerCount} /{' '}
              {c.peerCount == null ? NULL_DISPLAY : c.peerCount}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
