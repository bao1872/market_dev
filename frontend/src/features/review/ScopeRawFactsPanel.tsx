// [ScopeRawFactsPanel] - 描述: Raw Facts 面板（Slice E）
//
// 硬契约（prompt §12、§13）：
// - 数据源 ONLY detail response 顶层 observation（完整 Canonical Observation Core payload）。
// - 按精确 canonical 顶层顺序分组：scope / price / trend / structure / momentum / participation / chip。
// - chip 可诚实地为 { status: "unavailable" }，原样展示、不隐藏、不转 0。
// - null != 0、[] != null、unavailable != empty 语义保留。
// - 默认不是 JSON blob；嵌套未知节点用通用递归事实查看器，但无业务解释/score/推断标签。
import type { ScopeAttributionParsed } from './scopeDetailContract'
import { observationGroups } from './scopeDetailContract'
import { NULL_DISPLAY } from './reviewFormat'
import styles from './review.module.scss'

export function scalarDisplay(value: unknown): string | null {
  if (value === null || value === undefined) return NULL_DISPLAY
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : NULL_DISPLAY
  }
  if (typeof value === 'boolean') return String(value)
  if (typeof value === 'string') return value
  return null
}

export function objectKeys(value: Record<string, unknown>): string[] {
  return Object.keys(value)
}

function FactNode({ k, v }: { k: string; v: unknown }) {
  const scalar = scalarDisplay(v)
  if (scalar !== null) {
    return (
      <div className={styles.factRow}>
        <span className={styles.factLabel}>{k}</span>
        <span className={styles.factValue}>{scalar}</span>
      </div>
    )
  }
  if (Array.isArray(v as unknown)) {
    return (
      <details className={styles.factDetails}>
        <summary className={styles.factSummary}>{k} ({(v as unknown[]).length})</summary>
        {(v as unknown[]).slice(0, 20).map((item, i) => (
          <div key={i} className={styles.factArrayItem}>
            <FactNode k={`${k}[${i}]`} v={item} />
          </div>
        ))}
      </details>
    )
  }
  if (typeof v === 'object' && v !== null) {
    const rec = v as Record<string, unknown>
    return (
      <details className={styles.factDetails} open>
        <summary className={styles.factSummary}>{k}</summary>
        <div className={styles.factNested}>
          {objectKeys(rec).map((kk) => (
            <FactNode key={kk} k={kk} v={rec[kk]} />
          ))}
        </div>
      </details>
    )
  }
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{k}</span>
      <span className={styles.factValue}>{NULL_DISPLAY}</span>
    </div>
  )
}

export default function ScopeRawFactsPanel({
  observation,
  attr,
}: {
  observation: Record<string, unknown> | null | undefined
  attr?: ScopeAttributionParsed | null
}) {
  const groups = observationGroups(observation)
  if (groups.length === 0) {
    return <div className={styles.panelUnavailable}>该 Scope 当前没有 Observation payload</div>
  }
  return (
    <div className={styles.panel} data-panel="facts">
      {groups.map((g) => (
        <section key={g.key} className={styles.factGroup} data-fact-group={g.key}>
          <h4 className={styles.factGroupHeading}>{g.key}</h4>
          <FactNode k={g.key} v={g.value} />
        </section>
      ))}
      {attr?.determinismChecksum && (
        <div className={styles.checksumLine}>determinism_checksum {attr.determinismChecksum}</div>
      )}
    </div>
  )
}

export { scalarDisplay as rawScalarDisplay }