import { useMemo, type ReactNode } from 'react'
import {
  parseSmcObservation,
  buildSmcVM,
  parseSmcHistory,
  smcDisplayMember,
  type SmcVM,
  type SmcChangedMember,
  type SmcHistoryStateEntry,
  type SmcHistoryEventEntry,
  type SmcEventVM,
} from './scopeSmcContract'
import type { ReviewScopeHistoryDTO } from './types'
import type { MemberDirectory } from './reviewFormat'
import styles from './review.module.scss'

type Json = Record<string, unknown>

interface ScopeSmcPanelProps {
  observation: Json | null
  history: ReviewScopeHistoryDTO | null
  memberDirectory?: MemberDirectory | null
}

const UP_COLOR = '#16a34a'
const NEUTRAL_COLOR = '#9ca3af'
const DOWN_COLOR = '#dc2626'

function pct(v: number | null | undefined): number {
  if (v == null || !Number.isFinite(v)) return 0
  return Math.max(0, Math.min(1, v))
}

function CompositionBar({ vm }: { vm: SmcVM['swingState'] }) {
  if (!vm) return <span className={styles.kvVal}>—</span>
  return (
    <div>
      <div style={{ display: 'flex', height: 14, borderRadius: 3, overflow: 'hidden', background: '#f1f5f9' }}>
        <div style={{ width: `${pct(vm.upRatio) * 100}%`, background: UP_COLOR }} title={`Up ${vm.up}`} />
        <div style={{ width: `${pct(vm.neutralRatio) * 100}%`, background: NEUTRAL_COLOR }} title={`Neutral ${vm.neutral}`} />
        <div style={{ width: `${pct(vm.downRatio) * 100}%`, background: DOWN_COLOR }} title={`Down ${vm.down}`} />
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 4, fontSize: 12, color: '#475569' }}>
        <span><b style={{ color: UP_COLOR }}>Up {vm.up}</b></span>
        <span><b style={{ color: NEUTRAL_COLOR }}>Neutral {vm.neutral}</b></span>
        <span><b style={{ color: DOWN_COLOR }}>Down {vm.down}</b></span>
        <span style={{ marginLeft: 'auto' }}>denom {vm.denominator ?? '—'}</span>
      </div>
    </div>
  )
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <div className={styles.panelTitle} style={{ marginTop: 16 }}>{children}</div>
}

function BoshochGroup({
  title,
  cells,
}: {
  title: string
  cells: NonNullable<SmcVM['events']>['bosChoch']
}) {
  if (cells.length === 0) {
    return <div style={{ fontSize: 12, color: '#94a3b8' }}>{title}：今日无此类事件</div>
  }
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {cells.map((c, i) => (
          <div key={i} className={styles.bucketChip} style={{ border: '1px solid #e2e8f0' }}>
            <span style={{ fontWeight: 600 }}>{c.eventType} {c.direction === 'Up' ? '↑' : c.direction === 'Down' ? '↓' : ''}</span>
            <span style={{ fontSize: 12, color: '#475569' }}>
              {c.structureLevel} · 成员 {c.memberCount ?? '—'} · 占比 {c.memberRatio} · 事件 {c.eventCount ?? '—'}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ChangedMembersList({
  title,
  members,
  denominator,
  memberDirectory,
}: {
  title: string
  members: SmcChangedMember[]
  denominator: number | null
  memberDirectory?: MemberDirectory | null
}) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
        {title} <span style={{ fontWeight: 400, color: '#94a3b8' }}>(denom {denominator ?? '—'})</span>
      </div>
      {members.length === 0 ? (
        <div style={{ fontSize: 12, color: '#94a3b8' }}>今日无成员发生结构状态迁移</div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {members.map((m) => (
            <span key={m.memberId} className={styles.bucketChip}>
              {smcDisplayMember(m.memberId, memberDirectory)}
              <span style={{ fontSize: 12, color: '#475569' }}>
                {' '}{m.previousState} → {m.currentState}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function CompositionStrip({ entries }: { entries: SmcHistoryStateEntry[] }) {
  return (
    <div style={{ display: 'flex', gap: 1, height: 22, alignItems: 'stretch' }}>
      {entries.map((e) => (
        <div
          key={e.date}
          title={e.date}
          style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column-reverse', background: '#f1f5f9' }}
        >
          <div style={{ height: `${pct(e.facts?.upRatio) * 100}%`, background: UP_COLOR }} />
          <div style={{ height: `${pct(e.facts?.neutralRatio) * 100}%`, background: NEUTRAL_COLOR }} />
          <div style={{ height: `${pct(e.facts?.downRatio) * 100}%`, background: DOWN_COLOR }} />
        </div>
      ))}
    </div>
  )
}

const TAPE_POSITIONS: Array<{ eventType: string; direction: string; structureLevel: string; label: string }> = [
  { eventType: 'BOS', direction: 'Up', structureLevel: 'Swing', label: 'S-BOS ↑' },
  { eventType: 'BOS', direction: 'Down', structureLevel: 'Swing', label: 'S-BOS ↓' },
  { eventType: 'BOS', direction: 'Up', structureLevel: 'Internal', label: 'I-BOS ↑' },
  { eventType: 'BOS', direction: 'Down', structureLevel: 'Internal', label: 'I-BOS ↓' },
  { eventType: 'CHoCH', direction: 'Up', structureLevel: 'Swing', label: 'S-CHoCH ↑' },
  { eventType: 'CHoCH', direction: 'Down', structureLevel: 'Swing', label: 'S-CHoCH ↓' },
  { eventType: 'CHoCH', direction: 'Up', structureLevel: 'Internal', label: 'I-CHoCH ↑' },
  { eventType: 'CHoCH', direction: 'Down', structureLevel: 'Internal', label: 'I-CHoCH ↓' },
]

function markerSize(ratio: number | null): number {
  return 6 + pct(ratio) * 22
}

function EventTape({ tape }: { tape: SmcHistoryEventEntry[] }) {
  if (tape.length === 0) return <div style={{ fontSize: 12, color: '#94a3b8' }}>无事件历史</div>
  const cols = `140px repeat(${tape.length}, minmax(18px, 1fr))`
  return (
    <div style={{ overflowX: 'auto' }}>
      <div style={{ display: 'grid', gridTemplateColumns: cols, alignItems: 'center', gap: 2, fontSize: 12 }}>
        {TAPE_POSITIONS.map((pos) => (
          <FragmentRow key={pos.label} pos={pos} tape={tape} />
        ))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: cols, gap: 2, fontSize: 10, color: '#94a3b8', marginTop: 2 }}>
        <div />
        {tape.map((t) => (
          <div key={t.date} style={{ textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {t.date.slice(5)}
          </div>
        ))}
      </div>
    </div>
  )
}

function FragmentRow({ pos, tape }: { pos: typeof TAPE_POSITIONS[number]; tape: SmcHistoryEventEntry[] }) {
  return (
    <>
      <div style={{ fontWeight: 600 }}>{pos.label}</div>
      {tape.map((t) => {
        if (t.vm === null) return <div key={t.date} style={{ textAlign: 'center', color: '#cbd5e1' }}>—</div>
        if (t.facts?.status === 'unavailable') {
          return <div key={t.date} style={{ textAlign: 'center', color: '#f59e0b', fontSize: 10 }}>不可用</div>
        }
        const cell = t.facts?.bosChoch.find(
          (c) => c.eventType === pos.eventType && c.direction === pos.direction && c.structureLevel === pos.structureLevel,
        )
        if (!cell) return <div key={t.date} style={{ textAlign: 'center', color: '#e2e8f0' }}>·</div>
        return (
          <div key={t.date} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
            <div
              style={{
                width: markerSize(cell.memberRatio),
                height: markerSize(cell.memberRatio),
                borderRadius: '50%',
                background: pos.eventType === 'BOS' ? '#2563eb' : '#7c3aed',
              }}
              title={`成员 ${cell.memberCount ?? '—'} · 占比 ${cell.memberRatio} · 事件 ${cell.eventCount ?? '—'}`}
            />
            <span style={{ fontSize: 9, color: '#475569' }}>{cell.memberCount ?? '—'}</span>
          </div>
        )
      })}
    </>
  )
}

function SecondaryFacts({ events }: { events: SmcEventVM | null }) {
  if (!events) return null
  if (events.secondary.length === 0) return <div style={{ fontSize: 12, color: '#94a3b8' }}>今日无 OB / EQH / EQL 事实</div>
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
      {events.secondary.map((c, i) => (
        <div key={i} className={styles.bucketChip} style={{ border: '1px solid #e2e8f0', opacity: 0.85 }}>
          <span style={{ fontWeight: 600 }}>{c.eventType}</span>
          <span style={{ fontSize: 12, color: '#475569' }}>
            成员 {c.memberCount ?? '—'} · 事件 {c.eventCount ?? '—'}
          </span>
        </div>
      ))}
    </div>
  )
}

export default function ScopeSmcPanel({ observation, history, memberDirectory }: ScopeSmcPanelProps) {
  const facts = useMemo(() => parseSmcObservation(observation), [observation])
  const vm = useMemo(() => buildSmcVM(facts), [facts])
  const historyVm = useMemo(() => parseSmcHistory(history?.smc ?? null), [history])

  const swingBos = vm.events?.bosChoch.filter((c) => c.structureLevel === 'Swing') ?? []
  const internalBos = vm.events?.bosChoch.filter((c) => c.structureLevel === 'Internal') ?? []

  return (
    <div className={styles.detailCard}>
      <div className={styles.panelTitle}>SMC 结构（低频结构事件）</div>

      {/* A. 当前结构 */}
      <SectionTitle>当前结构状态</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Swing</div>
          <CompositionBar vm={vm.swingState} />
        </div>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Internal</div>
          <CompositionBar vm={vm.internalState} />
        </div>
      </div>

      {/* B. 今日结构事件 */}
      <SectionTitle>今日结构事件（BOS / CHoCH）</SectionTitle>
      {vm.events === null ? (
        <div style={{ fontSize: 12, color: '#94a3b8' }}>无结构事件数据</div>
      ) : vm.events.status === 'unavailable' ? (
        <div style={{ fontSize: 12, color: '#f59e0b' }}>事件覆盖不可用（denominator = null）</div>
      ) : (
        <>
          <BoshochGroup title="Swing" cells={swingBos} />
          <BoshochGroup title="Internal" cells={internalBos} />
        </>
      )}

      {/* C. T-1 → T 变化成员 */}
      <SectionTitle>T-1 → T 结构状态迁移成员</SectionTitle>
      <ChangedMembersList title="Swing" members={vm.swingChangedMembers} denominator={vm.swingTransitionDenominator} memberDirectory={memberDirectory} />
      <ChangedMembersList title="Internal" members={vm.internalChangedMembers} denominator={vm.internalTransitionDenominator} memberDirectory={memberDirectory} />

      {/* D. 次级状态 */}
      <SectionTitle>次级状态</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>Structure Alignment</span>
          <span className={styles.kvVal}>
            {vm.alignment ? `Aligned ${vm.alignment.aligned} / Divergent ${vm.alignment.divergent} (denom ${vm.alignment.denominator ?? '—'})` : '—'}
          </span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>Trailing Top (med / p25 / p75)</span>
          <span className={styles.kvVal}>{`${vm.trailingTop.median} / ${vm.trailingTop.p25} / ${vm.trailingTop.p75}`}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>Trailing Bottom (med / p25 / p75)</span>
          <span className={styles.kvVal}>{`${vm.trailingBottom.median} / ${vm.trailingBottom.p25} / ${vm.trailingBottom.p75}`}</span>
        </div>
      </div>
      <div style={{ marginTop: 8 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>OB / EQH / EQL（次要）</div>
        <SecondaryFacts events={vm.events} />
      </div>

      {/* 四. 20D History */}
      <SectionTitle>20D SMC Event Tape（BOS / CHoCH）</SectionTitle>
      <EventTape tape={historyVm.eventTape} />

      <SectionTitle>20D Swing / Internal 状态构成</SectionTitle>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Swing（Up / Neutral / Down）</div>
      <CompositionStrip entries={historyVm.swingState} />
      <div style={{ fontSize: 12, fontWeight: 600, margin: '10px 0 4px' }}>Internal（Up / Neutral / Down）</div>
      <CompositionStrip entries={historyVm.internalState} />
    </div>
  )
}
