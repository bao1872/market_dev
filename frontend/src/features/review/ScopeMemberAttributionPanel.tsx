// [ScopeMemberAttributionPanel] - 描述: Member Attribution 面板（Slice E correction）
//
// 硬契约（prompt §6、§7、§10、§11）：
// - 数据源 ONLY composition.member_attribution。
// - 嵌套 subtab：Direction / Capital Tilt / Breadth / Concentration / Leadership。
//   （prompt §10：无 attribution subtab 的 URL 要求；本地 local-only 切换是 clean 理由。）
// - 成员名 = member_name（payload 提供且 distinct 值）否则诚实展示 member_id
//   （当前 backend 未把 member_name_by_id 传入 Attribution，member_name 常回退 member_id；
//    不得猜测股票名、不得 N+1 补名）。
// - 绝不前端重算 contribution / AW-EW / HHI / sum。
// - 每个 subtab 使用不同的 canonical 字段：
//   Direction → contribution, Capital Tilt → tilt_contribution,
//   Breadth → return_1d (group membership is the fact),
//   Concentration → hhi_contribution, Leadership → aligned_contribution。
// - Reconciliation 只展示 canonical checks，不重跑。
//   pass=null / resolved=skipped → 显示 skipped，不是 failure。
//   skipped: string[] 保持数组语义。
//   checks: Record<string, Check> → Object.entries 展示，key = check identity。
import React, { useState } from 'react'
import type {
  ScopeAttributionParsed,
} from './scopeDetailContract'
import type { ScopeMemberEvidence } from './types'
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

/** 每个 subtab 的列配置：列标题 + 从 MemberEvidence 读取的字段 */
interface ColumnConfig {
  value: string
  /** 从 evidence 中读取的字段 key */
  field: keyof ScopeMemberEvidence
  /** 格式化函数 */
  format: (v: unknown) => string
}

/** Direction 列：return_1d + contribution */
const DIRECTION_COLUMNS: ColumnConfig[] = [
  { value: 'Return', field: 'return_1d', format: (v) => formatPercentNullable(v as number | null, 2) },
  { value: 'Contribution', field: 'contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** Capital Tilt 列：return_1d + tilt_contribution + aw_weight */
const CAPITAL_COLUMNS: ColumnConfig[] = [
  { value: 'Return', field: 'return_1d', format: (v) => formatPercentNullable(v as number | null, 2) },
  { value: 'Tilt Contrib', field: 'tilt_contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
  { value: 'AW Weight', field: 'aw_weight', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** Breadth 列：member 信息即事实，展示 return_1d */
const BREADTH_COLUMNS: ColumnConfig[] = [
  { value: 'Return', field: 'return_1d', format: (v) => formatPercentNullable(v as number | null, 2) },
]

/** Concentration 列：concentration_weight + hhi_contribution */
const CONCENTRATION_COLUMNS: ColumnConfig[] = [
  { value: 'Weight', field: 'concentration_weight', format: (v) => formatNumberNullable(v as number | null, 4) },
  { value: 'HHI Contrib', field: 'hhi_contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** Leadership 列：aligned_contribution */
const LEADERSHIP_COLUMNS: ColumnConfig[] = [
  { value: 'Aligned Contrib', field: 'aligned_contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
  { value: 'Contribution', field: 'contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/**
 * 成员组表格：按传入列配置渲染。
 * 不再使用固定三列的通用表格，每个 subtab 有自己的语义列。
 */
function GroupTable({
  members,
  groupLabel,
  columns,
}: {
  members: ScopeMemberEvidence[] | null
  groupLabel: string
  columns: ColumnConfig[]
}) {
  if (!members) return null
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
              {columns.map((c) => (
                <th key={c.value}>{c.value}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={String(m.member_id)}>
                <td className={styles.attrMember} title={String(m.member_id)}>
                  {memberName(m)}
                </td>
                {columns.map((c) => (
                  <td key={c.value}>{c.format(m[c.field])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function MetaRow({ children }: { children: React.ReactNode }) {
  return <div className={styles.attrGroupMeta}>{children}</div>
}

function renderDirection(sub: ScopeAttributionParsed['direction']) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.positive} groupLabel="Positive" columns={DIRECTION_COLUMNS} />
      <GroupTable members={sub.negative} groupLabel="Negative" columns={DIRECTION_COLUMNS} />
      <MetaRow>
        {sub.sumContribution !== null && sub.sumContribution !== undefined && (
          <span>sum_contribution {formatNumberNullable(sub.sumContribution, 4)}</span>
        )}
        {sub.canonicalAwReturn !== null && sub.canonicalAwReturn !== undefined && (
          <span>canonical_aw_return {formatPercentNullable(sub.canonicalAwReturn, 2)}</span>
        )}
      </MetaRow>
    </>
  )
}

function renderCapital(sub: ScopeAttributionParsed['capitalTilt']) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.positive} groupLabel="Positive" columns={CAPITAL_COLUMNS} />
      <GroupTable members={sub.negative} groupLabel="Negative" columns={CAPITAL_COLUMNS} />
      <MetaRow>
        {sub.sumTiltContribution !== null && sub.sumTiltContribution !== undefined && (
          <span>sum_tilt_contribution {formatNumberNullable(sub.sumTiltContribution, 4)}</span>
        )}
        {sub.canonicalAwReturn !== null && sub.canonicalAwReturn !== undefined && (
          <span>canonical_aw_return {formatPercentNullable(sub.canonicalAwReturn, 2)}</span>
        )}
        {sub.canonicalEwReturn !== null && sub.canonicalEwReturn !== undefined && (
          <span>canonical_ew_return {formatPercentNullable(sub.canonicalEwReturn, 2)}</span>
        )}
        {sub.priceUniverseCount !== null && sub.priceUniverseCount !== undefined && (
          <span>price_universe {sub.priceUniverseCount}</span>
        )}
        {sub.awUniverseCount !== null && sub.awUniverseCount !== undefined && (
          <span>aw_universe {sub.awUniverseCount}</span>
        )}
      </MetaRow>
    </>
  )
}

function renderBreadth(sub: ScopeAttributionParsed['breadth']) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.advance} groupLabel="Advance" columns={BREADTH_COLUMNS} />
      <GroupTable members={sub.decline} groupLabel="Decline" columns={BREADTH_COLUMNS} />
      <GroupTable members={sub.unchanged} groupLabel="Unchanged" columns={BREADTH_COLUMNS} />
      <GroupTable members={sub.unavailable} groupLabel="Unavailable" columns={BREADTH_COLUMNS} />
      {sub.denominator !== null && sub.denominator !== undefined && (
        <MetaRow>
          <span>denominator {sub.denominator}</span>
        </MetaRow>
      )}
    </>
  )
}

function renderConcentration(sub: ScopeAttributionParsed['concentration']) {
  if (!sub) return null
  return (
    <>
      {sub.price && (
        <>
          <GroupTable members={sub.price.members} groupLabel="Price" columns={CONCENTRATION_COLUMNS} />
          <MetaRow>
            {sub.price.sumHhi !== null && sub.price.sumHhi !== undefined && (
              <span>price sum_hhi {formatNumberNullable(sub.price.sumHhi, 4)}</span>
            )}
            {sub.price.canonicalRawHhi !== null && sub.price.canonicalRawHhi !== undefined && (
              <span>price raw_hhi {formatNumberNullable(sub.price.canonicalRawHhi, 4)}</span>
            )}
            {sub.price.canonicalNormalizedHhi !== null && sub.price.canonicalNormalizedHhi !== undefined && (
              <span>price normalized_hhi {formatNumberNullable(sub.price.canonicalNormalizedHhi, 4)}</span>
            )}
          </MetaRow>
        </>
      )}
      {sub.amount && (
        <>
          <GroupTable members={sub.amount.members} groupLabel="Amount" columns={CONCENTRATION_COLUMNS} />
          <MetaRow>
            {sub.amount.sumHhi !== null && sub.amount.sumHhi !== undefined && (
              <span>amount sum_hhi {formatNumberNullable(sub.amount.sumHhi, 4)}</span>
            )}
            {sub.amount.canonicalRawHhi !== null && sub.amount.canonicalRawHhi !== undefined && (
              <span>amount raw_hhi {formatNumberNullable(sub.amount.canonicalRawHhi, 4)}</span>
            )}
            {sub.amount.canonicalNormalizedHhi !== null && sub.amount.canonicalNormalizedHhi !== undefined && (
              <span>amount normalized_hhi {formatNumberNullable(sub.amount.canonicalNormalizedHhi, 4)}</span>
            )}
          </MetaRow>
        </>
      )}
    </>
  )
}

function renderLeadershipAttribution(sub: ScopeAttributionParsed['leadership']) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.retained} groupLabel="Retained" columns={LEADERSHIP_COLUMNS} />
      <GroupTable members={sub.entrants} groupLabel="Entrants" columns={LEADERSHIP_COLUMNS} />
      <GroupTable members={sub.exits} groupLabel="Exits" columns={LEADERSHIP_COLUMNS} />
    </>
  )
}

function ReconciliationStrip({ attr }: { attr: ScopeAttributionParsed }) {
  const r = attr.reconciliation
  if (!r) return null
  const skippedStr = r.skipped.length > 0 ? r.skipped.join(', ') : NULL_DISPLAY
  return (
    <div className={styles.reconStrip} data-panel="reconciliation">
      <div className={styles.reconTitle}>Reconciliation</div>
      <div className={styles.reconRow}>
        <span>violations {formatNumberNullable(r.violationCount, 0)}</span>
        <span>tolerance {r.tolerance === null || r.tolerance === undefined ? NULL_DISPLAY : String(r.tolerance)}</span>
        <span>skipped {skippedStr}</span>
      </div>
      {r.checks.length > 0 && (
        <ul className={styles.reconChecks}>
          {r.checks.map((c) => {
            const state = c.pass === null || c.resolved === 'skipped' ? 'skipped' : c.pass ? 'pass' : 'fail'
            return (
              <li key={c.key} className={styles.reconCheck}>
                <span className={styles.reconCheckKind}>{c.key}</span>
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
