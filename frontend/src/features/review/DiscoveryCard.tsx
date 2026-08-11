/** [V2] Discovery Card — single discovery in the list. */

import type { Discovery } from './types'
import styles from './review.module.scss'

interface Props {
  discovery: Discovery
  onClick: () => void
}

const LIFECYCLE_LABELS: Record<string, string> = {
  new: '新增',
  continuing: '持续',
  confirmed: '确认',
  weakened: '减弱',
  invalidated: '失效',
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
    <div className={styles.discoveryCard} onClick={onClick} role="button" tabIndex={0}
         onKeyDown={e => e.key === 'Enter' && onClick()}>
      <div className={styles.discoveryCardHeader}>
        <span className={styles.discoveryScopeType}>{scope.type}</span>
        <span className={styles.discoveryScopeName}>{scope.name}</span>
        <span className={`${styles.discoveryLifecycle} ${styles[`discoveryLifecycle${capitalize(lifecycle.status)}`] ?? ''}`}>
          {LIFECYCLE_LABELS[lifecycle.status] ?? lifecycle.status}
        </span>
      </div>

      {stateSummary && (
        <div className={styles.discoveryCardState}>
          <span className={styles.label}>状态</span> {stateSummary}
        </div>
      )}

      {changeSummary && (
        <div className={styles.discoveryCardChange}>
          <span className={styles.label}>变化</span> {changeSummary}
        </div>
      )}

      <div className={styles.discoveryCardEvidence}>
        {keyEvidence.slice(0, 4).map((e, i) => (
          <span key={i} className={styles.evidenceTag}>{e}</span>
        ))}
      </div>

      {lifecycle.duration > 0 && (
        <div className={styles.discoveryCardFooter}>
          <span>持续 {lifecycle.duration} 日</span>
          {rankKey && (
            <span className={styles.rankKey}>
              排序: {Object.entries(rankKey).filter(([,v]) => v > 0).slice(0,3)
                .map(([k]) => k).join(', ')}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s
}
