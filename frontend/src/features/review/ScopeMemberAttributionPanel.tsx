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
import {
  NULL_DISPLAY,
  formatNumberNullable,
  formatPercentNullable,
  displayMember,
  type MemberDirectory,
} from './reviewFormat'
import { ATTRIBUTION_SUBTAB_LABELS } from './reviewCopy'
import styles from './review.module.scss'

type Subtabs = 'direction' | 'capital' | 'breadth' | 'concentration' | 'leadership'

// REVIEW-UX-CN-01：subtab label 中文化（canonical value 不变）
const SUBTABS: ReadonlyArray<{ value: Subtabs; label: string }> = [
  { value: 'direction', label: ATTRIBUTION_SUBTAB_LABELS.direction },
  { value: 'capital', label: ATTRIBUTION_SUBTAB_LABELS.capital },
  { value: 'breadth', label: ATTRIBUTION_SUBTAB_LABELS.breadth },
  { value: 'concentration', label: ATTRIBUTION_SUBTAB_LABELS.concentration },
  { value: 'leadership', label: ATTRIBUTION_SUBTAB_LABELS.leadership },
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
  { value: '涨跌幅', field: 'return_1d', format: (v) => formatPercentNullable(v as number | null, 2) },
  { value: '贡献值', field: 'contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** Capital Tilt 列：return_1d + tilt_contribution + aw_weight */
const CAPITAL_COLUMNS: ColumnConfig[] = [
  { value: '涨跌幅', field: 'return_1d', format: (v) => formatPercentNullable(v as number | null, 2) },
  { value: '成交加权差贡献', field: 'tilt_contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
  { value: '成交额权重', field: 'aw_weight', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** Breadth 列：member 信息即事实，展示 return_1d */
const BREADTH_COLUMNS: ColumnConfig[] = [
  { value: '涨跌幅', field: 'return_1d', format: (v) => formatPercentNullable(v as number | null, 2) },
]

/** Concentration 列：concentration_weight + hhi_contribution */
const CONCENTRATION_COLUMNS: ColumnConfig[] = [
  { value: '权重', field: 'concentration_weight', format: (v) => formatNumberNullable(v as number | null, 4) },
  { value: '集中度贡献', field: 'hhi_contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** Leadership 列：aligned_contribution */
const LEADERSHIP_COLUMNS: ColumnConfig[] = [
  { value: '同向贡献', field: 'aligned_contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
  { value: '贡献值', field: 'contribution', format: (v) => formatNumberNullable(v as number | null, 4) },
]

/** 成员组展示 label（中文 + 小号 canonical key；无机会/风险解读） */
const GROUP_LABELS: Readonly<Record<string, string>> = {
  Positive: '正贡献',
  Negative: '负贡献',
  Advance: '上涨成员',
  Decline: '下跌成员',
  Unchanged: '平盘成员',
  Unavailable: '数据不可用',
  Price: '价格侧',
  Amount: '成交额侧',
  Retained: '留存成员',
  Entrants: '新进入成员',
  Exits: '退出成员',
}

/** 可见 metadata 中文 label（canonical payload key 保持原样展示在辅助位置） */
function metaLabel(key: string): string {
  const map: Readonly<Record<string, string>> = {
    sum_contribution: '贡献合计',
    sum_tilt_contribution: '成交加权差贡献合计',
    canonical_aw_return: '成交额加权涨跌幅基准',
    canonical_ew_return: '等权涨跌幅基准',
    price_universe: '价格有效样本数',
    aw_universe: '成交额有效样本数',
    sum_hhi: 'HHI 贡献合计',
    raw_hhi: '原始 HHI',
    normalized_hhi: '标准化 HHI',
    denominator: '有效成员数',
  }
  return map[key] ?? key
}

/** Reconciliation / checksum 中文 label（canonical key 保持原样） */
const RECON_LABELS: Readonly<Record<string, string>> = {
  violations: '异常数',
  tolerance: '容差',
  skipped: '跳过项',
  pass: '通过',
  fail: '未通过',
  skippedState: '已跳过',
  determinism_checksum: '确定性校验码',
}

/**
 * 成员组表格：按传入列配置渲染。
 * 不再使用固定三列的通用表格，每个 subtab 有自己的语义列。
 */
function GroupTable({
  members,
  groupLabel,
  columns,
  directory,
}: {
  members: ScopeMemberEvidence[] | null
  groupLabel: string
  columns: ColumnConfig[]
  directory: MemberDirectory | null | undefined
}) {
  if (!members) return null
  const label = GROUP_LABELS[groupLabel] ?? groupLabel
  return (
    <div className={styles.attrGroup}>
      <div className={styles.attrGroupTitle}>{label}</div>
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
                  {displayMember(m.member_id, directory)}
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

function renderDirection(sub: ScopeAttributionParsed['direction'], directory: MemberDirectory | null | undefined) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.positive} groupLabel="Positive" columns={DIRECTION_COLUMNS} directory={directory} />
      <GroupTable members={sub.negative} groupLabel="Negative" columns={DIRECTION_COLUMNS} directory={directory} />
      <MetaRow>
        {sub.sumContribution !== null && sub.sumContribution !== undefined && (
          <span>{metaLabel('sum_contribution')} {formatNumberNullable(sub.sumContribution, 4)}</span>
        )}
        {sub.canonicalAwReturn !== null && sub.canonicalAwReturn !== undefined && (
          <span>{metaLabel('canonical_aw_return')} {formatPercentNullable(sub.canonicalAwReturn, 2)}</span>
        )}
      </MetaRow>
    </>
  )
}

function renderCapital(sub: ScopeAttributionParsed['capitalTilt'], directory: MemberDirectory | null | undefined) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.positive} groupLabel="Positive" columns={CAPITAL_COLUMNS} directory={directory} />
      <GroupTable members={sub.negative} groupLabel="Negative" columns={CAPITAL_COLUMNS} directory={directory} />
      <MetaRow>
        {sub.sumTiltContribution !== null && sub.sumTiltContribution !== undefined && (
          <span>{metaLabel('sum_tilt_contribution')} {formatNumberNullable(sub.sumTiltContribution, 4)}</span>
        )}
        {sub.canonicalAwReturn !== null && sub.canonicalAwReturn !== undefined && (
          <span>{metaLabel('canonical_aw_return')} {formatPercentNullable(sub.canonicalAwReturn, 2)}</span>
        )}
        {sub.canonicalEwReturn !== null && sub.canonicalEwReturn !== undefined && (
          <span>{metaLabel('canonical_ew_return')} {formatPercentNullable(sub.canonicalEwReturn, 2)}</span>
        )}
        {sub.priceUniverseCount !== null && sub.priceUniverseCount !== undefined && (
          <span>{metaLabel('price_universe')} {sub.priceUniverseCount}</span>
        )}
        {sub.awUniverseCount !== null && sub.awUniverseCount !== undefined && (
          <span>{metaLabel('aw_universe')} {sub.awUniverseCount}</span>
        )}
      </MetaRow>
    </>
  )
}

function renderBreadth(sub: ScopeAttributionParsed['breadth'], directory: MemberDirectory | null | undefined) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.advance} groupLabel="Advance" columns={BREADTH_COLUMNS} directory={directory} />
      <GroupTable members={sub.decline} groupLabel="Decline" columns={BREADTH_COLUMNS} directory={directory} />
      <GroupTable members={sub.unchanged} groupLabel="Unchanged" columns={BREADTH_COLUMNS} directory={directory} />
      <GroupTable members={sub.unavailable} groupLabel="Unavailable" columns={BREADTH_COLUMNS} directory={directory} />
      {sub.denominator !== null && sub.denominator !== undefined && (
        <MetaRow>
          <span>{metaLabel('denominator')} {sub.denominator}</span>
        </MetaRow>
      )}
    </>
  )
}

function renderConcentration(sub: ScopeAttributionParsed['concentration'], directory: MemberDirectory | null | undefined) {
  if (!sub) return null
  return (
    <>
      {sub.price && (
        <>
          <GroupTable members={sub.price.members} groupLabel="Price" columns={CONCENTRATION_COLUMNS} directory={directory} />
          <MetaRow>
            {sub.price.sumHhi !== null && sub.price.sumHhi !== undefined && (
              <span>价格侧 {metaLabel('sum_hhi')} {formatNumberNullable(sub.price.sumHhi, 4)}</span>
            )}
            {sub.price.canonicalRawHhi !== null && sub.price.canonicalRawHhi !== undefined && (
              <span>价格侧 {metaLabel('raw_hhi')} {formatNumberNullable(sub.price.canonicalRawHhi, 4)}</span>
            )}
            {sub.price.canonicalNormalizedHhi !== null && sub.price.canonicalNormalizedHhi !== undefined && (
              <span>价格侧 {metaLabel('normalized_hhi')} {formatNumberNullable(sub.price.canonicalNormalizedHhi, 4)}</span>
            )}
          </MetaRow>
        </>
      )}
      {sub.amount && (
        <>
          <GroupTable members={sub.amount.members} groupLabel="Amount" columns={CONCENTRATION_COLUMNS} directory={directory} />
          <MetaRow>
            {sub.amount.sumHhi !== null && sub.amount.sumHhi !== undefined && (
              <span>成交额侧 {metaLabel('sum_hhi')} {formatNumberNullable(sub.amount.sumHhi, 4)}</span>
            )}
            {sub.amount.canonicalRawHhi !== null && sub.amount.canonicalRawHhi !== undefined && (
              <span>成交额侧 {metaLabel('raw_hhi')} {formatNumberNullable(sub.amount.canonicalRawHhi, 4)}</span>
            )}
            {sub.amount.canonicalNormalizedHhi !== null && sub.amount.canonicalNormalizedHhi !== undefined && (
              <span>成交额侧 {metaLabel('normalized_hhi')} {formatNumberNullable(sub.amount.canonicalNormalizedHhi, 4)}</span>
            )}
          </MetaRow>
        </>
      )}
    </>
  )
}

function renderLeadershipAttribution(sub: ScopeAttributionParsed['leadership'], directory: MemberDirectory | null | undefined) {
  if (!sub) return null
  return (
    <>
      <GroupTable members={sub.retained} groupLabel="Retained" columns={LEADERSHIP_COLUMNS} directory={directory} />
      <GroupTable members={sub.entrants} groupLabel="Entrants" columns={LEADERSHIP_COLUMNS} directory={directory} />
      <GroupTable members={sub.exits} groupLabel="Exits" columns={LEADERSHIP_COLUMNS} directory={directory} />
    </>
  )
}

function ReconciliationStrip({ attr }: { attr: ScopeAttributionParsed }) {
  const r = attr.reconciliation
  if (!r) return null
  const skippedStr = r.skipped.length > 0 ? r.skipped.join(', ') : NULL_DISPLAY
  return (
    <div className={styles.reconStrip} data-panel="reconciliation">
      <div className={styles.reconTitle}>一致性校验</div>
      <div className={styles.reconRow}>
        <span>{RECON_LABELS.violations} {formatNumberNullable(r.violationCount, 0)}</span>
        <span>{RECON_LABELS.tolerance} {r.tolerance === null || r.tolerance === undefined ? NULL_DISPLAY : String(r.tolerance)}</span>
        <span>{RECON_LABELS.skipped} {skippedStr}</span>
      </div>
      {r.checks.length > 0 && (
        <ul className={styles.reconChecks}>
          {r.checks.map((c) => {
            const state = c.pass === null || c.resolved === 'skipped' ? 'skipped' : c.pass ? 'pass' : 'fail'
            const stateLabel =
              state === 'skipped' ? RECON_LABELS.skippedState : state === 'pass' ? RECON_LABELS.pass : RECON_LABELS.fail
            return (
              <li key={c.key} className={styles.reconCheck}>
                <span className={styles.reconCheckKind}>{c.key}</span>
                <span className={`${styles.reconCheckState} ${styles[`reconState${state}`]}`}>{stateLabel}</span>
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
  memberDirectory,
}: {
  attr: ScopeAttributionParsed
  memberDirectory?: MemberDirectory | null
}) {
  const [subtab, setSubtab] = useState<Subtabs>('direction')
  return (
    <div className={styles.panel} data-panel="attribution">
      <div className={styles.attrSubtabs} role="tablist" aria-label="归因贡献子分组">
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

      {subtab === 'direction' && renderDirection(attr.direction, memberDirectory)}
      {subtab === 'capital' && renderCapital(attr.capitalTilt, memberDirectory)}
      {subtab === 'breadth' && renderBreadth(attr.breadth, memberDirectory)}
      {subtab === 'concentration' && renderConcentration(attr.concentration, memberDirectory)}
      {subtab === 'leadership' && renderLeadershipAttribution(attr.leadership, memberDirectory)}

      <ReconciliationStrip attr={attr} />

      {attr.determinismChecksum && (
        <div className={styles.checksumLine} data-testid="determinism-checksum">
          {RECON_LABELS.determinism_checksum} {attr.determinismChecksum}
        </div>
      )}
    </div>
  )
}
