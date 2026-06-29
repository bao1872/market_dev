// [趋势选股] - 主页可见列配置
// 职责：维护主页"最新趋势快照"显示的摘要列 key 列表 + 按 key 过滤列的辅助函数
// 设计：仅含类型导入（import type），Node --experimental-strip-types 可直接执行（用于契约测试）
import type { DataTableColumn } from '@/components/StrategyDataTable'
import type { TrendSelectionRow } from './types.ts'

/**
 * [趋势选股] - 描述: 主页"最新趋势快照"显示的摘要列 key 列表
 * 必须是 getTrendSelectionColumns() 返回的完整列集的子集
 * 主页通过 visibleColumnKeys(fullColumns, INDEX_VISIBLE_COLUMN_KEYS) 过滤显示
 */
export const INDEX_VISIBLE_COLUMN_KEYS = [
  'stock',
  'dsa_dir_bars',
  'vwap_ret_avg',
  'offset_mean',
  'action',
] as const

/**
 * [趋势选股] - 描述: 按 column key 过滤列定义，返回完整列集中顺序的子集
 * 主页与完整页共用同一列定义，通过此函数控制主页显示的列子集
 * - 按 column key 过滤，不依赖传入 keys 的数组顺序
 * - 输出顺序始终与完整列集顺序一致，避免主页与完整页因数组位置不同而错位
 * - 返回的列对象与完整列同 key 是同一引用，确保 title/format/颜色规则完全一致
 */
export function visibleColumnKeys(
  columns: DataTableColumn<TrendSelectionRow>[],
  keys: readonly string[],
): DataTableColumn<TrendSelectionRow>[] {
  const keySet = new Set(keys)
  return columns.filter((c) => keySet.has(c.key))
}
