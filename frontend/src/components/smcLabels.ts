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

/** SMC bias 值（1=bullish, -1=bearish，与后端 DTO bias 字段对齐） */
export type SmcBias = 1 | -1

/** SMC 等高/等低类型（与后端 DTO type 字段对齐） */
export type SmcEqType = 'EQH' | 'EQL'

/**
 * BOS/CHoCH 事件中文标签（按 bias 区分多空）。
 *
 * @param type 事件类型 'BOS' | 'CHoCH'
 * @param bias 1=bullish, -1=bearish；其他值（如 0）回退到 bullish 标签
 * @returns 中文标签字符串
 */
export function getSmcEventLabel(type: SmcEventType, bias: number | null | undefined): string {
  const isBull = bias === 1
  if (type === 'BOS') {
    return isBull ? '突破前高' : '跌破前低'
  }
  if (type === 'CHoCH') {
    return isBull ? '转强拐点' : '转弱拐点'
  }
  return type
}

/**
 * EQH/EQL 中文标签。
 *
 * @param type 'EQH' | 'EQL'
 * @returns "双顶压力" | "双底支撑"
 */
export function getSmcEqLabel(type: SmcEqType): string {
  return type === 'EQH' ? '双顶压力' : '双底支撑'
}

/**
 * Order Block 中文标签（按 bias 区分多空）。
 *
 * @param bias 1=bullish, -1=bearish；其他值回退到 bullish 标签
 * @returns "多头承接区" | "空头压制区"
 */
export function getSmcObLabel(bias: number | null | undefined): string {
  const isBull = bias === 1
  return isBull ? '多头承接区' : '空头压制区'
}
