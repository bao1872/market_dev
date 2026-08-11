/** [V2] Representative Instruments — CR-03: contributionPayload + roleEvidence. */

import type { DiscoveryRepresentativeInstrument } from './types'

interface Props {
  instruments: DiscoveryRepresentativeInstrument[]
}

export function InstrumentsPanel({ instruments }: Props) {
  return (
    <section className="discovery-section instruments">
      <h3>代表个股</h3>
      <table className="instruments-table">
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
                {/* [CR-03] contributionPayload and roleEvidence must be preserved */}
                {inst.contributionPayload != null ? '✓' : '-'}
                {' / '}
                {inst.roleEvidence != null ? '✓' : '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
