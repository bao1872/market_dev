// [SmcLabels] - 描述: 结构事件中文显示文案唯一映射（前后端/详情/飞书共用）
//
// 设计原则（PRD 盘迹生产链路稳定性收口 V1.0 §三.2 + QM-24 / QM-63）：
//   - 底层 key（BOS/CHoCH/EQH/EQL/order_block/bias=1/-1）不变，仅显示层走本表
//   - 禁止各模块（StrategyChart 渲染 / FirstPyramidPanel / Capture /
//     监控事件 / Review 证据 / 详情页 tab / 飞书卡片）各写一套中文字符串，
//     必须从此模块导入
//   - 通俗易懂，避免英文术语直接暴露给用户
//
// [QM-63 canonical 2026-08-04] 方向正式值为 bullish / bearish。
//   后端 build_pyramid_event 统一产出 direction=bullish|bearish|null 与
//   bias=1|-1|null。前端优先读正式字段（direction / structureLevel / bias），
//   不再直接消费 extra.structure_level / extra.bias。
//   历史值 up/down 仍可解析（兼容旧快照），但不作为新契约。
//
// 文案矩阵（QM-24，BOS/CHoCH/OB × swing/internal × bullish/bearish）：
//   BOS   swing×bullish → 主要·多头突破     swing×bearish → 主要·空头跌破
//         internal×bullish → 短线·多头突破  internal×bearish → 短线·空头跌破
//   CHoCH swing×bullish → 主要·转强拐点     swing×bearish → 主要·转弱拐点
//         internal×bullish → 短线·转强拐点  internal×bearish → 短线·转弱拐点
//   OB    swing×bullish → 主要·多头承接区   swing×bearish → 主要·空头压制区
//         internal×bullish → 短线·多头承接区 internal×bearish → 短线·空头压制区
//   EQH → 双顶压力    EQL → 双底支撑（无结构级别，不虚构主要/短线）
//
// 未知一律显式表达，不得猜测：
//   缺方向 → "方向未知"；缺级别 → "级别未知"；两者皆缺 → "结构未知"
//
// 用法：
//   import { formatSmcEvent, getSmcEqLabel } from '@/components/smcLabels'
//   formatSmcEvent({ type: 'BOS', direction: 'bullish', structureLevel: 'swing' }).label
//   // "主要·多头突破"

/** SMC 事件类型（与后端 DTO type 字段对齐，不改底层 key） */
export type SmcEventType = 'BOS' | 'CHoCH'
export type SmcBias = 1 | -1
export type SmcEqType = 'EQH' | 'EQL'
export type SmcStructureLevel = 'swing' | 'internal'

/** [QM-63] 正式方向值。up/down 为历史输入，归一后不再对外暴露。 */
export type SmcDirection = 'bullish' | 'bearish'

/** 可被解析的方向输入（含历史 up/down 与数值 bias） */
export type SmcDirectionInput = SmcDirection | 'up' | 'down' | number | null | undefined

export interface SmcSemantic {
  label: string
  direction: SmcDirection | null
  structureLevel: SmcStructureLevel | null
  arrow: '↑' | '↓' | ''
  inconsistent: boolean
  diagnostic: string | null
}

/** 方向归一：接受正式值/历史值/数值，无法识别一律 null（不默认空头）。 */
export function normalizeSmcDirection(raw: SmcDirectionInput): SmcDirection | null {
  if (raw === 'bullish' || raw === 'up') return 'bullish'
  if (raw === 'bearish' || raw === 'down') return 'bearish'
  if (typeof raw === 'number') {
    if (raw > 0) return 'bullish'
    if (raw < 0) return 'bearish'
  }
  return null
}

export function directionFromSmcBias(bias: number | null | undefined): SmcDirection | null {
  return normalizeSmcDirection(bias)
}

/** 级别归一：只接受 swing/internal，其余一律 null（不默认 swing）。 */
export function normalizeSmcStructureLevel(
  raw: string | null | undefined,
): SmcStructureLevel | null {
  return raw === 'swing' || raw === 'internal' ? raw : null
}

function resolveDirection(direction: SmcDirectionInput, bias: number | null | undefined) {
  const fromDirection = normalizeSmcDirection(direction)
  const fromBias = normalizeSmcDirection(bias)
  // direction 优先；缺失时用 bias 推导
  const normalized = fromDirection ?? fromBias
  const inconsistent = fromDirection != null && fromBias != null && fromDirection !== fromBias
  return {
    direction: normalized,
    inconsistent,
    diagnostic: inconsistent ? `direction=${fromDirection} 与 bias=${bias} 不一致` : null,
  }
}

const EVENT_ACTION: Record<SmcEventType, Record<SmcDirection, string>> = {
  BOS: { bullish: '多头突破', bearish: '空头跌破' },
  CHoCH: { bullish: '转强拐点', bearish: '转弱拐点' },
}
const OB_ACTION: Record<SmcDirection, string> = {
  bullish: '多头承接区',
  bearish: '空头压制区',
}
const LEVEL_LABEL: Record<SmcStructureLevel, string> = { swing: '主要', internal: '短线' }

/** 缺字段时的显式文案：不猜测方向或级别。 */
function unknownLabel(
  direction: SmcDirection | null,
  level: SmcStructureLevel | null,
): string {
  if (direction == null && level == null) return '结构未知'
  if (direction == null) return '方向未知'
  return '级别未知'
}

function arrowOf(direction: SmcDirection | null): '↑' | '↓' | '' {
  if (direction === 'bullish') return '↑'
  if (direction === 'bearish') return '↓'
  return ''
}

/** BOS/CHoCH 八组合统一格式化；未知值不猜测为多头或空头。 */
export function formatSmcEvent(input: {
  type: SmcEventType | string | null | undefined
  structureLevel: string | null | undefined
  direction?: SmcDirectionInput
  bias?: number | null
}): SmcSemantic {
  const resolved = resolveDirection(input.direction, input.bias)
  const eventType: SmcEventType | null =
    input.type === 'BOS' || input.type === 'CHoCH' ? input.type : null
  const level = normalizeSmcStructureLevel(input.structureLevel)
  if (eventType == null || level == null || resolved.direction == null) {
    return {
      ...resolved,
      structureLevel: level,
      label: eventType == null ? '结构未知' : unknownLabel(resolved.direction, level),
      arrow: arrowOf(resolved.direction),
    }
  }
  return {
    ...resolved,
    structureLevel: level,
    label: `${LEVEL_LABEL[level]}·${EVENT_ACTION[eventType][resolved.direction]}`,
    arrow: arrowOf(resolved.direction),
  }
}

/** Order Block 四组合统一格式化；未知值不猜测为多头或空头。 */
export function formatSmcOrderBlock(input: {
  structureLevel: string | null | undefined
  direction?: SmcDirectionInput
  bias?: number | null
}): SmcSemantic {
  const resolved = resolveDirection(input.direction, input.bias)
  const level = normalizeSmcStructureLevel(input.structureLevel)
  if (level == null || resolved.direction == null) {
    return {
      ...resolved,
      structureLevel: level,
      label: unknownLabel(resolved.direction, level),
      arrow: arrowOf(resolved.direction),
    }
  }
  return {
    ...resolved,
    structureLevel: level,
    label: `${LEVEL_LABEL[level]}·${OB_ACTION[resolved.direction]}`,
    arrow: arrowOf(resolved.direction),
  }
}

export function getSmcEventLabel(
  type: SmcEventType,
  bias: number | null | undefined,
  structureLevel: SmcStructureLevel = 'swing',
): string {
  return formatSmcEvent({ type, bias, structureLevel }).label
}

/** EQH/EQL 没有结构级别，不虚构主要/短线语义。 */
export function getSmcEqLabel(type: SmcEqType): string {
  return type === 'EQH' ? '双顶压力' : '双底支撑'
}

export function getSmcObLabel(
  bias: number | null | undefined,
  structureLevel: SmcStructureLevel = 'swing',
): string {
  return formatSmcOrderBlock({ bias, structureLevel }).label
}
