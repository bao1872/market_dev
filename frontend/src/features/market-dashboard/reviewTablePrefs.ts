// [PANJI-REVIEW-UI-RUNTIME-PARITY-FIX][§7] Review 表格列显隐偏好（纯模块，可单测）。
//
// 语义边界：
// - 这是 **Review 自己的**本地偏好 owner，**不共享** Market（StrategyDataTable）的持久化命名空间，
//   也不写入任何 server state —— 筛选 / 排序 / 分页 / 搜索仍 100% 由 URL 拥有。
// - 按「Review 表身份」隔离：industry 与 concept 各自独立的 key（两者列可用性未来可分化）。
// - name（身份列）/ operation（操作列）永不隐藏。
// - 存储访问放在本模块，页面只调用 read/write —— 偏好失败（隐私模式 / 配额）必须静默降级，不阻塞 UI。
import { EXPLORER_TABLE_COLUMNS } from './scopeExplorerUrlState'

export type ExplorerTableColumn = (typeof EXPLORER_TABLE_COLUMNS)[number]

/** 不可隐藏的列：身份列（name）+ 操作列（operation）。 */
export const NON_HIDEABLE_COLUMNS: readonly ExplorerTableColumn[] = ['name', 'operation']

/** 可隐藏的列（列设置里逐项列出）。 */
export const MANAGEABLE_COLUMNS: readonly ExplorerTableColumn[] = [
  'member_count',
  'ma5',
  'ma5_delta',
  'ma10',
  'ma10_delta',
  'ma20',
  'ma50',
  'ma120',
]

/** 偏好 key：`review:<tableId>:hiddenColumns`（tableId 形如 review-industry / review-concept）。 */
export function reviewTablePrefKey(tableId: string): string {
  return `review:${tableId}:hiddenColumns`
}

type PrefStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): PrefStorage | null {
  return typeof window === 'undefined' ? null : window.localStorage
}

function isTableColumn(value: string): value is ExplorerTableColumn {
  return (EXPLORER_TABLE_COLUMNS as readonly string[]).includes(value)
}

function isManageable(value: ExplorerTableColumn): boolean {
  return MANAGEABLE_COLUMNS.includes(value)
}

/** 读取被隐藏的列；非法 / 不可隐藏 / 解析失败的值一律丢弃。 */
export function readHiddenColumns(
  tableId: string,
  storage: PrefStorage | null = defaultStorage(),
): ReadonlySet<ExplorerTableColumn> {
  const out = new Set<ExplorerTableColumn>()
  if (!storage) return out
  try {
    const raw = storage.getItem(reviewTablePrefKey(tableId))
    if (!raw) return out
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return out
    for (const value of parsed) {
      if (typeof value !== 'string' || !isTableColumn(value)) continue
      if (!isManageable(value)) continue
      out.add(value)
    }
  } catch {
    return new Set()
  }
  return out
}

/** 写入被隐藏的列；写失败静默忽略（偏好不是业务状态）。 */
export function writeHiddenColumns(
  tableId: string,
  hidden: ReadonlySet<ExplorerTableColumn>,
  storage: PrefStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(reviewTablePrefKey(tableId), JSON.stringify([...hidden]))
  } catch {
    // 隐私模式 / 配额耗尽：忽略，不影响页面
  }
}

/** 切换单列显隐（返回新 Set，不修改入参）。 */
export function toggleHiddenColumn(
  hidden: ReadonlySet<ExplorerTableColumn>,
  key: ExplorerTableColumn,
): Set<ExplorerTableColumn> {
  const next = new Set(hidden)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return next
}
