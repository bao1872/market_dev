/** [V2] Representative Instruments — CR-03: contributionPayload + roleEvidence.
 *
 * CR-04: 结构化 evidence rendering —— 对 contributionPayload / roleEvidence
 * 显示有意义的 field/value（按 backend actual payload 形状），
 * 不使用 JSON.stringify 整块 payload；Raw JSON 仅作 debug fallback。
 */

import type { DiscoveryRepresentativeInstrument } from './types'
import styles from './review.module.scss'

interface Props {
  instruments: DiscoveryRepresentativeInstrument[]
}

/** 递归渲染结构化 payload：field / value / label（不做整块 JSON.stringify）。 */
function StructuredEvidence({ label, data }: { label: string; data: unknown }) {
  if (data == null) return null
  const entries =
    typeof data === 'object' && !Array.isArray(data)
      ? Object.entries(data as Record<string, unknown>)
      : null

  return (
    <div className={styles.evidenceBlock}>
      <div className={styles.evidenceLabel}>{label}</div>
      {entries ? (
        entries.map(([k, v]) => (
          <div key={k} className={styles.evidenceRow}>
            <span className={styles.evidenceKey}>{formatKey(k)}</span>
            <span className={styles.evidenceValue}>{formatValue(v)}</span>
          </div>
        ))
      ) : (
        <div className={styles.evidenceRow}>
          <span className={styles.evidenceValue}>{String(data)}</span>
        </div>
      )}
    </div>
  )
}

/** 嵌套对象以紧凑 JSON 展示该字段（仅该字段，非整块 payload）。 */
function formatValue(v: unknown): string {
  if (v == null) return '-'
  if (typeof v === 'object') return JSON.stringify(v)
  if (typeof v === 'number') return Number.isFinite(v) ? String(Number(v.toFixed(4))) : String(v)
  return String(v)
}

/** 后端 snake_case / camelCase → 可读 label */
function formatKey(k: string): string {
  const snake = k.replace(/([a-z0-9])([A-Z])/g, '$1_$2')
  return snake
    .replace(/^rank$/, '排名')
    .replace(/^total$/, '总数')
    .replace(/^rank_percentile$/, '排名分位')
    .replace(/^trend$/, '趋势')
    .replace(/^momentum_change$/, '动量变化')
    .replace(/^volume_ratio20$/, '量比(20日)')
    .replace(/^components$/, '组成')
    .replace(/^denominator$/, '分母')
    .replace(/^component_contributions$/, '组成贡献')
    .replace(/_/g, ' ')
}

export function InstrumentsPanel({ instruments }: Props) {
  return (
    <section className={styles.discoverySection}>
      <h3>代表个股</h3>
      <table className={styles.instrumentsTable}>
        <thead>
          <tr>
            <th>ID</th>
            <th>角色</th>
            <th>关系</th>
            <th>贡献值</th>
            <th>贡献排名</th>
            <th>证据</th>
          </tr>
        </thead>
        <tbody>
          {instruments.map((inst, i) => (
            <tr key={inst.instrumentId || i}>
              <td>{inst.instrumentId}</td>
              <td>{inst.boardRole || '-'}</td>
              <td>{inst.relationToScope || '-'}</td>
              <td>{inst.contributionValue?.toFixed(3) ?? '-'}</td>
              <td>{inst.contributionRank ?? '-'}</td>
              <td>
                <StructuredEvidence label="贡献" data={inst.contributionPayload} />
                <StructuredEvidence label="角色证据" data={inst.roleEvidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
