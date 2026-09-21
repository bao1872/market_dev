// [R3C] Explorer 表格（纯展示组件）：列顺序锁死、可排序、NULL → —、涨跌幅按 A 股红涨绿跌。
import type { ScopeExplorerItem } from './types'
import type { ScopeExplorerSort, SortDirection } from './scopeExplorerQuery'
import { formatBreadth, formatDelta, deltaDirection } from './dashboardLogic'
import styles from './dashboard.module.scss'

interface Props {
  items: ScopeExplorerItem[]
  sort: ScopeExplorerSort
  direction: SortDirection
  onSort: (field: ScopeExplorerSort) => void
  selectedId: string | null
  onSelect: (id: string) => void
  onAddCompare: (item: ScopeExplorerItem) => void
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

function SortHeader({
  field,
  label,
  sort,
  direction,
  onSort,
}: {
  field: ScopeExplorerSort
  label: string
  sort: ScopeExplorerSort
  direction: SortDirection
  onSort: (f: ScopeExplorerSort) => void
}) {
  const active = sort === field
  const ind = active ? (direction === 'asc' ? '▲' : '▼') : ''
  return (
    <th className={styles['th-sort']}>
      <button
        type="button"
        className={styles['sort-btn']}
        onClick={() => onSort(field)}
        aria-pressed={active}
        data-testid={`sort-${field}`}
      >
        <span>{label}</span>
        <span className={styles['sort-ind']}>{ind}</span>
      </button>
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
}: Props) {
  return (
    <div className={styles['rank-table']} data-testid="explorer-table">
      <table className={styles.table}>
        <thead>
          <tr>
            <SortHeader field="name" label="行业 / 概念" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="member_count" label="成员数" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma5" label="MA5" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma5_delta" label="5日Δ" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma10" label="MA10" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma10_delta" label="5日Δ" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma20" label="MA20" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma50" label="MA50" sort={sort} direction={direction} onSort={onSort} />
            <SortHeader field="ma120" label="MA120" sort={sort} direction={direction} onSort={onSort} />
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
