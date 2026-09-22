// [R3C] Explorer 表格（纯展示组件）：列顺序锁死、可排序、NULL → —、涨跌幅按 A 股红涨绿跌。
//
// [PANJI-REVIEW-UI-UNIFY] 表头统一（与 StrategyDataTable 同视觉）：
// - 42px 单行表头（不再自适应多行）；
// - **排序区与筛选区是两个独立点击区域**：点 label 排序，点 funnel 筛选，互不触发；
// - 仅 backend 当前真实支持数值筛选的 5 列（成员数 / MA5 / MA5Δ / MA10 / MA10Δ）显示 funnel；
//   MA20 / MA50 / MA120 没有对应 filter 参数 → 只可排序，绝不放「看起来能筛、实际做不到」的假按钮；
// - 表格本身仍由 URL state 驱动（本组件只渲染 + 回调，不持有筛选状态）。
import clsx from 'clsx'
import type { ScopeExplorerItem } from './types'
import type { ScopeExplorerSort, SortDirection } from './scopeExplorerQuery'
import { formatBreadth, formatDelta, deltaDirection } from './dashboardLogic'
import TableFilterIcon from '@/components/TableFilterIcon'
import {
  EXPLORER_FILTER_COLUMN_SPECS,
  FILTER_COLUMN_BY_FIELD,
  type ExplorerFilterColumn,
} from './scopeExplorerUrlState'
import styles from './dashboard.module.scss'

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
  label,
  sort,
  direction,
  onSort,
  filteredColumns,
  onFilterClick,
}: {
  field: ScopeExplorerSort
  label: string
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
    <th className={styles['th-sort']}>
      <div className={styles['th-head']}>
        <button
          type="button"
          className={styles['sort-btn']}
          onClick={() => onSort(field)}
          aria-pressed={active}
          data-testid={`sort-${field}`}
        >
          <span className={styles['th-label']}>{label}</span>
          <span className={styles['sort-ind']}>{ind}</span>
        </button>
        {filterColumn && (
          <button
            type="button"
            className={clsx(styles['th-filter'], isFiltered && styles['active'])}
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
}: Props) {
  const headerProps = { sort, direction, onSort, filteredColumns, onFilterClick }
  return (
    <div className={styles['rank-table']} data-testid="explorer-table">
      <table className={styles.table}>
        <thead>
          <tr>
            <HeaderCell field="name" label="行业 / 概念" {...headerProps} />
            <HeaderCell field="member_count" label="成员数" {...headerProps} />
            <HeaderCell field="ma5" label="MA5" {...headerProps} />
            <HeaderCell field="ma5_delta" label="5日Δ" {...headerProps} />
            <HeaderCell field="ma10" label="MA10" {...headerProps} />
            <HeaderCell field="ma10_delta" label="5日Δ" {...headerProps} />
            <HeaderCell field="ma20" label="MA20" {...headerProps} />
            <HeaderCell field="ma50" label="MA50" {...headerProps} />
            <HeaderCell field="ma120" label="MA120" {...headerProps} />
            <th className={styles['th-op']}>操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const selected = item.board_id === selectedId
            return (
              <tr
                key={item.board_id}
                className={selected ? styles['row-selected'] : undefined}
                onClick={() => onSelect(item.board_id)}
                data-board-id={item.board_id}
                data-testid={`row-${item.board_id}`}
              >
                <td className={styles['name-cell']}>{item.board_name}</td>
                <td className={styles['num-cell']}>{item.member_count.toLocaleString()}</td>
                <td className={styles['num-cell']}>{formatBreadth(item.ma5)}</td>
                <td className={styles['num-cell']}>{deltaCell(item.ma5_delta)}</td>
                <td className={styles['num-cell']}>{formatBreadth(item.ma10)}</td>
                <td className={styles['num-cell']}>{deltaCell(item.ma10_delta)}</td>
                <td className={styles['num-cell']}>{formatBreadth(item.ma20)}</td>
                <td className={styles['num-cell']}>{formatBreadth(item.ma50)}</td>
                <td className={styles['num-cell']}>{formatBreadth(item.ma120)}</td>
                <td className={styles['op-cell']}>
                  <button
                    type="button"
                    className={styles['compare-btn']}
                    onClick={(e) => {
                      e.stopPropagation()
                      onAddCompare(item)
                    }}
                  >
                    加入对比
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export { SORTABLE }
