/** [V2] Related Scopes — Cross-Scope Relation display. */

import type { DiscoveryRelatedScope } from './types'

interface Props {
  relations: DiscoveryRelatedScope[]
}

const RELATION_LABELS: Record<string, string> = {
  THEME_LED: '主题驱动',
  INDUSTRY_LED: '行业驱动',
  BROAD_CONFIRMATION: '广泛确认',
  ISOLATED_THEME: '独立主题',
  STYLE_LED: '风格驱动',
  CONFLICTING: '冲突',
}

export function RelatedScopesPanel({ relations }: Props) {
  return (
    <section className="discovery-section related-scopes">
      <h3>关联范围 (Cross-Scope Relation)</h3>
      <div className="relation-list">
        {relations.map((r, i) => (
          <div key={i} className={`relation-item ${r.relationType.toLowerCase()}`}>
            <span className="relation-type">
              {RELATION_LABELS[r.relationType] || r.relationType}
            </span>
            <span className="relation-scope-id">{r.targetScopeId || r.sourceScopeId}</span>
            {r.evidence && Object.keys(r.evidence).length > 0 && (
              <div className="relation-evidence">
                {Object.entries(r.evidence).slice(0, 3).map(([k, v]) => (
                  <span key={k} className="relation-evidence-item">
                    {k}: {typeof v === 'number' ? v.toFixed(1) : String(v)}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}
