// [SmcLabels] - 描述: 结构事件中文显示文案唯一映射（前后端/详情/飞书共用）
//
// 设计原则（PRD 盘迹生产链路稳定性收口 V1.0 §三.2 + 用户反馈 2026-07-21）：
//   - 底层 key（BOS/CHoCH/EQH/EQL/order_block/bias=1/-1）不变，仅显示层走本表
//   - 禁止各模块（StrategyChart 渲染 / MobileIndicatorStage 标题 / 详情页 tab / 飞书卡片）
//     各写一套中文字符串，必须从此模块导入
//   - 通俗易懂，避免英文术语直接暴露给用户
//
// 映射表（用户确认 2026-07-21）：
//   bullish BOS      → 突破前高
//   bearish BOS      → 跌破前低
//   bullish CHoCH    → 转强拐点
//   bearish CHoCH    → 转弱拐点
//   EQH              → 双顶压力
//   EQL              → 双底支撑
//   bullish OB       → 多头承接区
//   bearish OB       → 空头压制区
//
// trailing strong/weak high/low 不在本表（已是中文"强高/弱高/强低/弱低"，保持现状）。
//
// 用法：
//   import { getSmcEventLabel, getSmcEqLabel, getSmcObLabel } from '@/components/smcLabels'
//   const label = getSmcEventLabel('BOS', 1)  // "突破前高"

/** SMC 事件类型（与后端 DTO type 字段对齐，不改底层 key） */
export type SmcEventType = 'BOS' | 'CHoCH'
export type SmcBias = 1 | -1
export type SmcEqType = 'EQH' | 'EQL'
export type SmcStructureLevel = 'swing' | 'internal'
export type SmcDirection = 'up' | 'down'

export interface SmcSemantic {
  label: string
  direction: SmcDirection | null
  structureLevel: SmcStructureLevel | null
  arrow: '↑' | '↓' | ''
  inconsistent: boolean
  diagnostic: string | null
}

export function directionFromSmcBias(bias: number | null | undefined): SmcDirection | null {
  return bias === 1 ? 'up' : bias === -1 ? 'down' : null
}

function resolveDirection(direction: SmcDirection | null | undefined, bias: number | null | undefined) {
  const biasDirection = directionFromSmcBias(bias)
  const normalized = direction === 'up' || direction === 'down' ? direction : biasDirection
  const inconsistent = normalized != null && biasDirection != null && normalized !== biasDirection
  return {
    direction: normalized,
    inconsistent,
    diagnostic: inconsistent ? `direction=${normalized} 与 bias=${bias} 不一致` : null,
  }
}

const EVENT_ACTION: Record<SmcEventType, Record<SmcDirection, string>> = {
  BOS: { up: '突破前高', down: '跌破前低' },
  CHoCH: { up: '转强拐点', down: '转弱拐点' },
}
const LEVEL_LABEL: Record<SmcStructureLevel, string> = { swing: '主要', internal: '短线' }

/** BOS/CHoCH 八组合统一格式化；未知值不猜测为多头或空头。 */
export function formatSmcEvent(input: {
  type: SmcEventType | string | null | undefined
  structureLevel: SmcStructureLevel | null | undefined
  direction?: SmcDirection | null
  bias?: number | null
}): SmcSemantic {
  const resolved = resolveDirection(input.direction, input.bias)
  const eventType: SmcEventType | null = input.type === 'BOS' || input.type === 'CHoCH'
    ? input.type
    : null
  const level = input.structureLevel === 'swing' || input.structureLevel === 'internal'
    ? input.structureLevel
    : null
  if (eventType == null || level == null || resolved.direction == null) {
    return { ...resolved, structureLevel: level, label: '未知结构', arrow: '' }
  }
  return {
    ...resolved,
    structureLevel: level,
    label: `${LEVEL_LABEL[level]}${EVENT_ACTION[eventType][resolved.direction]}`,
    arrow: resolved.direction === 'up' ? '↑' : '↓',
  }
}

/** Order Block 四组合统一格式化；未知值不猜测为多头或空头。 */
export function formatSmcOrderBlock(input: {
  structureLevel: SmcStructureLevel | null | undefined
  direction?: SmcDirection | null
  bias?: number | null
}): SmcSemantic {
  const resolved = resolveDirection(input.direction, input.bias)
  const level = input.structureLevel === 'swing' || input.structureLevel === 'internal'
    ? input.structureLevel
    : null
  if (level == null || resolved.direction == null) {
    return { ...resolved, structureLevel: level, label: '未知结构', arrow: '' }
  }
  return {
    ...resolved,
    structureLevel: level,
    label: `${LEVEL_LABEL[level]}${resolved.direction === 'up' ? '多头承接区' : '空头压制区'}`,
    arrow: resolved.direction === 'up' ? '↑' : '↓',
  }
}

export function getSmcEventLabel(type: SmcEventType, bias: number | null | undefined, structureLevel: SmcStructureLevel = 'swing'): string {
  const semantic = formatSmcEvent({ type, bias, structureLevel })
  return semantic.label
}

/** EQH/EQL 没有结构级别，不虚构主要/短线语义。 */
export function getSmcEqLabel(type: SmcEqType): string {
  return type === 'EQH' ? '双顶压力' : '双底支撑'
}

export function getSmcObLabel(bias: number | null | undefined, structureLevel: SmcStructureLevel = 'swing'): string {
  const semantic = formatSmcOrderBlock({ bias, structureLevel })
  return semantic.label
}
