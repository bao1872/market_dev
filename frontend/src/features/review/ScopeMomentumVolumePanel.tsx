// [R3E-SLICE3] Momentum + Volume 详情页（纯 typed VM 渲染）。
//
// 唯一解析 owner = scopeMomentumVolumeContract。组件不自行解析 raw observation、
// 不得自建 formatter、不得重算 ratio/percentile/zscore、不得自制综合 score、
// 不得把 momentum_volume_relation 翻译成自创业务语义。
import { useMemo, type ReactNode } from 'react'
import {
  parseMomentumVolumeObservation,
  parseMomentumVolumeHistory,
  fmtSqueezeCategory,
  fmtRatioPct,
  type VolumeDistributionVM,
  type CurrentOnlyDistributionVM,
} from './scopeMomentumVolumeContract'
// 与 DSA 同一 gap helper（单一 owner）：null 处断开，绝不跨 gap 连线。
import { splitSeriesByGap } from './scopeDsaContract'
import {
  formatMultipleNullable,
  formatRawDimensionlessNullable,
  formatPercentileNullable,
  formatZScoreNullable,
  formatNumberNullable,
  NULL_DISPLAY,
} from './reviewFormat'
import type { ReviewScopeHistoryDTO, ReviewCrossSectionDTO } from './types'
import styles from './review.module.scss'

type Json = Record<string, unknown>

interface ScopeMomentumVolumePanelProps {
  observation: Json | null
  history: ReviewScopeHistoryDTO | null
  crossSection?: ReviewCrossSectionDTO | null
}

const STATE_COLORS: Record<string, string> = { Expanding: '#16a34a', Flat: '#9ca3af', Contracting: '#dc2626' }
const CHANGE_COLORS: Record<string, string> = { Enhancing: '#16a34a', Flat: '#9ca3af', Weakening: '#dc2626' }
const SQUEEZE_COLORS: Record<string, string> = { Squeeze: '#7c3aed', Squeeze_Release: '#2563eb', Non_Squeeze: '#9ca3af' }
const RELATION_COLORS = ['#2563eb', '#7c3aed', '#0891b2', '#d97706', '#db2777', '#16a34a', '#dc2626']

function pct(v: number | null | undefined): number {
  if (v == null || !Number.isFinite(v)) return 0
  return Math.max(0, Math.min(1, v))
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <div className={styles.panelTitle} style={{ marginTop: 16 }}>{children}</div>
}

interface CategoryRow {
  label: string
  count: number
  ratio: number | null
  color: string
}

function CategoryBlock({ title, rows, denominator }: { title: string; rows: CategoryRow[]; denominator: number | null }) {
  return (
    <div className={styles.mvBlock}>
      <div className={styles.mvBlockTitle}>{title}</div>
      <div className={styles.mvSqueezeGrid}>
        {rows.map((r) => (
          <div key={r.label} className={styles.mvSqueezeRow}>
            <span className={styles.mvSqueezeCat} style={{ color: r.color }}>{r.label}</span>
            <span className={styles.mvSqueezeRatio}>{r.ratio == null ? NULL_DISPLAY : fmtRatioPct(r.ratio)}</span>
            <span className={styles.mvSqueezeCount}>n={r.count}</span>
          </div>
        ))}
      </div>
      <div className={styles.mvDenominator}>有效成员数 = {denominator ?? NULL_DISPLAY}</div>
    </div>
  )
}

function CurrentOnlyTriple({ title, dist, unit }: { title: string; dist: CurrentOnlyDistributionVM | null; unit: 'raw' | 'multiple' }) {
  const fmt = unit === 'multiple' ? formatMultipleNullable : formatRawDimensionlessNullable
  if (!dist) return <div className={styles.mvBlock}><div className={styles.mvBlockTitle}>{title}</div><div className={styles.mvNeutral}>暂无事实</div></div>
  if (dist.unavailable) {
    return (
      <div className={styles.mvBlock}>
        <div className={styles.mvBlockTitle}>{title}</div>
        <div className={styles.mvUnavailable}>不可用{dist.reason ? `：${dist.reason}` : ''}（有效数为 0）</div>
      </div>
    )
  }
  return (
    <div className={styles.mvBlock}>
      <div className={styles.mvBlockTitle}>{title}</div>
      <div className={styles.mvMetricRow}>
        <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>P25</span><span className={styles.mvMetricValue}>{fmt(dist.p25)}</span></div>
        <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>Median</span><span className={styles.mvMetricValue}>{fmt(dist.median)}</span></div>
        <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>P75</span><span className={styles.mvMetricValue}>{fmt(dist.p75)}</span></div>
        <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>n</span><span className={styles.mvMetricValue}>{dist.validCount ?? NULL_DISPLAY}</span></div>
      </div>
    </div>
  )
}

function DistributionRow({ label, d, kind }: { label: string; d: VolumeDistributionVM | null; kind: 'ratio' | 'percentile' | 'zscore' }) {
  const fmt = kind === 'ratio' ? formatMultipleNullable : kind === 'percentile' ? formatPercentileNullable : formatZScoreNullable
  if (!d) return <div className={styles.mvMatrixRowLabel} style={{ opacity: 0.5 }}>{label}</div>
  return (
    <>
      <div className={styles.mvMatrixRowLabel}>{label}</div>
      <div className={styles.mvMatrixCell}>
        <span className={styles.mvMatrixPrimary}>{fmt(d.p25)}</span>
        <span className={styles.mvMatrixSub}>—</span>
      </div>
      <div className={styles.mvMatrixCell}>
        <span className={styles.mvMatrixPrimary}>{fmt(d.p50)}</span>
        <span className={styles.mvMatrixSub}>—</span>
      </div>
      <div className={styles.mvMatrixCell}>
        <span className={styles.mvMatrixPrimary}>{fmt(d.p75)}</span>
        <span className={styles.mvMatrixSub}>—</span>
      </div>
      <div className={styles.mvMatrixCell}>
        <span className={styles.mvMatrixPrimary}>{d.validCount ?? NULL_DISPLAY}</span>
      </div>
    </>
  )
}

// ---- mini line (history) ----
// null 处断开分段：每段独立 polyline，x 仍按原始 index 定位（保留 date slot，不压缩）。
// 禁止 forward fill / interpolation。
function MiniLine({ values, color }: { values: Array<number | null>; color: string }) {
  const w = 100
  const h = 30
  const present = values.filter((v): v is number => v != null)
  if (present.length === 0) return <div style={{ height: h, fontSize: 11, color: '#94a3b8' }}>无历史</div>
  const min = Math.min(...present)
  const max = Math.max(...present)
  const span = max - min || 1
  const n = values.length
  const xOf = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * w)
  const yOf = (v: number) => h - ((v - min) / span) * (h - 4) - 2
  const segments = splitSeriesByGap(values)
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: '100%', height: h, display: 'block' }}>
      {segments.map((seg, si) => (
        <polyline
          key={si}
          points={seg.map((p) => `${xOf(p.i).toFixed(1)},${yOf(p.v).toFixed(1)}`).join(' ')}
          fill="none"
          stroke={color}
          strokeWidth={1.5}
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  )
}

function HistoryRow({ label, values, color }: { label: string; values: Array<number | null>; color: string }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: 8, alignItems: 'center', marginBottom: 6 }}>
      <div style={{ fontSize: 12, color: '#475569' }}>{label}</div>
      <MiniLine values={values} color={color} />
    </div>
  )
}

// ---- aligned stacked composition multiples (history) ----
function CompositionMultiples({
  title,
  labels,
  colors,
  perDateRatios,
}: {
  title: string
  labels: string[]
  colors: string[]
  perDateRatios: Array<Array<number | null>>
}) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <div style={{ display: 'flex', gap: 1, height: 22, alignItems: 'stretch', background: '#f1f5f9' }}>
        {perDateRatios.map((ratios, i) => (
          <div key={i} style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column-reverse' }} title={labels.join(' / ')}>
            {ratios.map((r, j) => (
              <div key={j} style={{ height: `${pct(r) * 100}%`, background: colors[j] }} />
            ))}
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 10, fontSize: 11, color: '#475569', marginTop: 3 }}>
        {labels.map((l, j) => (
          <span key={l}><b style={{ color: colors[j] }}>{l}</b></span>
        ))}
      </div>
    </div>
  )
}

function fieldSeries(history: ReviewScopeHistoryDTO | null, key: string): Array<number | null> {
  return history?.fields?.[key]?.series ?? []
}

export default function ScopeMomentumVolumePanel({ observation, history, crossSection }: ScopeMomentumVolumePanelProps) {
  const vm = useMemo(() => parseMomentumVolumeObservation(observation), [observation])
  const historyVm = useMemo(() => parseMomentumVolumeHistory(history), [history])

  // 展示 ratio 由 typed VM 提供（contract 内派生），组件只 render。
  const changeRows: CategoryRow[] = (vm.change?.categories ?? []).map((c) => ({
    label: c.category,
    count: c.count,
    ratio: c.ratio,
    color: CHANGE_COLORS[c.category],
  }))

  const squeezeRows: CategoryRow[] = (vm.squeeze?.categories ?? []).map((c) => ({
    label: fmtSqueezeCategory(c.category),
    count: c.count,
    ratio: c.ratio,
    color: SQUEEZE_COLORS[c.category] ?? '#9ca3af',
  }))

  const stateRows: CategoryRow[] = (vm.state?.categories ?? []).map((c) => ({
    label: c.category,
    count: c.count,
    ratio: c.ratio,
    color: STATE_COLORS[c.category] ?? '#9ca3af',
  }))

  // relation categories (unknown preserved, no fixed enum / no business解释)
  const relationRows = vm.relation?.categories ?? []

  // volume matrix rows
  const vol = vm.volume

  // cross-sectional peer percentile（消费 canonical `field`，不另用 field 别名）
  const csFields = crossSection?.fields ?? []
  const CS_KEYS = ['momentum.bb_position', 'momentum.bb_width', 'participation.volume.ratio20', 'participation.volume.ratio200']
  const csMap = new Map(csFields.map((f) => [f.field, f]))

  return (
    <div className={styles.detailCard}>
      <div className={styles.panelTitle}>动量与量能（Momentum + Volume）</div>

      {/* 第一组：动量状态（同一行紧凑） */}
      <SectionTitle>动量状态</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
        <CategoryBlock title="Momentum State" rows={stateRows} denominator={vm.state?.denominator ?? null} />
        <CategoryBlock title="Momentum Change" rows={changeRows} denominator={vm.change?.denominator ?? null} />
        <CategoryBlock title="Squeeze State" rows={squeezeRows} denominator={vm.squeeze?.denominator ?? null} />
      </div>

      {/* 第二组：动量位置 */}
      <SectionTitle>动量位置</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
        <CurrentOnlyTriple title="BB Position" dist={vm.bbPosition} unit="raw" />
        <CurrentOnlyTriple title="BB Width" dist={vm.bbWidth} unit="raw" />
        <CurrentOnlyTriple title="Release Volume Ratio" dist={vm.releaseVolumeRatio} unit="multiple" />
        <div className={styles.mvBlock}>
          <div className={styles.mvBlockTitle}>SqzMom</div>
          <div className={styles.mvMetricRow}>
            <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>Mean</span><span className={styles.mvMetricValue}>{vm.sqzmom?.mean == null ? NULL_DISPLAY : formatNumberNullable(vm.sqzmom.mean, 3)}</span></div>
            <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>valid n</span><span className={styles.mvMetricValue}>{vm.sqzmom?.validCount ?? NULL_DISPLAY}</span></div>
          </div>
        </div>
      </div>

      {/* 第三组：Momentum × Volume Relation（OPEN categorical） */}
      <SectionTitle>Momentum × Volume Relation</SectionTitle>
      {vm.relation == null ? (
        <div className={styles.mvNeutral}>暂无关系事实</div>
      ) : vm.relation.unavailable ? (
        <div className={styles.mvUnavailable}>关系事实不可用（有效成员数 = 0）</div>
      ) : relationRows.length === 0 ? (
        <div className={styles.mvNeutral}>无有效类别</div>
      ) : (
        <div className={styles.mvSqueezeGrid} style={{ maxWidth: 420 }}>
          {relationRows.map((r, i) => (
            <div key={r.category} className={styles.mvSqueezeRow}>
              <span className={styles.mvSqueezeCat} style={{ color: RELATION_COLORS[i % RELATION_COLORS.length] }}>{r.category}</span>
              <span className={styles.mvSqueezeRatio}>{fmtRatioPct(r.ratio)}</span>
              <span className={styles.mvSqueezeCount}>n={r.count}</span>
            </div>
          ))}
          <div className={styles.mvDenominator}>有效成员数 = {vm.relation.denominator ?? NULL_DISPLAY}</div>
        </div>
      )}

      {/* 第四组：成交量 dense matrix */}
      <SectionTitle>成交量</SectionTitle>
      <div className={styles.mvBlock}>
        <div className={styles.mvMatrix}>
          <div className={styles.mvMatrixHead} />
          <div className={styles.mvMatrixHead}>P25</div>
          <div className={styles.mvMatrixHead}>P50</div>
          <div className={styles.mvMatrixHead}>P75</div>
          <div className={styles.mvMatrixHead}>n</div>
          <DistributionRow label="成交量比" d={vol?.ratio20 ?? null} kind="ratio" />
          <DistributionRow label="成交量比(200)" d={vol?.ratio200 ?? null} kind="ratio" />
          <DistributionRow label="成交量分位" d={vol?.percentile20 ?? null} kind="percentile" />
          <DistributionRow label="成交量分位(200)" d={vol?.percentile200 ?? null} kind="percentile" />
          <DistributionRow label="成交量Z" d={vol?.zscore20 ?? null} kind="zscore" />
          <DistributionRow label="成交量Z(200)" d={vol?.zscore200 ?? null} kind="zscore" />
        </div>
        <div className={styles.mvNote}>
          成交量比 → ×；成交量分位 → 原值（0–100）；成交量 Z → 原始 z。Ratio/Percentile/ZScore 单位不混。
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 8 }}>
        <div className={styles.mvBlock}>
          <div className={styles.mvBlockTitle}>Ratio Mean（仅 canonical mean）</div>
          <div className={styles.mvMetricRow}>
            <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>Ratio20</span><span className={styles.mvMetricValue}>{vm.ratio20Mean == null ? NULL_DISPLAY : formatMultipleNullable(vm.ratio20Mean)}</span></div>
            <div className={styles.mvMetric}><span className={styles.mvMetricLabel}>Ratio200</span><span className={styles.mvMetricValue}>{vm.ratio200Mean == null ? NULL_DISPLAY : formatMultipleNullable(vm.ratio200Mean)}</span></div>
          </div>
        </div>
        <div className={styles.mvBlock}>
          <div className={styles.mvBlockTitle}>Badge</div>
          {vm.volumeBadge ? (
            <div className={styles.mvSqueezeGrid}>
              {vm.volumeBadge.entries.map((e) => (
                <div key={e.category} className={styles.mvSqueezeRow}>
                  <span className={styles.mvSqueezeCat}>{e.category}</span>
                  <span className={styles.mvSqueezeRatio}>{fmtRatioPct(e.ratio)}</span>
                  <span className={styles.mvSqueezeCount}>n={e.count}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className={styles.mvNeutral}>暂无 Badge</div>
          )}
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 8 }}>
        {vm.percentile20Histogram && (
          <div className={styles.mvBlock}>
            <div className={styles.mvBlockTitle}>Percentile Histogram（20D）</div>
            <Histogram bins={vm.percentile20Histogram.bins} />
          </div>
        )}
        {vm.percentile200Histogram && (
          <div className={styles.mvBlock}>
            <div className={styles.mvBlockTitle}>Percentile Histogram（200D）</div>
            <Histogram bins={vm.percentile200Histogram.bins} />
          </div>
        )}
      </div>

      {/* 跨截面位置（轻量，消费 canonical field） */}
      <SectionTitle>横截面位置（peer percentile）</SectionTitle>
      {csFields.length === 0 ? (
        <div className={styles.mvNeutral}>无横截面位置证据（market scope 无 peer）</div>
      ) : (
        <div className={styles.mvSqueezeGrid} style={{ maxWidth: 620 }}>
          {CS_KEYS.map((key) => {
            const f = csMap.get(key)
            if (!f) {
              return (
                <div key={key} className={styles.mvSqueezeRow}>
                  <span className={styles.mvSqueezeCat}>{key}</span>
                  <span className={styles.mvSqueezeRatio}>{NULL_DISPLAY}</span>
                </div>
              )
            }
            // unavailable 不得被吞掉：明确展示 unavailable + canonical reason。
            if (f.status === 'unavailable') {
              return (
                <div key={key} className={styles.mvSqueezeRow}>
                  <span className={styles.mvSqueezeCat}>{key}</span>
                  <span className={styles.mvSqueezeRatio} title={f.reason ?? undefined}>
                    不可用{f.reason ? ` · ${f.reason}` : ''}
                  </span>
                  <span className={styles.mvSqueezeCount}>valid peers {f.valid_peer_count} / {f.peer_count}</span>
                </div>
              )
            }
            return (
              <div key={key} className={styles.mvSqueezeRow}>
                <span className={styles.mvSqueezeCat}>{key}</span>
                <span className={styles.mvSqueezeRatio}>P{f.percentile == null ? NULL_DISPLAY : formatNumberNullable(f.percentile, 1)}</span>
                <span className={styles.mvSqueezeCount}>valid peers {f.valid_peer_count} / {f.peer_count}</span>
              </div>
            )
          })}
        </div>
      )}

      {/* 历史 UI 1：Momentum State / Change / Squeeze 三个 aligned small multiples */}
      <SectionTitle>20D 动量状态构成（State / Change / Squeeze）</SectionTitle>
      <CompositionMultiples
        title="Momentum State"
        labels={['Expanding', 'Flat', 'Contracting']}
        colors={[STATE_COLORS.Expanding, STATE_COLORS.Flat, STATE_COLORS.Contracting]}
        perDateRatios={historyVm.momentumState.map((e) => [
          e.vm?.categories.find((c) => c.category === 'Expanding')?.ratio ?? null,
          e.vm?.categories.find((c) => c.category === 'Flat')?.ratio ?? null,
          e.vm?.categories.find((c) => c.category === 'Contracting')?.ratio ?? null,
        ])}
      />
      <CompositionMultiples
        title="Momentum Change"
        labels={['Enhancing', 'Flat', 'Weakening']}
        colors={[CHANGE_COLORS.Enhancing, CHANGE_COLORS.Flat, CHANGE_COLORS.Weakening]}
        perDateRatios={historyVm.momentumChange.map((e) => [
          e.vm?.categories.find((c) => c.category === 'Enhancing')?.ratio ?? null,
          e.vm?.categories.find((c) => c.category === 'Flat')?.ratio ?? null,
          e.vm?.categories.find((c) => c.category === 'Weakening')?.ratio ?? null,
        ])}
      />
      <CompositionMultiples
        title="Squeeze State"
        labels={['Squeeze', 'Squeeze_Release', 'Non_Squeeze']}
        colors={[SQUEEZE_COLORS.Squeeze, SQUEEZE_COLORS.Squeeze_Release, SQUEEZE_COLORS.Non_Squeeze]}
        perDateRatios={historyVm.squeezeState.map((e) => [
          e.vm?.categories.find((c) => c.category === 'Squeeze')?.ratio ?? null,
          e.vm?.categories.find((c) => c.category === 'Squeeze_Release')?.ratio ?? null,
          e.vm?.categories.find((c) => c.category === 'Non_Squeeze')?.ratio ?? null,
        ])}
      />

      {/* 历史 UI 2：BB Position / BB Width（复用 history.fields，不共享 Y 轴） */}
      <SectionTitle>20D BB Position / BB Width</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <HistoryRow label="BB Position" values={fieldSeries(history, 'bb_position')} color="#0ea5e9" />
        <HistoryRow label="BB Width" values={fieldSeries(history, 'bb_width')} color="#8b5cf6" />
      </div>

      {/* 历史 UI 3：Release Volume Ratio（独立 mini chart，member-first median） */}
      <SectionTitle>20D Release Volume Ratio</SectionTitle>
      <HistoryRow label="Release Volume Ratio" values={historyVm.releaseVolumeRatio.map((e) => e.vm?.median ?? null)} color="#f59e0b" />

      {/* 历史 UI 4：Volume（Ratio20/200 + ZScore20/200 复用 history.fields；Percentile20/200 用 momentumVolume 投影） */}
      <SectionTitle>20D 成交量（Ratio / ZScore / Percentile）</SectionTitle>
      <HistoryRow label="Volume Ratio20" values={fieldSeries(history, 'volume_ratio20')} color="#2563eb" />
      <HistoryRow label="Volume Ratio200" values={fieldSeries(history, 'volume_ratio200')} color="#1d4ed8" />
      <HistoryRow label="Volume ZScore20" values={fieldSeries(history, 'volume_zscore20')} color="#0891b2" />
      <HistoryRow label="Volume ZScore200" values={fieldSeries(history, 'volume_zscore200')} color="#0e7490" />
      <HistoryRow label="Volume Percentile20" values={historyVm.volumePercentile20.map((e) => e.vm?.p50 ?? null)} color="#16a34a" />
      <HistoryRow label="Volume Percentile200" values={historyVm.volumePercentile200.map((e) => e.vm?.p50 ?? null)} color="#15803d" />

      {/* 历史 UI 5：SqzMom Mean */}
      <SectionTitle>20D SqzMom Mean</SectionTitle>
      <HistoryRow label="SqzMom Mean" values={historyVm.sqzmomMean.map((e) => e.mean)} color="#db2777" />
    </div>
  )
}

function Histogram({ bins }: { bins: { label: string; count: number }[] }) {
  const max = Math.max(1, ...bins.map((b) => b.count))
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 48 }}>
      {bins.map((b) => (
        <div key={b.label} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
          <div style={{ width: '100%', height: `${(b.count / max) * 36}px`, background: '#94a3b8', borderRadius: 2 }} title={`${b.label}: ${b.count}`} />
          <span style={{ fontSize: 9, color: '#64748b' }}>{b.label}</span>
        </div>
      ))}
    </div>
  )
}
