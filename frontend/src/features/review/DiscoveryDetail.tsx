/** [V2] Discovery Detail — scope, state/change/anomaly, evidence, relations, instruments. */

import type { Discovery } from './types'
import { StatePanel } from './StatePanel'
import { RelatedScopesPanel } from './RelatedScopesPanel'
import { InstrumentsPanel } from './InstrumentsPanel'
import { RankKeyPanel } from './RankKeyPanel'

interface Props {
  discovery: Discovery
  onBack: () => void
  tradeDate: string
}

export function DiscoveryDetail({ discovery, onBack }: Props) {
  const { scope, state, change, anomaly, keyEvidence, lifecycle, rankKey,
    relatedScopes, representativeInstruments, dataQuality, supportingSignalIds } = discovery

  return (
    <div className="discovery-detail">
      {/* Header */}
      <div className="discovery-detail-header">
        <button onClick={onBack} className="back-btn">← 返回列表</button>
        <h2>{scope.name}</h2>
        <span className="scope-type-tag">{scope.type}</span>
        <span className={`lifecycle-tag ${lifecycle.status}`}>
          {lifecycle.status === 'new' && '新增'}
          {lifecycle.status === 'continuing' && '持续'}
          {lifecycle.status === 'confirmed' && '确认'}
          {lifecycle.status === 'weakened' && '减弱'}
          {lifecycle.status === 'invalidated' && '失效'}
        </span>
        {lifecycle.duration > 0 && (
          <span className="duration-tag">持续 {lifecycle.duration} 日</span>
        )}
        {lifecycle.firstSeen && (
          <span className="first-seen-tag">首次 {lifecycle.firstSeen}</span>
        )}
      </div>

      {/* Key Evidence */}
      <section className="discovery-section">
        <h3>关键证据</h3>
        <div className="evidence-tags">
          {keyEvidence.map((e, i) => (
            <span key={i} className="evidence-tag">{e}</span>
          ))}
        </div>
        <p className="signal-count">
          基于 {supportingSignalIds.length} 条 atomic signal evidence
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
      <section className="discovery-section data-quality">
        <h3>数据质量</h3>
        <span>覆盖率: {(dataQuality.coverage * 100).toFixed(1)}%</span>
        <span>readyCount: {dataQuality.readyCount}</span>
      </section>
    </div>
  )
}
