/** [V2] Rank Key — explainable ranking dimensions. */

import type { DiscoveryRankKey } from './types'

interface Props {
  rankKey: DiscoveryRankKey
}

const LABELS: Record<string, string> = {
  anomaly: '异常度',
  change: '变化度',
  evidenceConsistency: '证据一致性',
  crossScopeConfirmation: '跨范围确认',
  coverage: '覆盖率',
  duration: '持续时间',
  breadth: '参与宽度',
}

export function RankKeyPanel({ rankKey }: Props) {
  const entries = Object.entries(rankKey).filter(([, v]) => v > 0)

  if (entries.length === 0) return null

  return (
    <section className="discovery-section rank-key">
      <h3>排序依据 (Rank Key)</h3>
      <div className="rank-key-bars">
        {entries.map(([key, value]) => (
          <div key={key} className="rank-key-item">
            <span className="rank-key-label">{LABELS[key] || key}</span>
            <div className="rank-key-bar">
              <div
                className="rank-key-fill"
                style={{ width: `${Math.min(value / 40 * 100, 100)}%` }}
              />
            </div>
            <span className="rank-key-value">{value.toFixed(1)}</span>
          </div>
        ))}
      </div>
    </section>
  )
}
