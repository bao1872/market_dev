// StrategyDataTable：通用数据表组件（V1.5.1）
// 对应原型 app.js InteractiveTable 类
// 必需能力：三态排序、逐列筛选、服务端分页、固定表头首列、列设置、空态/错误态/过期态
// 所有用户端和管理员端数据表必须使用同一表格组件
import { useState, useMemo, useCallback, useEffect, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import clsx from 'clsx'

// ===== 类型定义（对应 UI_DEVELOPMENT_SPEC.md 3.3 推荐组件输入）=====
export type DataType = 'text' | 'number' | 'percent' | 'datetime' | 'enum' | 'range'
export type SortDirection = 'asc' | 'desc' | null
export type FilterOperator = 'contains' | 'eq' | 'gt' | 'gte' | 'lt' | 'lte' | 'between' | 'empty' | 'not_empty'

export interface DataTableColumn<Row> {
  key: string
  title: string
  dataType: DataType
  sortable: boolean
  filterable: boolean
  enumOptions?: Array<{ label: string; value: string }>
  render?: (row: Row) => ReactNode
  // 用于排序和筛选的原始值提取（默认从 row[key] 取）
  sortValue?: (row: Row) => string | number
  filterValue?: (row: Row) => string
  width?: number
  // V1.5.1：操作列不参与排序与筛选
  isAction?: boolean
  // V1.5.1：选择列
  isSelect?: boolean
  // [StrategyDataTable] - 描述: 表头旁 ? tooltip 帮助文本（hover 显示）
  helpText?: string
}

export interface DataTableFilter {
  key: string
  operator: FilterOperator
  value?: string | number
  // [StrategyDataTable] - 描述: between 操作符的第二个值（上界）
  value2?: string | number
}

export interface DataTableQuery {
  page: number
  pageSize: number
  // [StrategyDataTable] - 描述: 全文搜索关键词（服务端模式透传至后端 keyword 参数）
  keyword?: string
  sort?: { key: string; direction: 'asc' | 'desc' }
  filters: DataTableFilter[]
}

export interface DataTableProps<Row> {
  columns: DataTableColumn<Row>[]
  rows: Row[]
  // 服务端分页模式
  total?: number
  serverSide?: boolean
  // 状态
  loading?: boolean
  error?: string | null
  stale?: boolean
  // 查询回调（服务端分页时调用）
  onQueryChange?: (query: DataTableQuery) => void
  // 表格唯一标识（用于 sessionStorage 持久化）
  tableId: string
  // 全文搜索
  searchable?: boolean
  // 行选择
  selectable?: boolean
  selectedKeys?: Set<string>
  onSelectionChange?: (keys: Set<string>) => void
  rowKey: (row: Row) => string
  // 空态文案
  emptyText?: string
  // [StrategyDataTable] - 描述: 初始每页条数（默认 10），服务端模式由调用方注入（如 ScreenerPage 50）
  initialPageSize?: number
}

// [StrategyDataTable] - 描述: 按字段类型返回可选操作符列表（默认操作符为数组首项）
function operatorsForDataType(dataType: DataTableColumn<unknown>['dataType']): FilterOperator[] {
  switch (dataType) {
    case 'number':
    case 'percent':
      return ['gte', 'gt', 'lte', 'lt', 'eq', 'between']
    case 'text':
      return ['contains', 'eq']
    case 'enum':
      return ['eq']
    case 'datetime':
      return ['gte', 'gt', 'lte', 'lt', 'between']
    default:
      return ['contains', 'eq']
  }
}

// [StrategyDataTable] - 描述: 操作符下拉框中文标签
const OPERATOR_LABELS: Record<FilterOperator, string> = {
  contains: '包含',
  eq: '等于',
  gt: '大于',
  gte: '大于等于',
  lt: '小于',
  lte: '小于等于',
  between: '区间',
  empty: '为空',
  not_empty: '不为空',
}

// 解析可比较值（对应原型 parseComparable）
function parseComparable(text: string): { type: 'number' | 'text'; value: number | string } {
  const clean = String(text).replace(/,/g, '').trim()
  const num = clean.match(/[-+]?\d+(?:\.\d+)?/)
  if (num) {
    let v = parseFloat(num[0])
    if (clean.includes('万')) v *= 10000
    if (clean.includes('M')) v *= 1000000
    if (clean.includes('%')) return { type: 'number', value: v }
    if (/^\d{1,2}:\d{2}/.test(clean)) {
      const [h, m, s = '0'] = clean.split(/[:\s]/)
      return { type: 'number', value: +h * 3600 + (+m) * 60 + (+s || 0) }
    }
    return { type: 'number', value: v }
  }
  return { type: 'text', value: clean.toLocaleLowerCase('zh-CN') }
}

// 筛选匹配逻辑
function matchFilter(text: string, filter: DataTableFilter): boolean {
  const t = String(text).trim()
  const a = parseComparable(t)
  const b = parseComparable(String(filter.value || ''))
  switch (filter.operator) {
    case 'empty':
      return !t
    case 'not_empty':
      return !!t
    case 'eq':
      return a.type === 'number' && b.type === 'number'
        ? a.value === b.value
        : t.toLocaleLowerCase('zh-CN') === String(filter.value).toLocaleLowerCase('zh-CN')
    case 'gt':
      // [StrategyDataTable] - 描述: 数值大于 value（仅数值语义，文本列不应出现 gt）
      return a.type === 'number' && b.type === 'number' && a.value > b.value
    case 'gte':
      return a.type === 'number' && b.type === 'number'
        ? a.value >= b.value
        : t.localeCompare(String(filter.value), 'zh-CN') >= 0
    case 'lt':
      // [StrategyDataTable] - 描述: 数值小于 value（仅数值语义）
      return a.type === 'number' && b.type === 'number' && a.value < b.value
    case 'lte':
      return a.type === 'number' && b.type === 'number'
        ? a.value <= b.value
        : t.localeCompare(String(filter.value), 'zh-CN') <= 0
    case 'between': {
      // [StrategyDataTable] - 描述: 数值在 [value, value2] 闭区间（仅数值语义）
      const c = parseComparable(String(filter.value2 || ''))
      return a.type === 'number' && b.type === 'number' && c.type === 'number'
        ? a.value >= b.value && a.value <= c.value
        : false
    }
    default: // contains
      return t.toLocaleLowerCase('zh-CN').includes(String(filter.value).toLocaleLowerCase('zh-CN'))
  }
}

// 列筛选弹窗
function FilterPopover({
  column,
  current,
  anchor,
  onApply,
  onClear,
  onClose,
}: {
  column: DataTableColumn<unknown>
  current: DataTableFilter | undefined
  anchor: HTMLElement
  onApply: (filter: DataTableFilter) => void
  onClear: () => void
  onClose: () => void
}) {
  // [StrategyDataTable] - 描述: 操作符默认值由字段类型决定，不再硬编码 contains
  const availableOps = useMemo(
    () => operatorsForDataType(column.dataType),
    [column.dataType],
  )
  const [operator, setOperator] = useState<FilterOperator>(
    current?.operator && availableOps.includes(current.operator)
      ? current.operator
      : availableOps[0],
  )
  const [value, setValue] = useState(String(current?.value || ''))
  const [value2, setValue2] = useState(String(current?.value2 || ''))
  const isEmptyOp = operator === 'empty' || operator === 'not_empty'
  const isBetween = operator === 'between'

  // 定位弹窗
  const rect = anchor.getBoundingClientRect()
  const left = Math.min(window.innerWidth - 250, Math.max(8, rect.left - 150))
  const top = Math.max(8, Math.min(window.innerHeight - 230, rect.bottom + 6))

  useEffect(() => {
    const close = (e: MouseEvent) => {
      const pop = document.querySelector('.column-filter-popover')
      if (pop && !pop.contains(e.target as Node) && e.target !== anchor) {
        onClose()
      }
    }
    setTimeout(() => document.addEventListener('mousedown', close), 0)
    return () => document.removeEventListener('mousedown', close)
  }, [anchor, onClose])

  const handleApply = () => {
    const val = value.trim()
    const val2 = value2.trim()
    // [StrategyDataTable] - 描述: between 需要两个值都非空，其余非空校验 value
    if (isBetween) {
      if (!val || !val2) {
        onClear()
        return
      }
      onApply({ key: column.key, operator, value: val, value2: val2 })
      return
    }
    if (!isEmptyOp && !val) {
      onClear()
      return
    }
    onApply({ key: column.key, operator, value: val })
  }

  return (
    <div className="column-filter-popover" style={{ left, top }}>
      <div className="filter-pop-title">筛选：{column.title}</div>
      <select
        className="select filter-operator"
        value={operator}
        onChange={(e) => setOperator(e.target.value as FilterOperator)}
      >
        {availableOps.map((op) => (
          <option key={op} value={op}>
            {OPERATOR_LABELS[op]}
          </option>
        ))}
      </select>
      {isBetween ? (
        <div className="filter-between-inputs">
          <input
            className="input filter-value"
            placeholder="下界"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoFocus
          />
          <span className="filter-between-sep">~</span>
          <input
            className="input filter-value"
            placeholder="上界"
            value={value2}
            onChange={(e) => setValue2(e.target.value)}
          />
        </div>
      ) : (
        <input
          className="input filter-value"
          placeholder="输入筛选值"
          value={value}
          disabled={isEmptyOp}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
      )}
      <div className="filter-pop-actions">
        <button className="btn small filter-clear" onClick={onClear}>
          清除
        </button>
        <button className="btn small primary filter-apply" onClick={handleApply}>
          应用
        </button>
      </div>
    </div>
  )
}

// 列设置弹窗
function ColumnManager({
  columns,
  hiddenColumns,
  onToggle,
  onReset,
  onClose,
  anchor,
}: {
  columns: DataTableColumn<unknown>[]
  hiddenColumns: Set<number>
  onToggle: (index: number) => void
  onReset: () => void
  onClose: () => void
  anchor: HTMLElement
}) {
  const rect = anchor.getBoundingClientRect()
  const left = Math.min(window.innerWidth - 260, Math.max(8, rect.left - 100))
  const top = Math.min(window.innerHeight - 330, rect.bottom + 6)

  const manageable = columns.filter((c) => !c.isAction && !c.isSelect)

  return (
    <div className="column-filter-popover column-manager-popover" style={{ left, top }}>
      <div className="filter-pop-title">显示列</div>
      <div className="column-manager-list">
        {manageable.map((col) => {
          const idx = columns.indexOf(col)
          return (
            <div key={col.key} className="column-manager-item">
              <label className="table-checkbox-wrapper" style={{ width: 24, height: 24 }}>
                <input
                  type="checkbox"
                  className="table-checkbox"
                  checked={!hiddenColumns.has(idx)}
                  onChange={() => onToggle(idx)}
                />
              </label>
              <span>{col.title}</span>
            </div>
          )
        })}
      </div>
      <div className="filter-pop-actions">
        <button className="btn small columns-reset" onClick={onReset}>
          恢复默认
        </button>
        <button className="btn small primary columns-close" onClick={onClose}>
          完成
        </button>
      </div>
    </div>
  )
}

export function StrategyDataTable<Row extends Record<string, unknown>>(
  props: DataTableProps<Row>,
) {
  const {
    columns,
    rows,
    total,
    serverSide = false,
    loading = false,
    error = null,
    stale = false,
    onQueryChange,
    tableId,
    searchable = true,
    selectable = false,
    selectedKeys,
    onSelectionChange,
    rowKey,
    emptyText = '没有符合筛选条件的数据',
    initialPageSize = 10,
  } = props

  const [searchParams, setSearchParams] = useSearchParams()

  // ===== 状态 =====
  const [sortColumn, setSortColumn] = useState<number | null>(null)
  const [sortDirection, setSortDirection] = useState<SortDirection>(null)
  const [filters, setFilters] = useState<Record<number, DataTableFilter>>({})
  const [globalQuery, setGlobalQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(initialPageSize)
  const [hiddenColumns, setHiddenColumns] = useState<Set<number>>(new Set())
  const [filterPopover, setFilterPopover] = useState<{
    columnIndex: number
    anchor: HTMLElement
  } | null>(null)
  const [columnManagerAnchor, setColumnManagerAnchor] = useState<HTMLElement | null>(null)

  // ===== URL 状态同步 =====
  // 从 URL 恢复状态
  useEffect(() => {
    const urlSort = searchParams.get('sort')
    const urlDir = searchParams.get('dir')
    const urlPage = searchParams.get('page')
    const urlPageSize = searchParams.get('page_size')
    if (urlSort && urlDir) {
      const idx = columns.findIndex((c) => c.key === urlSort)
      if (idx >= 0) {
        setSortColumn(idx)
        setSortDirection(urlDir as 'asc' | 'desc')
      }
    }
    if (urlPage) setPage(parseInt(urlPage, 10))
    if (urlPageSize) setPageSize(parseInt(urlPageSize, 10))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ===== sessionStorage 恢复 =====
  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(`table-columns:${tableId}`)
      if (saved) {
        setHiddenColumns(new Set(JSON.parse(saved)))
      }
    } catch {
      // ignore
    }
  }, [tableId])

  // 保存列设置到 localStorage
  const saveColumns = useCallback(
    (hidden: Set<number>) => {
      try {
        localStorage.setItem(`table-columns:${tableId}`, JSON.stringify([...hidden]))
      } catch {
        // ignore
      }
    },
    [tableId],
  )

  // ===== 列可见性 =====
  const applyColumnVisibility = useCallback(
    (hidden: Set<number>) => {
      setHiddenColumns(hidden)
      saveColumns(hidden)
    },
    [saveColumns],
  )

  // ===== 排序切换（三态：无 → 升序 → 降序 → 无）=====
  const toggleSort = useCallback(
    (index: number) => {
      setSortColumn((prev) => {
        if (prev !== index) {
          setSortDirection('asc')
          return index
        }
        setSortDirection((prevDir) => {
          if (prevDir === 'asc') return 'desc'
          if (prevDir === 'desc') return null
          return 'asc'
        })
        return prev
      })
      setPage(1)
    },
    [],
  )

  // ===== 筛选 =====
  const applyFilter = useCallback((index: number, filter: DataTableFilter) => {
    setFilters((prev) => ({ ...prev, [index]: filter }))
    setPage(1)
    setFilterPopover(null)
  }, [])

  const clearFilter = useCallback((index: number) => {
    setFilters((prev) => {
      const next = { ...prev }
      delete next[index]
      return next
    })
    setPage(1)
    setFilterPopover(null)
  }, [])

  const reset = useCallback(() => {
    setFilters({})
    setSortColumn(null)
    setSortDirection(null)
    setGlobalQuery('')
    setPage(1)
  }, [])

  // ===== URL 同步 =====
  useEffect(() => {
    const params = new URLSearchParams(searchParams)
    if (sortColumn !== null && sortDirection) {
      params.set('sort', columns[sortColumn]?.key || '')
      params.set('dir', sortDirection)
    } else {
      params.delete('sort')
      params.delete('dir')
    }
    if (page > 1) params.set('page', String(page))
    else params.delete('page')
    // [StrategyDataTable] - 描述: URL 同步基准值为 initialPageSize（非默认值时写入 URL）
    if (pageSize !== initialPageSize) params.set('page_size', String(pageSize))
    else params.delete('page_size')
    setSearchParams(params, { replace: true })
  }, [sortColumn, sortDirection, page, pageSize, columns, searchParams, setSearchParams, initialPageSize])

  // ===== 服务端查询回调 =====
  useEffect(() => {
    if (serverSide && onQueryChange) {
      onQueryChange({
        page,
        pageSize,
        // [StrategyDataTable] - 描述: 透传全文搜索关键词至服务端
        keyword: globalQuery.trim() || undefined,
        sort:
          sortColumn !== null && sortDirection
            ? { key: columns[sortColumn]?.key || '', direction: sortDirection }
            : undefined,
        filters: Object.values(filters),
      })
    }
  }, [page, pageSize, sortColumn, sortDirection, filters, serverSide, onQueryChange, columns, globalQuery])

  // ===== 客户端排序和筛选 =====
  const processedRows = useMemo(() => {
    if (serverSide) return rows

    let visible = rows.filter((row) => {
      // 全文搜索
      if (globalQuery) {
        const rowText = JSON.stringify(row).toLocaleLowerCase('zh-CN')
        if (!rowText.includes(globalQuery.toLocaleLowerCase('zh-CN'))) return false
      }
      // 列筛选
      return Object.entries(filters).every(([idx, filter]) => {
        const col = columns[+idx]
        if (!col) return true
        const text = col.filterValue ? col.filterValue(row) : String(row[col.key] ?? '')
        return matchFilter(text, filter)
      })
    })

    // 排序
    if (sortColumn !== null && sortDirection) {
      const col = columns[sortColumn]
      if (col) {
        const dir = sortDirection === 'asc' ? 1 : -1
        visible = [...visible].sort((ra, rb) => {
          const aVal = col.sortValue ? col.sortValue(ra) : String(ra[col.key] ?? '')
          const bVal = col.sortValue ? col.sortValue(rb) : String(rb[col.key] ?? '')
          const a = parseComparable(String(aVal))
          const b = parseComparable(String(bVal))
          if (a.type === b.type) {
            return ((a.value as number) > (b.value as number) ? 1 : (a.value as number) < (b.value as number) ? -1 : 0) * dir
          }
          return String(a.value).localeCompare(String(b.value), 'zh-CN') * dir
        })
      }
    }

    return visible
  }, [rows, globalQuery, filters, sortColumn, sortDirection, columns, serverSide])

  // 分页
  // serverSide 模式：total 来自 API；客户端模式：total 优先取 prop，否则用 processedRows.length
  const totalCount = serverSide
    ? (total ?? 0)
    : (total ?? processedRows.length)
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize))
  const currentPage = Math.min(page, totalPages)
  const pageRows = serverSide
    ? processedRows
    : processedRows.slice((currentPage - 1) * pageSize, currentPage * pageSize)

  // [StrategyDataTable] - 描述: 分页大小选项保留 10/20/50，若 initialPageSize 不在其中则补一项
  const pageSizeOptions = useMemo(() => {
    const base = [10, 20, 50]
    if (!base.includes(initialPageSize)) base.push(initialPageSize)
    return base.sort((a, b) => a - b)
  }, [initialPageSize])

  // ===== 全选逻辑 =====
  const allChecked = selectable && pageRows.length > 0 && pageRows.every((r) => selectedKeys?.has(rowKey(r)))
  const someChecked = selectable && pageRows.some((r) => selectedKeys?.has(rowKey(r)))

  const handleSelectAll = () => {
    if (!onSelectionChange || !selectedKeys) return
    const next = new Set(selectedKeys)
    if (allChecked) {
      pageRows.forEach((r) => next.delete(rowKey(r)))
    } else {
      pageRows.forEach((r) => next.add(rowKey(r)))
    }
    onSelectionChange(next)
  }

  const handleSelectRow = (row: Row) => {
    if (!onSelectionChange || !selectedKeys) return
    const key = rowKey(row)
    const next = new Set(selectedKeys)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    onSelectionChange(next)
  }

  // ===== 渲染 =====
  const filterCount = Object.keys(filters).length
  const hasActiveState = filterCount > 0 || sortColumn !== null || globalQuery !== ''

  // 找到第一个可排序的列作为 sticky-col
  let stickyAssigned = false

  return (
    <div className="table-wrap">
      {/* 元信息栏 */}
      <div className="table-meta-bar">
        <div>
          <span className="table-result-count">
            结果 {totalCount}{total != null && !serverSide ? ` / 服务端 ${total}` : ''}
          </span>
          <span className="table-active-state">
            {[
              globalQuery ? '全文搜索' : null,
              filterCount ? `${filterCount} 个列筛选` : null,
              sortColumn !== null
                ? `按「${columns[sortColumn]?.title}」${sortDirection === 'asc' ? '升序' : '降序'}`
                : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
          {stale && <span className="tag warn" style={{ marginLeft: 8 }}>数据过期</span>}
        </div>
        <div className="table-meta-actions">
          <button
            className="table-columns-btn"
            onClick={(e) => setColumnManagerAnchor(e.currentTarget)}
          >
            列设置
          </button>
          <button
            className="table-reset-btn"
            disabled={!hasActiveState}
            onClick={reset}
          >
            清除排序与筛选
          </button>
        </div>
      </div>

      {/* 全文搜索 */}
      {searchable && (
        <div style={{ padding: '8px 10px', borderBottom: '1px solid var(--border-soft)' }}>
          <div className="field search" style={{ display: 'inline-block' }}>
            <input
              className="input search"
              style={{ width: 260 }}
              placeholder="全文搜索"
              value={globalQuery}
              onChange={(e) => {
                setGlobalQuery(e.target.value.trim().toLocaleLowerCase('zh-CN'))
                setPage(1)
              }}
            />
          </div>
        </div>
      )}

      {/* 表格 */}
      <table className="data-table interactive-table">
        <thead>
          <tr>
            {selectable && (
              <th className="table-select-column">
                <label className="table-checkbox-wrapper">
                  <input
                    type="checkbox"
                    className="table-checkbox"
                    checked={allChecked}
                    ref={(el) => {
                      if (el) el.indeterminate = !allChecked && someChecked
                    }}
                    onChange={handleSelectAll}
                  />
                </label>
              </th>
            )}
            {columns.map((col, i) => {
              const isHidden = hiddenColumns.has(i)
              if (isHidden) return null

              // V1.5.1：操作列和选择列不参与排序与筛选
              if (col.isAction) {
                return (
                  <th key={col.key} className="table-action-column">
                    {col.title}
                  </th>
                )
              }

              // 第一个非操作列设为 sticky-col
              const isSticky = !stickyAssigned
              if (isSticky) stickyAssigned = true

              return (
                <th
                  key={col.key}
                  className={clsx(
                    sortColumn === i && 'sorted',
                    isSticky && 'sticky-col',
                  )}
                >
                  <div className="th-shell">
                    {col.sortable && (
                      <button
                        className="th-sort"
                        title={`按${col.title}排序`}
                        onClick={() => toggleSort(i)}
                      >
                        <span className="th-label">{col.title}</span>
                        <span className="sort-icon">
                          {sortColumn === i
                            ? sortDirection === 'asc'
                              ? '↑'
                              : sortDirection === 'desc'
                                ? '↓'
                                : '↕'
                            : '↕'}
                        </span>
                      </button>
                    )}
                    {!col.sortable && <span className="th-label">{col.title}</span>}
                    {col.helpText && (
                      <span className="th-help" title={col.helpText}>
                        ?
                        <span className="th-help-tooltip">{col.helpText}</span>
                      </span>
                    )}
                    {col.filterable && (
                      <button
                        className={clsx('th-filter', filters[i] && 'active')}
                        aria-label={`筛选${col.title}`}
                        title={`筛选${col.title}`}
                        onClick={(e) =>
                          setFilterPopover({ columnIndex: i, anchor: e.currentTarget })
                        }
                      >
                        ⌁
                      </button>
                    )}
                  </div>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr className="table-empty-row">
              <td colSpan={columns.length + (selectable ? 1 : 0)}>
                <div className="table-empty-state">
                  <b>加载中…</b>
                  <span>正在获取数据</span>
                </div>
              </td>
            </tr>
          )}
          {!loading && error && (
            <tr className="table-empty-row">
              <td colSpan={columns.length + (selectable ? 1 : 0)}>
                <div className="table-empty-state">
                  <b>加载失败</b>
                  <span>{error}</span>
                </div>
              </td>
            </tr>
          )}
          {!loading && !error && pageRows.length === 0 && (
            <tr className="table-empty-row">
              <td colSpan={columns.length + (selectable ? 1 : 0)}>
                <div className="table-empty-state">
                  <b>{emptyText}</b>
                  <span>可清除列筛选或调整条件后重试</span>
                </div>
              </td>
            </tr>
          )}
          {!loading &&
            !error &&
            pageRows.map((row) => {
              const key = rowKey(row)
              const isSelected = selectedKeys?.has(key)
              stickyAssigned = false // 重置用于行内 sticky-col
              return (
                <tr key={key}>
                  {selectable && (
                    <td className="table-select-column">
                      <label className="table-checkbox-wrapper">
                        <input
                          type="checkbox"
                          className="table-checkbox"
                          checked={isSelected || false}
                          onChange={() => handleSelectRow(row)}
                        />
                      </label>
                    </td>
                  )}
                  {columns.map((col, i) => {
                    if (hiddenColumns.has(i)) return null
                    const isSticky = !stickyAssigned && !col.isAction
                    if (isSticky) stickyAssigned = true
                    return (
                      <td
                        key={col.key}
                        className={clsx(
                          col.dataType === 'number' ||
                            col.dataType === 'percent' ||
                            col.dataType === 'datetime'
                            ? 'num'
                            : '',
                          isSticky && 'sticky-col',
                        )}
                      >
                        {col.render ? col.render(row) : String(row[col.key] ?? '')}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
        </tbody>
      </table>

      {/* 分页 */}
      <div className="table-pager">
        <span className="table-page-info">
          第 {currentPage} / {totalPages} 页
        </span>
        <label>
          每页{' '}
          <select
            className="select table-page-size"
            value={pageSize}
            onChange={(e) => {
              setPageSize(parseInt(e.target.value, 10))
              setPage(1)
            }}
          >
            {pageSizeOptions.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
        <button
          className="btn small table-prev"
          disabled={currentPage <= 1}
          onClick={() => setPage(currentPage - 1)}
        >
          上一页
        </button>
        <button
          className="btn small table-next"
          disabled={currentPage >= totalPages}
          onClick={() => setPage(currentPage + 1)}
        >
          下一页
        </button>
      </div>

      {/* 筛选弹窗 */}
      {filterPopover && (
        <FilterPopover
          column={columns[filterPopover.columnIndex] as DataTableColumn<unknown>}
          current={filters[filterPopover.columnIndex]}
          anchor={filterPopover.anchor}
          onApply={(filter) => applyFilter(filterPopover.columnIndex, filter)}
          onClear={() => clearFilter(filterPopover.columnIndex)}
          onClose={() => setFilterPopover(null)}
        />
      )}

      {/* 列设置弹窗 */}
      {columnManagerAnchor && (
        <ColumnManager
          columns={columns as DataTableColumn<unknown>[]}
          hiddenColumns={hiddenColumns}
          onToggle={(idx) => {
            const next = new Set(hiddenColumns)
            if (next.has(idx)) next.delete(idx)
            else next.add(idx)
            applyColumnVisibility(next)
          }}
          onReset={() => {
            applyColumnVisibility(new Set())
            setColumnManagerAnchor(null)
          }}
          onClose={() => setColumnManagerAnchor(null)}
          anchor={columnManagerAnchor}
        />
      )}
    </div>
  )
}
