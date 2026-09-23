// [R3C] Explorer 表格（纯展示组件）：列顺序锁死、可排序、NULL → —、涨跌幅按 A 股红涨绿跌。
//
// [PANJI-REVIEW-UI-UNIFY] 表头统一（与 StrategyDataTable 同视觉）：
// - 42px 单行表头（不再自适应多行）；
// - **排序区与筛选区是两个独立点击区域**：点 label 排序，点 funnel 筛选，互不触发；
// - 仅 backend 当前真实支持数值筛选的 5 列（成员数 / MA5 / MA5Δ / MA10 / MA10Δ）显示 funnel；
//   MA20 / MA50 / MA120 没有对应 filter 参数 → 只可排序，绝不放「看起来能筛、实际做不到」的假按钮；
// - 表格本身仍由 URL state 驱动（本组件只渲染 + 回调，不持有筛选状态）。
//
// [PANJI-REVIEW-UI-RUNTIME-PARITY-FIX][§5] 表格 chrome **不再由 CSS module 重复实现**：
// 直接消费 global.scss 的 canonical 表格语言（table-shell / table-scroll / data-table
// interactive-table / th-shell / th-sort / th-label / sort-icon / th-filter），
// 与 StrategyDataTable 共用同一个 presentation owner。此前 module 里那份
// `.rank-table/.table/.th-head/.th-sort/.th-filter/...` 副本已删除。
import { Fragment } from 'react'
import clsx from 'clsx'
import type { ScopeExplorerItem } from './types'
import type { ScopeExplorerSort, SortDirection } from './scopeExplorerQuery'
import { formatBreadth, formatDelta, deltaDirection } from './dashboardLogic'
import TableFilterIcon from '@/components/TableFilterIcon'
import {
  EXPLORER_FILTER_COLUMN_SPECS,
  EXPLORER_TABLE_COLUMNS,
  FILTER_COLUMN_BY_FIELD,
  type ExplorerFilterColumn,
} from './scopeExplorerUrlState'
import type { ExplorerTableColumn } from './reviewTablePrefs'
import styles from './dashboard.module.scss'

// 列标题表（列**顺序**的唯一 owner 仍是 scopeExplorerUrlState.EXPLORER_TABLE_COLUMNS）。
export const EXPLORER_COLUMN_LABELS: Record<ExplorerTableColumn, string> = {
  name: '行业 / 概念',
  member_count: '成员数',
  ma5: 'MA5',
  ma5_delta: '5日Δ',
  ma10: 'MA10',
  ma10_delta: '5日Δ',
  ma20: 'MA20',
  ma50: 'MA50',
  ma120: 'MA120',
  operation: '操作',
}

interface Props {
  items: ScopeExplorerItem[]
  sort: ScopeExplorerSort
  direction: SortDirection
  onSort: (field: ScopeExplorerSort) => void
  selectedId: string | null
  onSelect: (id: string) => void
  onAddCompare: (item: ScopeExplorerItem) => void
  /** 已激活筛选的列（由 URL SSOT 派生，用于 funnel active 态） */
  filteredColumns: ReadonlySet<ExplorerFilterColumn>
  /** 点击列 funnel；anchor 供弹层定位 */
  onFilterClick: (column: ExplorerFilterColumn, anchor: HTMLElement) => void
  /** 列设置（§7）：被隐藏的列。name / operation 永不隐藏。 */
  hiddenColumns: ReadonlySet<string>
}

const SORTABLE: readonly ScopeExplorerSort[] = [
  'name',
  'member_count',
  'ma5',
  'ma5_delta',
  'ma10',
  'ma10_delta',
  'ma20',
  'ma50',
  'ma120',
]

// 列 → 该列对应的数值筛选列由纯模块 scopeExplorerUrlState.FILTER_COLUMN_BY_FIELD 定义
// （**只有列在那里出现才渲染 funnel**；MA20/MA50/MA120 只可排序）。

function HeaderCell({
  field,
  sort,
  direction,
  onSort,
  filteredColumns,
  onFilterClick,
}: {
  field: ScopeExplorerSort
  sort: ScopeExplorerSort
  direction: SortDirection
  onSort: (f: ScopeExplorerSort) => void
  filteredColumns: ReadonlySet<ExplorerFilterColumn>
  onFilterClick: (column: ExplorerFilterColumn, anchor: HTMLElement) => void
}) {
  const active = sort === field
  const ind = active ? (direction === 'asc' ? '▲' : '▼') : ''
  const filterColumn = FILTER_COLUMN_BY_FIELD[field]
  const isFiltered = filterColumn !== undefined && filteredColumns.has(filterColumn)
  const filterLabel = filterColumn ? EXPLORER_FILTER_COLUMN_SPECS[filterColumn].filterLabel : ''
  return (
    <th>
      <div className="th-shell">
        <button
          type="button"
          className="th-sort"
          onClick={() => onSort(field)}
          aria-pressed={active}
          data-testid={`sort-${field}`}
        >
          <span className="th-label">{EXPLORER_COLUMN_LABELS[field]}</span>
          <span className="sort-icon">{ind}</span>
        </button>
        {filterColumn && (
          <button
            type="button"
            className={clsx('th-filter', isFiltered && 'active')}
            aria-pressed={isFiltered}
            aria-label={`筛选${filterLabel}`}
            title={`筛选${filterLabel}`}
            data-testid={`filter-${filterColumn}`}
            onClick={(e) => {
              e.stopPropagation()
              onFilterClick(filterColumn, e.currentTarget)
            }}
          >
            <TableFilterIcon />
          </button>
        )}
      </div>
    </th>
  )
}

function deltaCell(value: number | null) {
  return <span className={styles[deltaDirection(value)]}>{formatDelta(value)}</span>
}

/** 单个数据单元格（列顺序由 EXPLORER_TABLE_COLUMNS 驱动）。 */
function bodyCell(key: ExplorerTableColumn, item: ScopeExplorerItem) {
  switch (key) {
    case 'name':
      return <td className={styles.nameCell}>{item.board_name}</td>
    case 'member_count':
      return <td className={styles.numCell}>{item.member_count.toLocaleString()}</td>
    case 'ma5':
      return <td className={styles.numCell}>{formatBreadth(item.ma5)}</td>
    case 'ma5_delta':
      return <td className={styles.numCell}>{deltaCell(item.ma5_delta)}</td>
    case 'ma10':
      return <td className={styles.numCell}>{formatBreadth(item.ma10)}</td>
    case 'ma10_delta':
      return <td className={styles.numCell}>{deltaCell(item.ma10_delta)}</td>
    case 'ma20':
      return <td className={styles.numCell}>{formatBreadth(item.ma20)}</td>
    case 'ma50':
      return <td className={styles.numCell}>{formatBreadth(item.ma50)}</td>
    case 'ma120':
      return <td className={styles.numCell}>{formatBreadth(item.ma120)}</td>
    case 'operation':
      return null // 单独处理（需要 item 上下文）
  }
}

export default function ScopeExplorerTable({
  items,
  sort,
  direction,
  onSort,
  selectedId,
  onSelect,
  onAddCompare,
  filteredColumns,
  onFilterClick,
  hiddenColumns,
}: Props) {
  const headerProps = { sort, direction, onSort, filteredColumns, onFilterClick }
  return (
    <div className="table-shell" data-testid="explorer-table">
      <div className="table-scroll">
        <table className="data-table interactive-table">
          <thead>
            <tr>
              {EXPLORER_TABLE_COLUMNS.map((key) => {
                if (hiddenColumns.has(key)) return null
                if (key === 'operation') {
                  return (
                    <th key={key} className={styles.thOp}>
                      操作
                    </th>
                  )
                }
                return <HeaderCell key={key} field={key} {...headerProps} />
              })}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const selected = item.board_id === selectedId
              return (
                <tr
                  key={item.board_id}
                  className={selected ? styles.rowSelected : undefined}
                  onClick={() => onSelect(item.board_id)}
                  data-board-id={item.board_id}
                  data-testid={`row-${item.board_id}`}
                >
                  {EXPLORER_TABLE_COLUMNS.map((key) => {
                    if (hiddenColumns.has(key)) return null
                    if (key === 'operation') {
                      return (
                        <td key={key} className={styles.opCell}>
                          <button
                            type="button"
                            className={styles.compareBtn}
                            onClick={(e) => {
                              e.stopPropagation()
                              onAddCompare(item)
                            }}
                          >
                            加入对比
                          </button>
                        </td>
                      )
                    }
                    return <Fragment key={key}>{bodyCell(key, item)}</Fragment>
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// React 需要 key 时用 Fragment（bodyCell 已返回 <td>）。

export { SORTABLE }
