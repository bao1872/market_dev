// [ScopeObservationWorkspaceContract] - 描述: Current Observation Workspace 纯 TS adapter owner（R3B）
//
// 职责（R3B §7）：
// - 校验 / 组织 backend canonical ObservationGroups（8 固定 key）。
// - 把 8 个 canonical group 组织进 UI navigation / information architecture 区域。
// - 保留 group 引用（不复制事实）、保留 facts verbatim、保留 backend group.label。
//
// 严禁（R3B §7/§17）：
// - 不计算业务事实（score / ratio / HHI / momentum-trend relation / event ratio）。
// - 不归一化事实、不推断 bullish/bearish、不把 unavailable 当 0。
// - 不创建 generic fact-kind detector（detectFactKind / inferDistribution / inferCategorical）。
// - 前端不维护第二份 canonical 8-group 中文 label map（label 只能来自 backend group.label）。
//
// 区域标题（Price/Trend/Structure/Momentum/Volume/Observation Context）是 UI IA 标签，
// 不是新的业务 semantic owner；区域内部 group heading 必须渲染 backend group.label。
//
// 纯 TS（无 React / @/ 别名依赖），可被 node --test 直接运行。

import type { ObservationGroup, ObservationGroups } from './types'

/** backend 权威 8-group 顺序（与 ObservationGroups 接口键顺序一致） */
export const CANONICAL_GROUP_KEYS: ReadonlyArray<keyof ObservationGroups> = [
  'price_capital',
  'trend_state',
  'trend_progress',
  'trend_volume_confirmation',
  'structure_break_turn',
  'structure_evolution_position',
  'momentum_squeeze_release',
  'volume_anomaly',
]

/** Current Observation 的 UI 信息架构区域（纯 navigation label，非新 semantic owner） */
export type ObservationWorkspaceAreaKey =
  | 'price'
  | 'trend'
  | 'structure'
  | 'momentum'
  | 'volume'
  | 'context'

export interface ObservationWorkspaceArea {
  areaKey: ObservationWorkspaceAreaKey
  /** UI navigation / IA 标题（前端定义，不影响 backend group 语义） */
  areaTitle: string
  /** 该区域包含的 canonical group keys（顺序即渲染顺序） */
  groupKeys: ReadonlyArray<keyof ObservationGroups>
}

/**
 * 区域 → canonical group 映射（R3B §4/§9）。
 * 这是 INFORMATION ARCHITECTURE，不是新的业务 owner。
 */
export const OBSERVATION_WORKSPACE_AREAS: ReadonlyArray<ObservationWorkspaceArea> = [
  {
    areaKey: 'price',
    areaTitle: 'Price & Capital',
    groupKeys: ['price_capital'],
  },
  {
    areaKey: 'trend',
    areaTitle: 'Trend',
    groupKeys: ['trend_state', 'trend_progress', 'trend_volume_confirmation'],
  },
  {
    areaKey: 'structure',
    areaTitle: 'Structure',
    groupKeys: ['structure_break_turn', 'structure_evolution_position'],
  },
  {
    areaKey: 'momentum',
    areaTitle: 'Momentum',
    groupKeys: ['momentum_squeeze_release'],
  },
  {
    areaKey: 'volume',
    areaTitle: 'Volume',
    groupKeys: ['volume_anomaly'],
  },
  {
    areaKey: 'context',
    areaTitle: 'Observation Context',
    groupKeys: [],
  },
]

export class ObservationGroupContractError extends Error {}

export interface CanonicalGroupValidation {
  /** 恰好 8 个 canonical group present */
  exactCount: boolean
  /** 每个对象的 group_key 与其 container key 一致 */
  keysAgree: boolean
  /** 每个 group.label 非空 */
  labelsNonEmpty: boolean
  /** 每个 group.facts 为对象 */
  factsAreObjects: boolean
  /** 综合：contract 是否被违反（false = 应 fail-closed） */
  valid: boolean
}

/**
 * 在 contract 边界校验 backend canonical ObservationGroups 完整性（R3B §8）。
 * 不静默 relabel；任一硬约束失败抛出 ObservationGroupContractError。
 */
export function validateCanonicalGroups(
  groups: ObservationGroups | null | undefined,
): CanonicalGroupValidation {
  const validation: CanonicalGroupValidation = {
    exactCount: false,
    keysAgree: false,
    labelsNonEmpty: false,
    factsAreObjects: false,
    valid: false,
  }
  if (!groups || typeof groups !== 'object') return validation

  const presentKeys = Object.keys(groups) as Array<keyof ObservationGroups>
  validation.exactCount = presentKeys.length === CANONICAL_GROUP_KEYS.length

  // 每个 container key 必须出现在权威键集中，且其 group_key 字段自洽。
  let keysAgree = true
  let labelsNonEmpty = true
  let factsAreObjects = true
  for (const key of CANONICAL_GROUP_KEYS) {
    const g = groups[key]
    if (!g) {
      keysAgree = false
      break
    }
    if (g.group_key !== key) keysAgree = false
    if (typeof g.label !== 'string' || g.label.trim() === '') labelsNonEmpty = false
    if (!g.facts || typeof g.facts !== 'object' || Array.isArray(g.facts)) {
      factsAreObjects = false
      break
    }
  }
  validation.keysAgree = keysAgree
  validation.labelsNonEmpty = labelsNonEmpty
  validation.factsAreObjects = factsAreObjects
  validation.valid =
    validation.exactCount && validation.keysAgree && validation.labelsNonEmpty && validation.factsAreObjects
  return validation
}

export interface ObservationWorkspaceModel {
  /** 校验结果（用于测试/失败时 fail-closed） */
  validation: CanonicalGroupValidation
  /** 按 UI 区域组织的 canonical group（引用原对象，verbatim facts） */
  areas: ReadonlyArray<{
    area: ObservationWorkspaceArea
    groups: ReadonlyArray<ObservationGroup>
  }>
  /** 扁平的 8 个 canonical group（顺序与 backend 一致） */
  allGroups: ReadonlyArray<ObservationGroup>
}

/**
 * 构建 Current Observation Workspace 模型。
 * 仅组织 + 校验，不计算、不复制事实、不重排 canonical group。
 * contract 违反时抛出 ObservationGroupContractError（R3B §18：不静默 relabel）。
 */
export function buildObservationWorkspaceModel(
  groups: ObservationGroups | null | undefined,
): ObservationWorkspaceModel {
  const validation = validateCanonicalGroups(groups)
  if (!validation.valid) {
    throw new ObservationGroupContractError(
      `Canonical ObservationGroups contract invalid: ${JSON.stringify(validation)}`,
    )
  }

  const allGroups = CANONICAL_GROUP_KEYS.map((k) => groups![k])
  const areas = OBSERVATION_WORKSPACE_AREAS.map((area) => ({
    area,
    groups: area.groupKeys.map((k) => groups![k]),
  }))

  return { validation, areas, allGroups }
}

/**
 * Observation Context 从 detail.observation（L1）读取（R3B §12），
 * 不依赖 Composition。返回是否存在 chip availability 事实（用于诚实展示 unavailable）。
 */
export interface ObservationContextFacts {
  hasCurrentState: boolean
  hasFreshness: boolean
  /** 原始 freshness 对象（persisted backend 形状），供 ContextShell 完整数值展示；无则 null */
  freshness: Record<string, unknown> | null
  /** chip 当前 canonical producer 恒为 unavailable；前端如实展示 */
  chipAvailability: 'unavailable' | 'present' | 'absent'
}

export function extractObservationContext(
  observation: Record<string, unknown> | null | undefined,
): ObservationContextFacts {
  const obs = observation && typeof observation === 'object' ? observation : null
  const structure = obs?.structure
  const currentState =
    structure && typeof structure === 'object' && (structure as Record<string, unknown>).current_state
  const freshness = obs?.freshness && typeof obs.freshness === 'object'
    ? (obs.freshness as Record<string, unknown>)
    : null
  const chip = obs?.chip
  let chipAvailability: ObservationContextFacts['chipAvailability'] = 'absent'
  if (chip && typeof chip === 'object') {
    const c = chip as Record<string, unknown>
    if (c.status === 'unavailable' || c.status === 'UNAVAILABLE') chipAvailability = 'unavailable'
    else if ('available' in c || c.status === 'available') chipAvailability = 'present'
    else chipAvailability = 'unavailable' // 无明确 available 标记 → 保守 unavailable，不伪造 ready
  }
  return {
    hasCurrentState: !!currentState,
    hasFreshness: !!freshness,
    freshness,
    chipAvailability,
  }
}
