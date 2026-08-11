/** [V2] State / Change / Anomaly panel — structured rendering (CR-04). */

import type { DiscoveryState, DiscoveryChange, DiscoveryAnomaly } from './types'

interface Props {
  state: DiscoveryState
  change: DiscoveryChange
  anomaly: DiscoveryAnomaly
}

export function StatePanel({ state, change, anomaly }: Props) {
  return (
    <section className="discovery-section state-panel">
      <h3>State / Change / Anomaly</h3>

      {/* State */}
      <div className="state-subsection">
        <h4>当前状态 (State)</h4>
        <table className="evidence-table">
          <thead>
            <tr><th>指标</th><th>值</th><th>历史分位</th><th>横截面分位</th></tr>
          </thead>
          <tbody>
            {Object.entries(state.metrics).map(([code, m]) => (
              <tr key={code}>
                <td>{code}</td>
                <td>{m.value?.toFixed(1) ?? '-'}</td>
                <td>{m.historyPercentile != null ? `${m.historyPercentile.toFixed(0)}%` : '-'}</td>
                <td>{m.crossSectionPercentile != null ? `${m.crossSectionPercentile.toFixed(0)}%` : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {state.concentration.hhi != null && (
          <div className="concentration-state">
            集中度: HHI={state.concentration.hhi?.toFixed(3)},
            Top5={state.concentration.top5Contribution?.toFixed(2)},
            Leader-Median Gap={state.concentration.leaderMedianGap?.toFixed(2)}
          </div>
        )}
        <div className="internal-structure">
          结构: 趋势宽度={state.internalStructure.trendBreadth?.toFixed(0)}%,
          结构宽度={state.internalStructure.structureBreadth?.toFixed(0)}%,
          动量宽度={state.internalStructure.momentumBreadth?.toFixed(0)}%,
          结构破坏扩散={state.internalStructure.structureBreakdownDiffusion?.toFixed(3)},
          同步改善={state.internalStructure.synchronizedImprovement ? '是' : '否'}
        </div>
      </div>

      {/* Change */}
      <div className="state-subsection">
        <h4>变化 (Change)</h4>
        <table className="evidence-table">
          <thead>
            <tr><th>指标</th><th>1日变化</th><th>5日变化</th></tr>
          </thead>
          <tbody>
            {Object.entries(change.metrics).map(([code, m]) => (
              <tr key={code}>
                <td>{code}</td>
                <td>{m.delta1d != null ? `${m.delta1d > 0 ? '+' : ''}${m.delta1d.toFixed(1)}` : '-'}</td>
                <td>{m.delta5d != null ? `${m.delta5d > 0 ? '+' : ''}${m.delta5d.toFixed(1)}` : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {change.concentration.direction && (
          <div className="concentration-change">
            集中度变化: {change.concentration.direction} (Δ{change.concentration.delta1d?.toFixed(1)})
          </div>
        )}
      </div>

      {/* Anomaly */}
      <div className="state-subsection">
        <h4>异常 (Anomaly)</h4>
        <table className="evidence-table">
          <thead>
            <tr><th>指标</th><th>自身历史分位</th><th>同类横截面分位</th></tr>
          </thead>
          <tbody>
            {Object.entries(anomaly.selfHistorical).map(([code, v]) => (
              <tr key={code}>
                <td>{code}</td>
                <td>{v != null ? `${v.toFixed(0)}%` : '-'}</td>
                <td>{anomaly.crossSectional[code] != null ? `${anomaly.crossSectional[code]!.toFixed(0)}%` : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
