/** [V2] Discovery Detail — scope, state/change/anomaly, evidence, relations, instruments.
 *
 * Detail 消费真实 Detail API 返回的 Discovery（deep link 独立于分页）。
 * 提供最小 Discovery/Scope 区分追踪：用户可区分「追踪此发现」与「追踪此范围」。
 */

import { useState } from 'react'
import type { Discovery } from './types'
import { createReviewTracking, extractReviewError } from './api'
import { StatePanel } from './StatePanel'
import { RelatedScopesPanel } from './RelatedScopesPanel'
import { InstrumentsPanel } from './InstrumentsPanel'
import { RankKeyPanel } from './RankKeyPanel'
import styles from './review.module.scss'

interface Props {
  discovery: Discovery
  onBack: () => void
  tradeDate: string
  showToast: (title: string, desc?: string) => void
}

const LIFECYCLE_LABELS: Record<string, string> = {
  new: '新增',
  continuing: '持续',
  confirmed: '确认',
  weakened: '减弱',
  invalidated: '失效',
}

export function DiscoveryDetail({ discovery, onBack, tradeDate, showToast }: Props) {
  const { scope, state, change, anomaly, keyEvidence, lifecycle, rankKey,
    relatedScopes, representativeInstruments, dataQuality, supportingSignalIds, discoveryId } = discovery
  const [tracking, setTracking] = useState<{ kind: string; pending: boolean }>({ kind: '', pending: false })

  const trackDiscovery = async () => {
    setTracking({ kind: 'discovery', pending: true })
    try {
      await createReviewTracking({
        tracking_type: 'discovery',
        discovery_id: discoveryId,
        scope_type: scope.type,
        scope_key: scope.key,
        note: `Discovery: ${scope.name} (${scope.type}/${scope.key}) @ ${tradeDate}`,
        idempotency_key: `disc-${discoveryId}`,
      })
      showToast('已加入追踪', '正在追踪此 Discovery')
    } catch (e) {
      const err = extractReviewError(e)
      showToast('追踪失败', err.message)
    } finally {
      setTracking({ kind: '', pending: false })
    }
  }

  const trackScope = async () => {
    setTracking({ kind: 'scope', pending: true })
    try {
      await createReviewTracking({
        tracking_type: 'scope',
        scope_type: scope.type,
        scope_key: scope.key,
        idempotency_key: `scope-${scope.type}-${scope.key}`,
      })
      showToast('已加入追踪', '正在追踪此范围')
    } catch (e) {
      const err = extractReviewError(e)
      showToast('追踪失败', err.message)
    } finally {
      setTracking({ kind: '', pending: false })
    }
  }

  return (
    <div className={styles.discoveryDetail}>
      {/* Header */}
      <div className={styles.discoveryDetailHeader}>
        <button type="button" onClick={onBack} className={styles.backBtn}>← 返回列表</button>
        <h2>{scope.name}</h2>
        <span className={styles.scopeTypeTag}>{scope.type}</span>
        <span className={`${styles.lifecycleTag} ${styles[`discoveryLifecycle${cap(lifecycle.status)}`] ?? ''}`}>
          {LIFECYCLE_LABELS[lifecycle.status] ?? lifecycle.status}
        </span>
        {lifecycle.duration > 0 && (
          <span className={styles.durationTag}>持续 {lifecycle.duration} 日</span>
        )}
        {lifecycle.firstSeen && (
          <span className={styles.firstSeenTag}>首次 {lifecycle.firstSeen}</span>
        )}
      </div>

      {/* Key Evidence */}
      <section className={styles.discoverySection}>
        <h3>关键证据</h3>
        <div className={styles.evidenceTags}>
          {keyEvidence.map((e, i) => (
            <span key={i} className={styles.evidenceTag}>{e}</span>
          ))}
        </div>
        <p className={styles.signalCount}>
          基于 {supportingSignalIds.length} 条 atomic signal evidence
        </p>
      </section>

      {/* Tracking actions：区分 Discovery target 与 Scope target */}
      <section className={styles.discoverySection}>
        <h3>追踪</h3>
        <div className={styles.trackingActions}>
          <button
            type="button"
            className={`${styles.trackingBtn} ${styles.primary}`}
            onClick={trackDiscovery}
            disabled={tracking.pending}
          >
            {tracking.kind === 'discovery' ? '加入中...' : '追踪此发现'}
          </button>
          <button
            type="button"
            className={styles.trackingBtn}
            onClick={trackScope}
            disabled={tracking.pending}
          >
            {tracking.kind === 'scope' ? '加入中...' : '追踪此范围'}
          </button>
        </div>
        <p className={styles.signalCount}>
          追踪「发现」保留 Discovery 身份；追踪「范围」仅按 scope_type/scope_key 追踪
        </p>
      </section>

      {/* State / Change / Anomaly */}
      <StatePanel state={state} change={change} anomaly={anomaly} />

      {/* Rank Key */}
      <RankKeyPanel rankKey={rankKey} />

      {/* Related Scopes */}
      {relatedScopes.length > 0 && (
        <RelatedScopesPanel relations={relatedScopes} />
      )}

      {/* Representative Instruments */}
      {representativeInstruments.length > 0 && (
        <InstrumentsPanel instruments={representativeInstruments} />
      )}

      {/* Data Quality */}
      <section className={`${styles.discoverySection} ${styles.dataQuality}`}>
        <h3>数据质量</h3>
        <span>覆盖率: {(dataQuality.coverage * 100).toFixed(1)}%</span>
        <span>readyCount: {dataQuality.readyCount}</span>
      </section>
    </div>
  )
}

function cap(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s
}
