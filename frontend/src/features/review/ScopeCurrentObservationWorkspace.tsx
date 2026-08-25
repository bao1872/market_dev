// [ScopeCurrentObservationWorkspace] - 描述: Current Observation Workspace 主 owner（R3B + R3C + R3D）
//
// R3B 核心：Current 从此由 Canonical Observation 拥有：
//   detail.data.observationGroups (L2) + detail.data.observation (L1 Observation Context)
// 不再由混合 ScopeCurrentSnapshot（Composition + Observation + list identity）拥有。
//
// R3C：G1–G4 使用正式、真实 Current Observation UX（price_capital / trend_state /
// trend_progress / trend_volume_confirmation）；G5–G8 仍保留 R3B shell。
//
// R3D：G5（structure_break_turn）/ G6（structure_evolution_position）升级为正式
//   Structure Observation UX；G7–G8 仍保留 R3B shell。
//
// 硬契约（R3B §3/§6/§10/§13/§15/§18/§20；R3C §1/§11/§19；R3D §1/§4/§5/§21）：
// - 单一 owner：只接收已加载的 observationGroups / observation，绝不 fetch。
// - 无第二请求：复用上层 useReviewScopeDetail（ONE query invariant）。
// - 无 useState sub-tab / 无新 URL state / 无新 query 参数（anchor scroll 仅 presentational）。
// - 不渲染 Analysis：Position / Velocity / Acceleration / Capital Tilt / Migration 不属于 Current。
// - composition = null 不阻断 Current 渲染（Fact-only detail）。
// - G1–G4 前端只承载、不重算；方向色仅限 signed directional facts（见 R3C §8）。
// - G5–G6 前端只承载、不重算；member_ratio 为主事实，事件条数仅作证据（R3D §5/§13）。
// - 不创建 generic fact-kind detector；G7–G8 仍走 R3B shell。
import { type FC, useMemo } from 'react'
import { anchorScroll } from './reviewAnchorScroll'
import {
  buildObservationWorkspaceModel,
  extractObservationContext,
  ObservationGroupContractError,
} from './scopeObservationWorkspaceContract'
import { formatNumberNullable } from './reviewFormat'
import type { ObservationGroup, ObservationGroups } from './types'
import {
  parsePriceCapital,
  buildPriceCapitalVM,
  parseTrendState,
  buildTrendStateVM,
  parseTrendProgress,
  buildTrendProgressVM,
  parseTrendVolumeConfirmation,
} from './scopePriceTrendContract'
import {
  parseStructureBreakTurn,
  parseStructureEvolutionPosition,
} from './scopeStructureContract'
import ScopePriceCapitalObservation from './ScopePriceCapitalObservation'
import ScopeTrendObservation from './ScopeTrendObservation'
import ScopeStructureObservation from './ScopeStructureObservation'
import ScopeMomentumObservation from './ScopeMomentumObservation'
import ScopeVolumeObservation from './ScopeVolumeObservation'
import { parseMomentumObservation, parseVolumeObservation } from './scopeMomentumVolumeContract'
import ReviewTerm from './ReviewTerm'
import styles from './review.module.scss'

function GroupShell({ group }: { group: ObservationGroup }) {
  // R3B §11：minimal shell 仅证明 group 已 wired、label 来自 backend、facts 已到达。
  // 不做 generic fact-kind inference / 不显示"已加载/不可用"等状态判断（事实可用性属 R3C-R3E）。
  // backend canonical L2 = {group_key,label,facts}，无 group-level status。
  const factEntries = Object.entries(group.facts ?? {})
  return (
    <section className={styles.observationGroup} id={`obs-group-${group.group_key}`}>
      <header className={styles.observationGroupHeader}>
        <h4 className={styles.observationGroupTitle}>{group.label}</h4>
      </header>
      {factEntries.length === 0 ? (
        <div className={styles.observationGroupEmpty}>暂无事实字段</div>
      ) : (
        <ul className={styles.observationFactList}>
          {factEntries.map(([key]) => (
            <li key={key} className={styles.observationFactItem}>
              <span className={styles.observationFactKey}>{key}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// ---- R3C formal renderers (G1–G4) -----------------------------------------

function PriceCapitalBlock({ facts, observation }: { facts: Record<string, unknown>; observation: Record<string, unknown> | null }) {
  const vm = useMemo(
    () => buildPriceCapitalVM(parsePriceCapital(facts, observation ?? undefined)),
    [facts, observation],
  )
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopePriceCapitalObservation vm={vm} />
}

function TrendStateBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => buildTrendStateVM(parseTrendState(facts)), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeTrendObservation state={vm} />
}

function TrendProgressBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => buildTrendProgressVM(parseTrendProgress(facts)), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeTrendObservation progress={vm} />
}

function TrendVolumeBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => parseTrendVolumeConfirmation(facts), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeTrendObservation volume={vm} />
}

// ---- R3D formal renderers (G5–G6) -----------------------------------------

function StructureBreakTurnBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => parseStructureBreakTurn(facts), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeStructureObservation breakTurn={vm} />
}

function StructureEvolutionBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => parseStructureEvolutionPosition(facts), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeStructureObservation evolution={vm} />
}

// ---- R3E formal renderers (G7–G8) -----------------------------------------
// Canonical L2 group keys (backend ObservationGroupSpec):
//   momentum_squeeze_release / volume_anomaly
// group.facts is the direct fact object (squeeze_state / bb_position / ...),
// NOT a nested wrapper key. Parser consumes group.facts directly.

function MomentumBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => parseMomentumObservation(facts), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeMomentumObservation vm={vm} />
}

function VolumeBlock({ facts }: { facts: Record<string, unknown> }) {
  const vm = useMemo(() => parseVolumeObservation(facts), [facts])
  if (!vm) return <div className={styles.observationGroupEmpty}>暂无事实字段</div>
  return <ScopeVolumeObservation vm={vm} />
}

const FORMAL_RENDERERS: Record<string, FC<{ facts: Record<string, unknown>; observation: Record<string, unknown> | null }>> = {
  price_capital: PriceCapitalBlock,
  trend_state: TrendStateBlock,
  trend_progress: TrendProgressBlock,
  trend_volume_confirmation: TrendVolumeBlock,
  structure_break_turn: StructureBreakTurnBlock,
  structure_evolution_position: StructureEvolutionBlock,
  momentum_squeeze_release: MomentumBlock,
  volume_anomaly: VolumeBlock,
}

export function groupHasAnyPresentFact(facts: Record<string, unknown> | undefined | null): boolean {
  return groupHasAnyPresentFactImpl(facts)
}
import { groupHasAnyPresentFact as groupHasAnyPresentFactImpl } from './scopeObservationWorkspaceContract'

function GroupBody({
  group,
  observation,
}: {
  group: ObservationGroup
  observation: Record<string, unknown> | null
}) {
  // [Phase 2] 整个父级 observation group 无有效事实 → 中文父级 unavailable 态，
  // 不产生 dash-spam。文字与 canonical label 对齐（group.label 来自 backend L2）。
  if (!groupHasAnyPresentFact(group.facts)) {
    return (
      <div className={styles.observationGroupEmpty}>
        本期暂无{group.label}数据
      </div>
    )
  }
  const Renderer = FORMAL_RENDERERS[group.group_key]
  if (Renderer) {
    return <Renderer facts={group.facts ?? {}} observation={observation} />
  }
  return <GroupShell group={group} />
}

// ---- Anchor nav ------------------------------------------------------------

function AnchorNav({
  areaKeys,
  areaTitles,
}: {
  areaKeys: ReadonlyArray<string>
  areaTitles: ReadonlyArray<string>
}) {
  // R3B §10：anchor / scroll navigation，仅 presentational，不创建新 state 机。
  const onClick = (e: React.MouseEvent<HTMLAnchorElement>, key: string) => {
    e.preventDefault()
    anchorScroll(`obs-area-${key}`)
  }
  return (
    <nav className={styles.observationNav} aria-label="Current observation sections">
      {areaKeys.map((k, i) => (
        <a key={k} href={`#obs-area-${k}`} className={styles.observationNavLink} onClick={(e) => onClick(e, k)}>
          {areaTitles[i]}
        </a>
      ))}
    </nav>
  )
}

export default function ScopeCurrentObservationWorkspace({
  observationGroups,
  observation,
}: {
  observationGroups: ObservationGroups | null | undefined
  observation: Record<string, unknown> | null | undefined
}) {
  let model
  try {
    model = buildObservationWorkspaceModel(observationGroups)
  } catch (err) {
    if (err instanceof ObservationGroupContractError) {
      return (
        <div className={styles.panelUnavailable}>
          当日观察数据格式异常
          <div className={styles.panelErrorDetail}>技术细节：{err.message}</div>
        </div>
      )
    }
    throw err
  }

  const obsRecord = observation && typeof observation === 'object' ? (observation as Record<string, unknown>) : null
  const ctx = extractObservationContext(observation)
  const areaKeys = model.areas.map((a) => a.area.areaKey)
  const areaTitles = model.areas.map((a) => a.area.areaTitle)

  return (
    <div className={styles.panel} data-panel="current-observation">
      <AnchorNav areaKeys={areaKeys} areaTitles={areaTitles} />

      {model.areas.map(({ area, groups }) => {
        // Observation Context 区不渲染 canonical group shell，单独处理（R3B §12）。
        if (area.areaKey === 'context') {
          return (
            <section key={area.areaKey} id={`obs-area-${area.areaKey}`} className={styles.observationArea}>
              <h3 className={styles.observationAreaTitle}>{area.areaTitle}</h3>
              <ContextShell ctx={ctx} observation={observation} />
            </section>
          )
        }
        return (
          <section key={area.areaKey} id={`obs-area-${area.areaKey}`} className={styles.observationArea}>
            <h3 className={styles.observationAreaTitle}>{area.areaTitle}</h3>
            {groups.map((g) => (
              <div key={g.group_key} id={`obs-group-${g.group_key}`} className={styles.observationGroup}>
                <header className={styles.observationGroupHeader}>
                  <h4 className={styles.observationGroupTitle}>{g.label}</h4>
                </header>
                <GroupBody group={g} observation={obsRecord} />
              </div>
            ))}
          </section>
        )
      })}
    </div>
  )
}

// ============================================================
// Observation Context（R3B §12/§16）：从 observation（L1）读取，不依赖 Composition
// ============================================================
function ContextShell({
  ctx,
  observation,
}: {
  ctx: ReturnType<typeof extractObservationContext>
  observation: Record<string, unknown> | null | undefined
}) {
  const obs = observation && typeof observation === 'object' ? observation : null
  const structure = obs?.structure
  const currentState =
    structure && typeof structure === 'object' && (structure as Record<string, unknown>).current_state

  const f = ctx.freshness
  const num = (k: string): number | null =>
    f && typeof f[k] === 'number' && Number.isFinite(f[k] as number) ? (f[k] as number) : null
  const dim = (k: string): Record<string, unknown> | null =>
    f && f.by_dimension && typeof f.by_dimension === 'object'
      ? ((f.by_dimension as Record<string, unknown>)[k] as Record<string, unknown> | null) ?? null
      : null
  const dimNum = (k: string, field: string): number | null => {
    const d = dim(k)
    const v = d?.[field]
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }

  const DIMENSIONS: { key: string; label: string }[] = [
    { key: 'trend', label: '趋势' },
    { key: 'structure', label: '结构' },
    { key: 'momentum', label: '动量' },
    { key: 'chip', label: '筹码' },
  ]

  return (
    <div className={styles.observationContext}>
      <div className={styles.observationContextBlock}>
        <h4 className={styles.observationContextTitle}>当前技术状态</h4>
        {ctx.hasCurrentState ? (
          <div className={styles.observationContextNote}>
            <ReviewTerm
              label="当前技术状态已加载"
              help="当前状态来自本期结构分析结果。技术字段：observation.structure.current_state"
            />
          </div>
        ) : (
          <div className={styles.observationContextUnavailable}>本期暂无结构状态数据</div>
        )}
      </div>

      <div className={styles.observationContextBlock}>
        <h4 className={styles.observationContextTitle}>事件新鲜度</h4>
        {ctx.hasFreshness && f ? (
          <div className={styles.observationContextMetrics}>
            <div className={styles.observationContextMetric}>
              <span className={styles.observationContextMetricLabel}>今日</span>
              <span className={styles.observationContextMetricValue}>{formatNumberNullable(num('today_count'), 0)}</span>
            </div>
            <div className={styles.observationContextMetric}>
              <span className={styles.observationContextMetricLabel}>近5日</span>
              <span className={styles.observationContextMetricValue}>{formatNumberNullable(num('last_5d_count'), 0)}</span>
            </div>
            <div className={styles.observationContextMetric}>
              <span className={styles.observationContextMetricLabel}>近10日</span>
              <span className={styles.observationContextMetricValue}>{formatNumberNullable(num('last_10d_count'), 0)}</span>
            </div>
            <div className={styles.observationContextMetric}>
              <span className={styles.observationContextMetricLabel}>近20日</span>
              <span className={styles.observationContextMetricValue}>{formatNumberNullable(num('last_20d_count'), 0)}</span>
            </div>
            <div className={styles.observationContextMetric}>
              <span className={styles.observationContextMetricLabel}>涉及股票数</span>
              <span className={styles.observationContextMetricValue}>{formatNumberNullable(num('instrument_count'), 0)}</span>
            </div>
            <div className={styles.observationContextMetric}>
              <span className={styles.observationContextMetricLabel}>衰减加权密度</span>
              <span className={styles.observationContextMetricValue}>{formatNumberNullable(num('decay_weighted_density'), 3)}</span>
            </div>
            {DIMENSIONS.map(({ key, label }) => (
              <div key={key} className={styles.observationContextMetric}>
                <span className={styles.observationContextMetricLabel}>{label} 密度 / 事件数</span>
                <span className={styles.observationContextMetricValue}>
                  {formatNumberNullable(dimNum(key, 'density'), 3)}
                  {' / '}
                  {formatNumberNullable(dimNum(key, 'event_count'), 0)}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className={styles.observationContextUnavailable}>事件新鲜度不可用（observation 无 freshness）</div>
        )}
      </div>

      <div className={styles.observationContextBlock}>
        <h4 className={styles.observationContextTitle}>筹码可用性</h4>
        {ctx.chipAvailability === 'unavailable' ? (
          <div className={styles.observationContextUnavailable}>本期暂无筹码数据</div>
        ) : ctx.chipAvailability === 'present' ? (
          <div className={styles.observationContextNote}>筹码已加载</div>
        ) : (
          <div className={styles.observationContextUnavailable}>筹码不存在（不伪造 ready）</div>
        )}
      </div>

      {currentState ? <span className={styles.hiddenFactProbe} data-probe="current_state_present" /> : null}
      {f ? <span className={styles.hiddenFactProbe} data-probe="freshness_present" /> : null}
    </div>
  )
}
