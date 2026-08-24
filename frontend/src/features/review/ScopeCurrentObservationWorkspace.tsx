// [ScopeCurrentObservationWorkspace] - 描述: Current Observation Workspace 主 owner（R3B）
//
// R3B 核心：Current 从此由 Canonical Observation 拥有：
//   detail.data.observationGroups (L2) + detail.data.observation (L1 Observation Context)
// 不再由混合 ScopeCurrentSnapshot（Composition + Observation + list identity）拥有。
//
// 硬契约（R3B §3/§6/§10/§13/§15/§18/§20）：
// - 单一 owner：只接收已加载的 observationGroups / observation，绝不 fetch。
// - 无第二请求：复用上层 useReviewScopeDetail（ONE query invariant）。
// - 无 useState sub-tab / 无新 URL state / 无新 query 参数（anchor scroll 仅 presentational）。
// - 不渲染 Analysis：Position / Velocity / Acceleration / Capital Tilt / Migration 不属于 Current。
// - composition = null 不阻断 Current 渲染（Fact-only detail）。
// - 不创建 generic fact-kind detector；group body 仅展示 shell（R3C-R3E 才做正式 grammar）。
import { anchorScroll } from './reviewAnchorScroll'
import {
  buildObservationWorkspaceModel,
  extractObservationContext,
  ObservationGroupContractError,
} from './scopeObservationWorkspaceContract'
import type { ObservationGroup, ObservationGroups } from './types'
import styles from './review.module.scss'

function GroupShell({ group }: { group: ObservationGroup }) {
  // R3B §11：minimal shell 仅证明 group 已 wired、label 来自 backend、facts 已到达。
  // 不实现详细 fact 可视化（属 R3C-R3E）。
  const factEntries = Object.entries(group.facts ?? {})
  return (
    <section className={styles.observationGroup} id={`obs-group-${group.group_key}`}>
      <header className={styles.observationGroupHeader}>
        <h4 className={styles.observationGroupTitle}>{group.label}</h4>
        <code className={styles.observationGroupKey}>{group.group_key}</code>
      </header>
      {factEntries.length === 0 ? (
        <div className={styles.observationGroupEmpty}>
          {group.status === 'unavailable' ? '本组事实当前不可用（unavailable，未覆盖）' : '本组暂无事实'}
        </div>
      ) : (
        <ul className={styles.observationFactList}>
          {factEntries.map(([key]) => (
            <li key={key} className={styles.observationFactItem}>
              <span className={styles.observationFactKey}>{key}</span>
              <span className={styles.observationFactState}>已加载</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

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
          Current Observation 合同无效：{err.message}
        </div>
      )
    }
    throw err
  }

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
              <GroupShell key={g.group_key} group={g} />
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
  const freshness = obs?.freshness

  return (
    <div className={styles.observationContext}>
      <div className={styles.observationContextBlock}>
        <h4 className={styles.observationContextTitle}>Current Technical State</h4>
        {ctx.hasCurrentState ? (
          <div className={styles.observationContextNote}>
            来自 observation.structure.current_state（已加载）
          </div>
        ) : (
          <div className={styles.observationContextUnavailable}>Current Technical State 不可用（observation 无 structure.current_state）</div>
        )}
      </div>

      <div className={styles.observationContextBlock}>
        <h4 className={styles.observationContextTitle}>Freshness</h4>
        {ctx.hasFreshness ? (
          <div className={styles.observationContextNote}>来自 observation.freshness（已加载）</div>
        ) : (
          <div className={styles.observationContextUnavailable}>Event Freshness 不可用（observation 无 freshness）</div>
        )}
      </div>

      <div className={styles.observationContextBlock}>
        <h4 className={styles.observationContextTitle}>Chip availability</h4>
        {ctx.chipAvailability === 'unavailable' ? (
          <div className={styles.observationContextUnavailable}>Chip Unavailable（当前 canonical producer 未产出 chip 事实，如实展示）</div>
        ) : ctx.chipAvailability === 'present' ? (
          <div className={styles.observationContextNote}>Chip 已加载</div>
        ) : (
          <div className={styles.observationContextUnavailable}>Chip 不存在（不伪造 ready）</div>
        )}
      </div>

      {currentState ? <span className={styles.hiddenFactProbe} data-probe="current_state_present" /> : null}
      {freshness ? <span className={styles.hiddenFactProbe} data-probe="freshness_present" /> : null}
    </div>
  )
}
