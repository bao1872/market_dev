// [PresentationSemanticRegistry] - 描述: 第一金字塔 / 行情 / 自选 用户展示语义唯一中枢
//
// 设计原则（Commit C / P0 展示语义统一 2026-09-08）：
//   - canonical value（后端下发的稳定 key / 中文枚举）→ 用户展示 label 的唯一映射层
//   - SMC 结构事件（BOS/CHoCH/OB/EQH/EQL）必须复用 @/components/smcLabels，
//     禁止在此复制其文字矩阵（smcLabels 为唯一真源）
//   - 未知 canonical 值一律显式表达（方向未知 / 级别未知 / 结构未知 / 未知状态），
//     绝不把内部 code 原样吐给 DOM（防以后开发者又加 ?? rawValue）
//   - 三轴语义严格区分（见用户裁决）：
//       结构事件方向 → 多头 / 空头
//       动量方向     → 偏多 / 偏空 / 中性
//       趋势方向     → 上行 / 下行 / 震荡
//
// 本文件不依赖 React，纯函数便于单测。

import {
  formatSmcEvent,
  formatSmcOrderBlock,
  getSmcEqLabel,
  type SmcDirectionInput,
} from '../../components/smcLabels'

const UNKNOWN = {
  structure: '结构未知',
  direction: '方向未知',
  level: '级别未知',
  state: '未知状态',
} as const

// ===== 结构事件方向（多头/空头，非 偏多/偏空）=====
export function formatStructureDirection(raw: unknown): string {
  if (raw === 'bullish' || raw === 'up') return '多头'
  if (raw === 'bearish' || raw === 'down') return '空头'
  return UNKNOWN.direction
}

// ===== 结构事件级别（主要级别/短线级别）=====
export function formatStructureLevel(raw: unknown): string {
  if (raw === 'swing') return '主要级别'
  if (raw === 'internal') return '短线级别'
  return UNKNOWN.level
}

// ===== 结构事件完整语义（组合 type + direction + level），调用 smcLabels =====
export function formatStructureEvent(input: {
  type?: string | null
  direction?: string | null
  level?: string | null
}): string {
  const type = input.type
  if (type === 'EQH') return getSmcEqLabel('EQH')
  if (type === 'EQL') return getSmcEqLabel('EQL')
  if (type === 'BOS' || type === 'CHoCH') {
    return formatSmcEvent({
      type,
      structureLevel: input.level ?? undefined,
      direction: (input.direction ?? undefined) as SmcDirectionInput,
    }).label
  }
  if (typeof type === 'string' && type.startsWith('OB')) {
    return formatSmcOrderBlock({
      structureLevel: input.level ?? undefined,
      direction: (input.direction ?? undefined) as SmcDirectionInput,
    }).label
  }
  return UNKNOWN.structure
}

// ===== 动量方向（扩张/收缩/平缓 → 偏多/偏空/中性，向详情页 sqzmom 语义靠齐）=====
export function formatMomentumDirection(raw: unknown): string {
  if (raw === '扩张') return '偏多'
  if (raw === '收缩') return '偏空'
  if (raw === '平缓') return '中性'
  return UNKNOWN.state
}

// ===== 动量事件类型 =====
export function formatMomentumEvent(raw: unknown): string {
  if (raw === 'SQZ_OFF') return '挤压释放'
  if (raw === 'MOMENTUM_DIFFUSION') return '动量扩散'
  return UNKNOWN.state
}

// ===== 挤压状态（后端已下发中文，passthrough + fail-closed）=====
export function formatSqueezeState(raw: unknown): string {
  if (raw === '挤压中' || raw === '已释放' || raw === '无挤压') return raw as string
  if (raw === 'squeeze_on') return '挤压中'
  if (raw === 'released') return '已释放'
  if (raw === 'no_squeeze') return '无挤压'
  if (raw === null || raw === undefined || raw === '') return '—'
  return UNKNOWN.state
}

// ===== 结构对齐（仅 fp_structure_alignment）：共振/背离 → 长短结构同向/长短结构分歧 =====
export function formatAlignment(raw: unknown): string {
  if (raw === '共振') return '长短结构同向'
  if (raw === '背离') return '长短结构分歧'
  return UNKNOWN.state
}

// ===== 筹码节点事件类型 =====
export function formatNodeEventType(raw: unknown): string {
  if (raw === 'node_cluster_touch') return '节点簇触及'
  return UNKNOWN.state
}

// ===== 布尔（是/否）=====
export function formatBoolean(raw: unknown): string {
  if (raw === true) return '是'
  if (raw === false) return '否'
  if (raw === null || raw === undefined) return '—'
  return UNKNOWN.state
}

// ===== 趋势方向（上行/下行/震荡，fail-closed）=====
export function formatTrendDirection(raw: unknown): string {
  const s = raw == null ? '' : String(raw)
  if (s === '上行' || s === 'up') return '上行'
  if (s === '下行' || s === 'down') return '下行'
  if (s === '震荡' || s === 'sideways') return '震荡'
  return UNKNOWN.direction
}

// ===== 筛选器 enumOptions（canonical value → 展示 label；提交仍为 canonical value）=====
export const STRUCTURE_EVENT_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'BOS', label: '结构突破' },
  { value: 'CHoCH', label: '结构转折' },
  { value: 'OB_CREATED', label: '承接/压制区(建)' },
  { value: 'OB_ENTERED', label: '承接/压制区(入)' },
  { value: 'OB_MITIGATED', label: '承接/压制区(消)' },
  { value: 'EQH', label: '双顶压力' },
  { value: 'EQL', label: '双底支撑' },
]

export const EVENT_DIRECTION_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'bullish', label: '多头' },
  { value: 'bearish', label: '空头' },
  { value: 'up', label: '多头' },
  { value: 'down', label: '空头' },
]

export const STRUCTURE_LEVEL_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'swing', label: '主要级别' },
  { value: 'internal', label: '短线级别' },
]

export const MOMENTUM_DIRECTION_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '扩张', label: '偏多' },
  { value: '收缩', label: '偏空' },
  { value: '平缓', label: '中性' },
]

export const MOMENTUM_EVENT_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'SQZ_OFF', label: '挤压释放' },
  { value: 'MOMENTUM_DIFFUSION', label: '动量扩散' },
]

export const ALIGNMENT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '共振', label: '长短结构同向' },
  { value: '背离', label: '长短结构分歧' },
]

export const NODE_EVENT_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'node_cluster_touch', label: '节点簇触及' },
]
