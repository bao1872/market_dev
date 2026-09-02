// [CHANGE-20260902] 排序合同统一校验（纯函数，便于单测）：
// 仅当列存在且 column.sortable===true 时才接受排序。
// 用于 StrategyDataTable 的 URL hydration / preset 应用 / 当前 query 派生三处，
// 避免旧 URL、session、默认 preset 或用户 preset 中残留的非法 sort
// （如 stock/industry/text/datetime 等非数值字段）被重新发给 API。
// 返回匹配列的索引；非法或缺失时返回 -1（表示不排序，不发送 sort 参数）。筛选不受影响。

interface SortableColumnLike {
  key: string
  sortable?: boolean
}

export function resolveValidSort(
  sort: { key: string; direction: 'asc' | 'desc' } | undefined,
  columns: SortableColumnLike[],
): number {
  if (!sort || !sort.key) return -1
  const idx = columns.findIndex((c) => c.key === sort.key)
  if (idx < 0) return -1
  if (!columns[idx]?.sortable) return -1
  return idx
}
