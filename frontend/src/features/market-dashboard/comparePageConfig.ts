// [MarketDashboard][R3D] - 比较页冻结配置（表格列 / 范围 / 文案 的唯一真源）。
//
// 设计约束：
//   - 范围只用于 `useMarketCompare(boardIds, range)`；前端不先请求 60 再 slice。
//   - 矩阵列顺序锁死（与 R3D0 后端字段 1:1），表格与测试共用本真源，杜绝漂移。
//   - 该模块**纯逻辑**：不依赖 React / axios，可被 node:test 直接加载。
import type { ScopeType } from './types'

/** 比较页时间范围（server window）。默认 10。 */
export const COMPARE_RANGES = [10, 20, 60] as const
export type CompareRange = (typeof COMPARE_RANGES)[number]
export const COMPARE_DEFAULT_RANGE: CompareRange = 10

/** 矩阵列（锁死顺序）。board_type 在前端与后端同为 'industry' / 'concept'。 */
export type CompareMatrixColumn =
  | 'name'
  | 'type'
  | 'ma5'
  | 'ma5_delta'
  | 'ma10'
  | 'ma10_delta'
  | 'ma20'
  | 'ma50'
  | 'ma120'
  | 'member_count'

export const COMPARE_MATRIX_COLUMNS: readonly CompareMatrixColumn[] = [
  'name',
  'type',
  'ma5',
  'ma5_delta',
  'ma10',
  'ma10_delta',
  'ma20',
  'ma50',
  'ma120',
  'member_count',
]

/** 类型标签：industry → 行业 / concept → 概念（不自行计算，仅展示）。 */
export function boardTypeLabel(type: ScopeType | string | null | undefined): string {
  if (type === 'concept') return '概念'
  if (type === 'industry') return '行业'
  return '—'
}

/** 成员数：整数 locale 显示；None → —（不得显示 0 冒充 NULL）。 */
export function formatMemberCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString()
}
