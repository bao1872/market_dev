// [EvidenceDrawer] - 描述: 统一证据抽屉（PRD §14.8）
// 由任何指标、信号、归因或股票打开。右侧固定面板。
// 内容：定义/当前值/昨日值/5日变化/120日历史分位/分母与coverage/components/
//      底层字段来源/贡献板块股票/缺失原因/source run与算法版本
// 主页面保持简洁，但所有结论可追溯。禁止自由 AI 结论。
import type {
  ReviewMetricPayload,
  ReviewSignal,
  ReviewAttribution,
  ReviewInstrument,
  ReviewMetricComponent,
} from './types'
import styles from './review.module.scss'

function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return n.toFixed(digits)
}

/** 证据目标联合类型：任意可下钻实体 */
export type EvidenceTarget =
  | {
      kind: 'metric'
      title: string
      payload: ReviewMetricPayload | null
      meta?: { sourceRunId?: string; algorithmVersion?: string; definition?: string }
    }
  | { kind: 'signal'; signal: ReviewSignal }
  | { kind: 'attribution'; attr: ReviewAttribution }
  | { kind: 'instrument'; inst: ReviewInstrument }

export interface EvidenceDrawerProps {
  target: EvidenceTarget | null
  onClose: () => void
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.drawerField}>
      <span className={styles.drawerFieldLabel}>{label}</span>
      <span className={styles.drawerFieldValue}>{children}</span>
    </div>
  )
}

/** components 列表：name / rawValue / direction / fieldSource / weight / coverage / status */
function ComponentsList({ components }: { components: ReviewMetricComponent[] }) {
  if (components.length === 0) {
    return <span className={styles.metricUnavailable}>无 components</span>
  }
  return (
    <div className={styles.drawerComponents}>
      {components.map((c, i) => (
        <div key={`${c.name}-${i}`} className={styles.componentRow}>
          <span className={styles.componentName}>
            {c.name}
            {c.direction !== 'neutral' ? ` (${c.direction})` : ''}
          </span>
          <span className={styles.componentVal}>
            {fmt(c.normalizedValue)} / 原始 {fmt(c.rawValue)}
          </span>
          <span className={styles.componentSource}>
            来源 {c.fieldSource} · 分母 {c.denominator ?? '-'} · 权重 {fmt(c.weight)}
            {c.coverage !== null ? ` · coverage ${(c.coverage * 100).toFixed(1)}%` : ''}
            {c.weightMode ? ` · ${c.weightMode}` : ''}
          </span>
          {c.readiness?.reason && (
            <span className={styles.componentSource}>{c.readiness.reason}</span>
          )}
        </div>
      ))}
    </div>
  )
}

/** 缺失原因（status != ready 时展示） */
function missingReason(payload: ReviewMetricPayload | null): string | null {
  if (!payload) return '未计算该变量'
  if (payload.readiness?.reason) return payload.readiness.reason
  if (payload.status === 'insufficient_history') {
    const need = payload.historyObservationCount ?? 0
    return `历史观测不足（当前 ${need}，需 >= 60），无法计算分位`
  }
  if (payload.status === 'unavailable') return '该变量不可用（必要组件缺失）'
  if (payload.status === 'partial') return '部分组件缺失，值为部分加权结果'
  return null
}

function MetricEvidence({
  payload,
  meta,
}: {
  payload: ReviewMetricPayload | null
  meta?: { sourceRunId?: string; algorithmVersion?: string; definition?: string }
}) {
  const reason = missingReason(payload)
  return (
    <>
      <Field label="定义">{meta?.definition ?? '-'}</Field>
      <Field label="当前值">{fmt(payload?.value, 1)}</Field>
      <Field label="原始值">{fmt(payload?.rawValue)}</Field>
      <Field label="1日变化">{fmt(payload?.delta1d, 1)}</Field>
      <Field label="5日变化">{fmt(payload?.delta5d, 1)}</Field>
      <Field label="120日历史分位">
        {payload?.historyPercentile120d !== null && payload?.historyPercentile120d !== undefined
          ? fmt(payload.historyPercentile120d, 1)
          : '-'}
      </Field>
      <Field label="横截面分位">{fmt(payload?.crossSectionPercentile, 1)}</Field>
      <Field label="历史观测数">{payload?.historyObservationCount ?? '-'}</Field>
      <Field label="coverage">
        {payload?.coverage !== null && payload?.coverage !== undefined
          ? `${(payload.coverage * 100).toFixed(1)}%`
          : '-'}
      </Field>
      <Field label="状态">{payload?.status ?? '-'}</Field>
      <Field label="readiness">
        raw={String(payload?.readiness?.raw_ready ?? false)} · normalized={String(payload?.readiness?.normalized_ready ?? false)}
      </Field>
      {reason && (
        <Field label="缺失原因">
          <span className={styles.metricUnavailable}>{reason}</span>
        </Field>
      )}
      <Field label="components">
        <ComponentsList components={payload?.components ?? []} />
      </Field>
      <Field label="source run">{meta?.sourceRunId ?? '-'}</Field>
      <Field label="算法版本">{meta?.algorithmVersion ?? '-'}</Field>
    </>
  )
}

function SignalEvidence({ signal }: { signal: ReviewSignal }) {
  return (
    <>
      <Field label="范围">{signal.scopeName}（{signal.scopeType}）</Field>
      <Field label="信号类型">{signal.filterFamily} · {signal.signalType}</Field>
      <Field label="生命周期状态">{signal.status}</Field>
      <Field label="首次出现">{signal.firstSeenDate}</Field>
      <Field label="持续日数">{signal.durationDays}</Field>
      <Field label="coverage">
        {signal.coverageRatio !== null
          ? `${(signal.coverageRatio * 100).toFixed(1)}%`
          : '-'}
      </Field>
      <Field label="触发条件">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(signal.triggerPayload, null, 2)}
        </pre>
      </Field>
      <Field label="证据">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(signal.evidencePayload, null, 2)}
        </pre>
      </Field>
      <Field label="确认规则">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(signal.confirmationRule, null, 2)}
        </pre>
      </Field>
      <Field label="失效规则">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(signal.invalidationRule, null, 2)}
        </pre>
      </Field>
      {signal.previousSignalId && <Field label="前日信号">{signal.previousSignalId}</Field>}
      {signal.transformedToSignalId && (
        <Field label="转化后信号">{signal.transformedToSignalId}</Field>
      )}
    </>
  )
}

function AttributionEvidence({ attr }: { attr: ReviewAttribution }) {
  return (
    <>
      <Field label="子范围">{attr.childScopeName}（{attr.childScopeType}）</Field>
      <Field label="关系类型">{attr.relationType ?? '-'}</Field>
      <Field label="贡献值">{fmt(attr.contributionValue)}</Field>
      <Field label="贡献排名">{attr.contributionRank ?? '-'}</Field>
      <Field label="coverage">
        {attr.coverageRatio !== null
          ? `${(attr.coverageRatio * 100).toFixed(1)}%`
          : '-'}
      </Field>
      <Field label="指标">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(attr.metricsPayload, null, 2)}
        </pre>
      </Field>
      <Field label="证据">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(attr.evidencePayload, null, 2)}
        </pre>
      </Field>
    </>
  )
}

function InstrumentEvidence({ inst }: { inst: ReviewInstrument }) {
  return (
    <>
      <Field label="股票">{inst.name}（{inst.symbol}）</Field>
      <Field label="板块角色">{inst.boardRole ?? '-'}</Field>
      <Field label="与板块关系">{inst.relationToScope ?? '-'}</Field>
      <Field label="贡献值">{fmt(inst.contributionValue)}</Field>
      <Field label="贡献排名">{inst.contributionRank ?? '-'}</Field>
      <Field label="来源快照">{inst.sourceSnapshotId ?? '-'}</Field>
      <Field label="第一金字塔">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(inst.firstPyramidPayload, null, 2)}
        </pre>
      </Field>
      <Field label="新鲜事件">
        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
          {JSON.stringify(inst.freshEventsPayload, null, 2)}
        </pre>
      </Field>
    </>
  )
}

function targetTitle(target: EvidenceTarget): string {
  switch (target.kind) {
    case 'metric':
      return target.title
    case 'signal':
      return `信号 · ${target.signal.signalType}`
    case 'attribution':
      return `归因 · ${target.attr.childScopeName}`
    case 'instrument':
      return `个股 · ${target.inst.name}`
  }
}

export default function EvidenceDrawer({ target, onClose }: EvidenceDrawerProps) {
  return (
    <aside className={styles.drawer}>
      <div className={styles.drawerHeader}>
        <span className={styles.drawerTitle}>证据详情</span>
        <button
          type="button"
          className={styles.btn}
          onClick={onClose}
          aria-label="关闭证据抽屉"
        >
          关闭
        </button>
      </div>
      <div className={styles.drawerBody}>
        {!target ? (
          <div className={styles.drawerEmpty}>
            点击任意指标、信号、归因或股票，查看完整证据链与字段来源
          </div>
        ) : (
          <>
            <Field label="目标">{targetTitle(target)}</Field>
            {target.kind === 'metric' && (
              <MetricEvidence
                payload={target.payload}
                meta={target.meta}
              />
            )}
            {target.kind === 'signal' && <SignalEvidence signal={target.signal} />}
            {target.kind === 'attribution' && (
              <AttributionEvidence attr={target.attr} />
            )}
            {target.kind === 'instrument' && (
              <InstrumentEvidence inst={target.inst} />
            )}
          </>
        )}
      </div>
    </aside>
  )
}
