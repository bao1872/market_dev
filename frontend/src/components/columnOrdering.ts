// [ColumnOrdering] - 描述: 可见列派生与列顺序重排纯函数（P0 列对齐测试目标）
//
// 职责：
//   1. 从 columns + hiddenColumns 派生 visibleColumns（保留 originalIndex 映射）
//   2. 当 columnOrder 非空时按其顺序排列管理列（action/select 列固定在末尾，不参与重排）
//   3. 不依赖数组下标，仅按 col.key 派生与排序
//
// 不变量（P0 列对齐契约）：
//   - 表头 th、表体 td、colgroup col 三者必须从同一 visibleColumns 派生
//   - 每行 td 数 = 可见 th 数 = visibleColumns.length
//   - 单元格按 col.key 取值，禁止依赖数组下标或对象遍历顺序
//   - action/select 列固定 id，不参与重排，始终在末尾
//
// 提取自 StrategyDataTable.tsx，确保列对齐逻辑可独立测试。

export interface ColumnLike {
  key: string
  isAction?: boolean
  isSelect?: boolean
}

export interface IndexedColumn<Col extends ColumnLike> {
  col: Col
  originalIndex: number
}

/**
 * 从 columns + hiddenColumns + columnOrder 派生可见列列表。
 *
 * @param columns 原始列定义（保持声明顺序）
 * @param hiddenColumns 隐藏列 key 集合
 * @param columnOrder 自定义列顺序（null/空数组表示按原始顺序）；action/select 列不参与重排
 * @returns 可见列列表（携带 originalIndex），action/select 列固定在末尾
 */
export function reorderVisibleColumns<Col extends ColumnLike>(
  columns: Col[],
  hiddenColumns: Set<string>,
  columnOrder: string[] | null,
): IndexedColumn<Col>[] {
  const indexed = columns.map((col, originalIndex) => ({ col, originalIndex }))
  const visible = indexed.filter(({ col }) => !hiddenColumns.has(col.key))
  if (!columnOrder || columnOrder.length === 0) return visible

  const orderMap = new Map<string, number>()
  columnOrder.forEach((k, i) => orderMap.set(k, i))
  const manageable = visible.filter(({ col }) => !col.isAction && !col.isSelect)
  const fixed = visible.filter(({ col }) => col.isAction || col.isSelect)
  manageable.sort((a, b) => {
    const ai = orderMap.has(a.col.key) ? orderMap.get(a.col.key)! : 9999
    const bi = orderMap.has(b.col.key) ? orderMap.get(b.col.key)! : 9999
    return ai - bi
  })
  return [...manageable, ...fixed]
}
