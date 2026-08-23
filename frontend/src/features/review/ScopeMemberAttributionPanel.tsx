// [ScopeMemberAttributionPanel] - 描述: Member Attribution 面板（Slice E）
//
// 硬契约（prompt §9、§10、§11）：
// - 数据源 ONLY composition.member_attribution。
// - 嵌套 subtab：Direction / Capital Tilt / Breadth / Concentration / Leadership。
//   （prompt §10：无 attribution subtab 的 URL 要求；本地 local-only 切换是 clean 理由。）
// - 成员名 = member_name（payload 提供且 distinct 值）否则诚实展示 member_id
//   （当前 backend 未把 member_name_by_id 传入 Attribution，member_name 常回退 member_id；
//    不得猜测股票名、不得 N+1 补名）。
// - 绝不前端重算 contribution / AW-EW / HHI / sum。
// - Reconciliation 只展示 canonical checks，不重跑。
// - pass=null / resolved=skipped → 显示 skipped，不是 failure。
import { useState } from 'react'
import type {
  ScopeAttributionParsed,
  ScopeAttributionSub,
  ScopeAttributionMemberGroup,
} from './scopeDetailContract'
import { NULL_DISPLAY, formatNumberNullable, formatPercentNullable, memberName } from './reviewFormat'
import styles from './review.module.scss'

type Subtabs = 'direction' | 'capital' | 'breadth' | 'concentration' | 'leadership'

const SUBTABS: ReadonlyArray<{ value: Subtabs; label: string }> = [
  { value: 'direction', label: 'Direction' },
  { value: 'capital', label: 'Capital Tilt' },
  { value: 'breadth', label: 'Breadth' },
  { value: 'concentration', label: 'Concentration' },
  { value: 'leadership', label: 'Leadership' },
]

function GroupTable({ group, groupLabel }: { group: ScopeAttributionMemberGroup | null; groupLabel: string }) {
  if (!group) return null
  const members = group.members
  return (
    <div className={styles.attrGroup}>
      <div className={styles.attrGroupTitle}>{groupLabel}</div>
      {members.length === 0 ? (
        <div className={styles.attrEmpty}>（空成员组）</div>
      ) : (
        <table className={styles.attrTable}>
          <thead>
            <tr>
              <th>成员</th>
              <th>Return</th>
              <th>Contrib</th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={String(m.member_id)}>
                <td className={styles.attrMember} title={String(m.member_id)}>
                  {memberName(m)}
                </td>
                <td>{formatPercentNullable(m.return_1d, 2)}</td>
                <td>{formatNumberNullable(m.contribution, 4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className={styles.attrGroupMeta}>
        {group.sumContribution !== null && group.sumContribution !== undefined && (
          <span>sum_contribution {formatNumberNullable(group.sumContribution, 4)}</span>
        )}
        {group.canonicalAwReturn !== null && group.canonicalAwReturn !== undefined && (
          <span>canonical_aw_return {formatPercentNullable(group.canonicalAwReturn, 2)}</span>
        )}
        {group.canonicalEwReturn !== null && group.canonicalEwReturn !== undefined && (
          <span>canonical_ew_return {formatPercentNullable(group.canonicalEwReturn, 2)}</span>
        )}
        {group.sumTiltContribution !== null && group.sumTiltContribution !== undefined && (
          <span>sum_tilt_contribution {formatNumberNullable(group.sumTiltContribution, 4)}</span>
        )}
        {group.priceUniverseCount !== null && group.priceUniverseCount !== undefined && (
          <span>price_universe {group.priceUniverseCount}</span>
        )}
        {group.awUniverseCount !== null && group.awUniverseCount !== undefined && (
          <span>aw_universe {group.awUniverseCount}</span>
        )}
        {group.denominator !== null && group.denominator !== undefined && (
          <span>denominator {group.denominator}</span>
        )}
      </div>
    </div>
  )
}

function renderDirection(sub: ScopeAttributionSub | null) {
  return (
    <>
      <GroupTable group={sub?.positive ?? null} groupLabel="Positive" />
      <GroupTable group={sub?.negative ?? null} groupLabel="Negative" />
    </>
  )
}

function renderCapital(sub: ScopeAttributionSub | null) {
  return (
    <>
      <GroupTable group={sub?.positive ?? null} groupLabel="Positive" />
      <GroupTable group={sub?.negative ?? null} groupLabel="Negative" />
    </>
  )
}

function renderBreadth(sub: ScopeAttributionSub | null) {
  return (
    <>
      <GroupTable group={sub?.advance ?? null} groupLabel="Advance" />
      <GroupTable group={sub?.decline ?? null} groupLabel="Decline" />
      <GroupTable group={sub?.unchanged ?? null} groupLabel="Unchanged" />
      <GroupTable group={sub?.unavailable ?? null} groupLabel="Unavailable" />
    </>
  )
}

function renderConcentration(sub: ScopeAttributionSub | null) {
  return (
    <>
      <GroupTable group={sub?.price ?? null} groupLabel="Price" />
      <GroupTable group={sub?.amount ?? null} groupLabel="Amount" />
    </>
  )
}

function renderLeadershipAttribution(sub: ScopeAttributionSub | null) {
  return (
    <>
      <GroupTable group={sub?.retained ?? null} groupLabel="Retained" />
      <GroupTable group={sub?.entrants ?? null} groupLabel="Entrants" />
      <GroupTable group={sub?.exits ?? null} groupLabel="Exits" />
    </>
  )
}

function ReconciliationStrip({ attr }: { attr: ScopeAttributionParsed }) {
  const r = attr.reconciliation
  if (!r) return null
  return (
    <div className={styles.reconStrip} data-panel="reconciliation">
      <div className={styles.reconTitle}>Reconciliation</div>
      <div className={styles.reconRow}>
        <span>violations {formatNumberNullable(r.violation_count, 0)}</span>
        <span>tolerance {r.tolerance === null || r.tolerance === undefined ? NULL_DISPLAY : String(r.tolerance)}</span>
        <span>skipped {r.skipped === null || r.skipped === undefined ? NULL_DISPLAY : String(r.skipped)}</span>
      </div>
      {r.checks && r.checks.length > 0 && (
        <ul className={styles.reconChecks}>
          {r.checks.map((c, i) => {
            const state = c.pass === null || c.resolved === 'skipped' ? 'skipped' : c.pass ? 'pass' : 'fail'
            return (
              <li key={`${c.kind}-${i}`} className={styles.reconCheck}>
                <span className={styles.reconCheckKind}>{c.kind}</span>
                <span className={`${styles.reconCheckState} ${styles[`reconState${state}`]}`}>{state}</span>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export default function ScopeMemberAttributionPanel({
  attr,
}: {
  attr: ScopeAttributionParsed
}) {
  const [subtab, setSubtab] = useState<Subtabs>('direction')
  return (
    <div className={styles.panel} data-panel="attribution">
      <div className={styles.attrSubtabs} role="tablist" aria-label="Attribution 子分组">
        {SUBTABS.map((s) => (
          <button
            key={s.value}
            type="button"
            role="tab"
            aria-selected={subtab === s.value}
            className={subtab === s.value ? `${styles.attrSubtab} ${styles.attrSubtabActive}` : styles.attrSubtab}
            onClick={() => setSubtab(s.value)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {subtab === 'direction' && renderDirection(attr.direction)}
      {subtab === 'capital' && renderCapital(attr.capitalTilt)}
      {subtab === 'breadth' && renderBreadth(attr.breadth)}
      {subtab === 'concentration' && renderConcentration(attr.concentration)}
      {subtab === 'leadership' && renderLeadershipAttribution(attr.leadership)}

      <ReconciliationStrip attr={attr} />
    </div>
  )
}