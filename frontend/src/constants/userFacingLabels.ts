// 用户可见文案常量 - 把内部事件类型/字段名映射为普通用户能看懂的中文文案
// 设计目的（advice.md 第二节）：飞书消息和消息中心原文案过于专业（"BB上轨穿越"/"POC"等），普通用户难理解
// 统一收敛到本文件，作为前端唯一权威实现，禁止在组件层再散落 switch/dict
//
// 注意：
// - 内部 event_type 值（如 bb_upper_touch）保持不变，仅改展示文案
// - emoji 与文案分离，emoji 仍在各组件维护
//
// 用法：
//   import { getEventLabel } from '@/constants/userFacingLabels'
//   const label = getEventLabel('bb_upper_touch') // '价格触及近期波动上沿'

/** 事件类型 → 通俗文案（用户在飞书/消息中心看到的名称） */
export const EVENT_LABELS = {
  bb_upper_touch: '价格触及近期波动上沿',
  bb_mid_touch: '价格回到近期价格中枢',
  bb_lower_touch: '价格触及近期波动下沿',
  node_cluster_touch: '价格触及成交密集区',
  // [StockDetailFeishu] - 个股快照主动分享（不暴露内部 manual_send 代码）
  STOCK_SNAPSHOT_SHARE: '个股快照',
} as const

/** 字段名 → 通俗文案（消息详情/自选监控表格中各数据行的标签） */
export const FIELD_LABELS = {
  bb_upper: '近期波动上沿',
  bb_mid: '近期价格中枢',
  bb_lower: '近期波动下沿',
  upper_node: '上方成交密集区',
  lower_node: '下方成交密集区',
  poc: '最密集成交价',
  position: '当前区间位置',
  // 概览行用的简称（保持单行紧凑可读）
  bb_upper_short: '波动上沿',
  bb_mid_short: '价格中枢',
  bb_lower_short: '波动下沿',
  node_cluster_short: '密集区',
} as const

/** 事件类型联合类型（来自 EVENT_LABELS 的 key） */
export type EventType = keyof typeof EVENT_LABELS

/** 字段名联合类型（来自 FIELD_LABELS 的 key） */
export type FieldName = keyof typeof FIELD_LABELS

/**
 * 查询事件类型对应的通俗文案，未知则返回原 eventType。
 * @param eventType 内部事件类型（如 'bb_upper_touch'）
 * @returns 用户可见文案（如 '价格触及近期波动上沿'）；未知返回原值
 */
export function getEventLabel(eventType: string): string {
  if (eventType in EVENT_LABELS) {
    return EVENT_LABELS[eventType as EventType]
  }
  return eventType
}

/**
 * 查询字段名对应的通俗文案，未知则返回原 field。
 * @param field 内部字段名（如 'bb_upper' / 'upper_node' / 'poc' / 'position'）
 * @returns 用户可见文案（如 '近期波动上沿'）；未知返回原值
 */
export function getFieldLabel(field: string): string {
  if (field in FIELD_LABELS) {
    return FIELD_LABELS[field as FieldName]
  }
  return field
}
