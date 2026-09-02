// [AuctionScopeDetail] - 描述: V3.2 Selected Scope Detail（五组 + diagnostics）
//
// 硬契约：只展示后端 payload，绝不重算。
// historical_dynamics 后端仅暴露 latest 标量（无历史时间序列），因此以当前态读数呈现，
// 不臆造时间序列图（那会是 false-green）。
import type { AuctionScopeDetailOut } from './types'
import { formatRatioAsPercent, formatNumber } from './auctionScopeViewModel'
import styles from './auction.module.scss'

interface Props {
  detail: AuctionScopeDetailOut | null | undefined
  loading?: boolean
  error?: string | null
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'up' | 'down' | 'neutral'
}) {
  const toneCls =
    tone === 'up' ? styles.up : tone === 'down' ? styles.down : undefined
  return (
    <div className={styles.kpiCard}>
      <span className={styles.kpiLabel}>{label}</span>
      <span className={`${styles.kpiValue} ${toneCls ?? ''}`}>{value}</span>
    </div>
  )
}

function gapTone(v: number | null): 'up' | 'down' | 'neutral' | undefined {
  if (v === null) return undefined
  if (v > 0) return 'up'
  if (v < 0) return 'down'
  return 'neutral'
}

function CrossBar({ label, value }: { label: string; value: number | null }) {
  const pct = value === null ? 0 : Math.max(0, Math.min(100, value))
  return (
    <div className={styles.crossBarRow}>
      <span className={styles.crossBarLabel}>{label}</span>
      <div className={styles.crossBarTrack}>
        <div className={styles.crossBarFill} style={{ width: `${pct}%` }} />
      </div>
      <span className={styles.crossBarValue}>{value === null ? '—' : value.toFixed(1)}</span>
    </div>
  )
}

export function AuctionScopeDetail({ detail, loading, error }: Props) {
  if (loading) {
    return (
      <div className={styles.stateBox}>
        <span className={styles.stateTitle}>加载板块明细…</span>
      </div>
    )
  }
  if (error) {
    return (
      <div className={styles.stateBox}>
        <span className={styles.stateTitle}>无法加载板块明细</span>
        <span className={styles.stateDesc}>{error}</span>
      </div>
    )
  }
  if (!detail) {
    return (
      <div className={styles.stateBox}>
        <span className={styles.stateTitle}>选择一个板块</span>
        <span className={styles.stateDesc}>
          从左侧列表点击任意板块，查看其重定价 / 历史动态 / 参与度 / 跨截面 / 成员贡献。
        </span>
      </div>
    )
  }

  const r = detail.repricing
  const h = detail.historical_dynamics
  const p = detail.participation
  const cs = detail.cross_sectional

  return (
    <div className={styles.detailScroll}>
      <div className={styles.detailHeader}>
        <h2 className={styles.detailTitle}>{detail.scope_name}</h2>
        <span className={styles.chip}>
          {detail.family === 'industry' ? '行业' : '概念'}
        </span>
        <span className={styles.metaValue}>{detail.trade_date}</span>
      </div>

      <section>
        <h3 className={styles.sectionTitle} title="Auction Gap / Amount 当前重定价结构">
          重定价 Repricing
        </h3>
        <div className={styles.kpiGrid}>
          <Metric label="高开幅度 EW" value={formatRatioAsPercent(r.equal_weight_gap)} tone={gapTone(r.equal_weight_gap)} />
          <Metric label="高开幅度 AW" value={formatRatioAsPercent(r.amount_weighted_gap)} tone={gapTone(r.amount_weighted_gap)} />
          <Metric label="资金倾斜" value={formatNumber(r.capital_tilt)} tone={gapTone(r.capital_tilt)} />
          <Metric label="高开广度" value={formatNumber(r.positive_gap_breadth, 1)} />
          <Metric label="低开广度" value={formatNumber(r.negative_gap_breadth, 1)} />
          <Metric label="平开广度" value={formatNumber(r.unchanged_gap_breadth, 1)} />
          <Metric label="离散度" value={formatNumber(r.gap_dispersion)} />
          <Metric label="价格HHI" value={formatNumber(r.price_normalized_hhi, 3)} />
          <Metric label="有效样本" value={formatNumber(r.price_valid_count, 0)} />
        </div>
      </section>

      <section>
        <h3 className={styles.sectionTitle} title="EW Position/Velocity/Acceleration 当前态（后端仅暴露 latest 标量，无历史序列）">
          历史动态 Historical Dynamics（当前态）
        </h3>
        <div className={styles.kpiGrid}>
          <Metric label="EW 位置" value={formatNumber(h.position, 1)} />
          <Metric label="EW 速度" value={formatNumber(h.velocity)} />
          <Metric label="EW 加速度" value={formatNumber(h.acceleration)} />
          <Metric label="EMA 快" value={formatNumber(h.ema_fast)} />
          <Metric label="EMA 慢" value={formatNumber(h.ema_slow)} />
          <Metric label="信号" value={h.signal ?? '—'} />
          <Metric label="最新交易日" value={h.latest_trade_date ?? '—'} />
        </div>
      </section>

      <section>
        <h3 className={styles.sectionTitle} title="Auction Amount 参与度">
          参与度 Participation
        </h3>
        <div className={styles.kpiGrid}>
          <Metric label="金额位置" value={formatNumber(p.amount_position, 1)} />
          <Metric label="金额倍数" value={formatNumber(p.amount_multiple, 2)} />
          <Metric label="异常广度" value={formatNumber(p.amount_abnormal_breadth, 1)} />
          <Metric label="Top1 份额" value={formatNumber(p.top1_amount_share, 3)} />
          <Metric label="Top3 份额" value={formatNumber(p.top3_amount_share, 3)} />
          <Metric label="金额HHI" value={formatNumber(p.amount_normalized_hhi, 3)} />
        </div>
      </section>

      <section>
        <h3 className={styles.sectionTitle} title="同 family 跨截面位置（0–100，四轴独立，不压成综合分）">
          跨截面 Cross-sectional
        </h3>
        <div className={styles.crossBars}>
          <CrossBar label="重定价" value={cs.repricing ? (cs.repricing.equal_weight_gap ?? null) : null} />
          <CrossBar label="广度" value={cs.breadth ? (cs.breadth.positive_gap_breadth ?? null) : null} />
          <CrossBar label="参与" value={cs.participation ? (cs.participation.amount_historical_position ?? null) : null} />
          <CrossBar label="集中度" value={cs.concentration ? (cs.concentration.normalized_hhi ?? null) : null} />
        </div>
      </section>

      <section>
        <h3 className={styles.sectionTitle} title="EW/AW/Amount 三 owner 成员贡献">
          成员贡献 Member Attribution
        </h3>
        <div className={styles.attrTableWrap}>
          <table className={styles.scopeTable}>
            <thead>
              <tr>
                <th>个股</th>
                <th className={styles.numHead}>Gap</th>
                <th className={styles.numHead}>EW贡献</th>
                <th className={styles.numHead}>AW贡献</th>
                <th className={styles.numHead}>金额份额</th>
                <th className={styles.numHead}>竞价额</th>
              </tr>
            </thead>
            <tbody>
              {detail.member_attribution.members.map((m) => (
                <tr key={m.instrument_id}>
                  <td>{m.instrument_id}</td>
                  <td className={`${styles.numCell} ${gapTone(m.gap_ratio)}`}>
                    {formatRatioAsPercent(m.gap_ratio)}
                  </td>
                  <td className={styles.numCell}>{formatNumber(m.ew_contribution)}</td>
                  <td className={styles.numCell}>{formatNumber(m.aw_contribution)}</td>
                  <td className={styles.numCell}>{formatNumber(m.amount_share, 3)}</td>
                  <td className={styles.numCell}>{formatNumber(m.auction_amount, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <details className={styles.diagnostics}>
        <summary>Diagnostics</summary>
        <pre className={styles.diagnosticsPre}>
          {JSON.stringify(
            { scope_key: detail.scope_key, family: detail.family, ...detail.diagnostics },
            null,
            2,
          )}
        </pre>
      </details>
    </div>
  )
}
