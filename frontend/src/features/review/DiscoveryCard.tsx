/** [V2] Discovery Card — single discovery in the list. */

import type { Discovery } from './types'

interface Props {
  discovery: Discovery
  onClick: () => void
}

export function DiscoveryCard({ discovery, onClick }: Props) {
  const { scope, state, change, keyEvidence, lifecycle, rankKey } = discovery

  // State summary: strongest metric direction
  const stateSummary = Object.entries(state.metrics)
    .filter(([, m]) => m.value !== null)
    .map(([code, m]) => `${code}:${m.historyPercentile?.toFixed(0) || '?'}%`)
    .join(' | ')

  // Change summary
  const changeSummary = Object.entries(change.metrics)
    .filter(([, m]) => m.delta1d !== null && Math.abs(m.delta1d!) > 0.5)
    .map(([code, m]) => `${code}${m.delta1d! > 0 ? '↑' : '↓'}${Math.abs(m.delta1d!).toFixed(1)}`)
    .join(' ')

  return (
    <div className="discovery-card" onClick={onClick} role="button" tabIndex={0}
         onKeyDown={e => e.key === 'Enter' && onClick()}>
      <div className="discovery-card-header">
        <span className="discovery-scope-type">{scope.type}</span>
        <span className="discovery-scope-name">{scope.name}</span>
        <span className={`discovery-lifecycle ${lifecycle.status}`}>
          {lifecycle.status === 'new' && '新增'}
          {lifecycle.status === 'continuing' && '持续'}
          {lifecycle.status === 'confirmed' && '确认'}
          {lifecycle.status === 'weakened' && '减弱'}
          {lifecycle.status === 'invalidated' && '失效'}
        </span>
      </div>

      {stateSummary && (
        <div className="discovery-card-state">
          <span className="label">状态</span> {stateSummary}
        </div>
      )}

      {changeSummary && (
        <div className="discovery-card-change">
          <span className="label">变化</span> {changeSummary}
        </div>
      )}

      <div className="discovery-card-evidence">
        {keyEvidence.slice(0, 4).map((e, i) => (
          <span key={i} className="evidence-tag">{e}</span>
        ))}
      </div>

      {lifecycle.duration > 0 && (
        <div className="discovery-card-footer">
          <span>持续 {lifecycle.duration} 日</span>
          {rankKey && (
            <span className="rank-key">
              排序: {Object.entries(rankKey).filter(([,v]) => v > 0).slice(0,3)
                .map(([k]) => k).join(', ')}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
