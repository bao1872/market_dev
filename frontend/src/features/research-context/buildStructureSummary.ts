// [buildStructureSummary] - 描述: 结构状态摘要纯函数（无 React 依赖，可被 node --test 直接运行）
// 从 StructuralFactorResponse + TemporalFeaturesResponse DTO 中提取用户可读的关键字段。
// DTO 路径：
//   - structural.primary[timeframe].cost_position.position_0_1 / price_vs_poc_atr / poc_price / nearest_upper_node.price_mid / nearest_lower_node.price_mid
//   - structural.primary[timeframe].swing_position.active_swing_* （PR #64 active swing 字段，非 developing）
//   - temporal.daily_context.daily_dsa_dir / daily_price_position_in_swing_0_1 / daily_distance_to_node_above_atr
//   - temporal.m15_response.m15_price_position_in_swing_0_1
//   - temporal.derived_relation.m15_response_direction_relative_to_daily / m15_response_intensity
//   - meta.degraded_reasons / warmup_notes
// 不消费任何非白名单字段；不暴露 POC/VAL/VAH/SQZMOM/ATR 等内部字段名给调用方。
import type {
  StructuralFactorResponse,
  TemporalFeaturesResponse,
} from '../../api/endpoints.ts'

export interface StructureSummaryInput {
  structural?: StructuralFactorResponse | null
  temporal?: TemporalFeaturesResponse | null
}

export interface StructureDailySummary {
  /** 日线 DSA 方向（1=上升, -1=下降, 其他=未知） */
  dir: number | string | null
  /** 日线段内位置 [0,1] */
  position: number | null
  /** 距上方节点 / ATR */
  distanceToNodeAbove: number | null
}

export interface StructureM15Summary {
  /** 15m 响应方向（相对日线） */
  responseDir: string | null
  /** 15m 响应强度 [0,1] */
  responseIntensity: number | null
  /** 15m 区间位置 [0,1] */
  position: number | null
}

export interface StructureCostPositionSummary {
  /** VP 全区间位置 [0,1] */
  position: number | null
  /** close vs POC / ATR */
  distanceToPoc: number | null
  /** POC 价格（最密集成交价） */
  poc: number | null
  /** 上方节点价格中点 */
  upperNode: number | null
  /** 下方节点价格中点 */
  lowerNode: number | null
}

export interface StructureSummary {
  /** 是否有任意可用数据 */
  hasData: boolean
  /** 是否降级（degraded_reasons 非空） */
  degraded: boolean
  /** 是否预热中（warmup_notes 非空） */
  warmup: boolean
  /** 降级原因列表（已合并 structural + temporal） */
  degradedReasons: string[]
  /** 预热提示列表（已合并 structural + temporal） */
  warmupNotes: string[]
  daily: StructureDailySummary | null
  m15: StructureM15Summary | null
  costPosition: StructureCostPositionSummary | null
}

function num(v: unknown): number | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'number' && isFinite(v)) return v
  return null
}

function nodePrice(v: unknown): number | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'object' && v !== null && 'price_mid' in v) {
    return num((v as { price_mid: unknown }).price_mid)
  }
  return null
}

/**
 * 从 structural + temporal DTO 构建用户可读的结构状态摘要。
 * 纯函数：无副作用，相同输入相同输出。
 * 调用方负责将 summary 转换为通俗文案（dirText/positionText 等）。
 */
export function buildStructureSummary(input: StructureSummaryInput): StructureSummary {
  const { structural, temporal } = input

  const degradedReasons = [
    ...(structural?.meta.degraded_reasons || []),
    ...(temporal?.meta.degraded_reasons || []),
  ]
  const warmupNotes = [
    ...(structural?.meta.warmup_notes || []),
    ...(temporal?.meta.warmup_notes || []),
  ]

  // daily_context（temporal）
  const daily = temporal?.daily_context
    ? {
        dir: daily_safeDir(temporal.daily_context.daily_dsa_dir),
        position: num(temporal.daily_context.daily_price_position_in_swing_0_1),
        distanceToNodeAbove: num(temporal.daily_context.daily_distance_to_node_above_atr),
      }
    : null

  // m15_response + derived_relation（temporal）
  const m15 = temporal?.m15_response && temporal?.derived_relation
    ? {
        responseDir: temporal.derived_relation.m15_response_direction_relative_to_daily,
        responseIntensity: num(temporal.derived_relation.m15_response_intensity),
        position: num(temporal.m15_response.m15_price_position_in_swing_0_1),
      }
    : null

  // cost_position（structural.primary[timeframe]）
  const costPosition = buildCostPosition(structural)

  const hasData = !!(daily || m15 || costPosition)

  return {
    hasData,
    degraded: degradedReasons.length > 0,
    warmup: warmupNotes.length > 0,
    degradedReasons,
    warmupNotes,
    daily,
    m15,
    costPosition,
  }
}

function daily_safeDir(v: unknown): number | string | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'number') return v
  return String(v)
}

function buildCostPosition(
  structural: StructuralFactorResponse | null | undefined,
): StructureCostPositionSummary | null {
  if (!structural?.primary) return null
  // primary 为 Record<string, Record<string, unknown> | null>，取第一个非 null timeframe
  const primaryTf = Object.values(structural.primary).find((v) => v !== null)
  if (!primaryTf) return null
  const cp = (primaryTf as Record<string, unknown>).cost_position as
    | Record<string, unknown>
    | undefined
  if (!cp) return null
  return {
    position: num(cp.position_0_1),
    distanceToPoc: num(cp.price_vs_poc_atr),
    poc: num(cp.poc_price),
    upperNode: nodePrice(cp.nearest_upper_node),
    lowerNode: nodePrice(cp.nearest_lower_node),
  }
}
