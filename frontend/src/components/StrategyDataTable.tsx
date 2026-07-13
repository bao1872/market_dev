// StrategyDataTable：通用数据表组件（V1.5.1）
// 对应原型 app.js InteractiveTable 类
// 必需能力：三态排序、逐列筛选、服务端分页、固定表头首列、列设置、空态/错误态/过期态
// 所有用户端和管理员端数据表必须使用同一表格组件
import { useState, useMemo, useCallback, useEffect, useRef, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import { TablePresetMenu } from './TablePresetMenu'
import { useTableViewPresets } from '@/hooks/useApi'
import { decodeScreenerUrlState, encodeScreenerUrlState } from './screenerUrlState'
import { reorderVisibleColumns } from './columnOrdering'
import type { TableViewPresetConfig } from '@/api/endpoints'

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
  // [StrategyDataTable] - 描述: 表头缩写（显示用），title 保留完整描述用于 tooltip；缺省时回退到 title
  shortTitle?: string
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
  // 当前激活的运行 ID；切换时自动重置分页到第 1 页
  activeRunId?: string
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
  // [StrategyDataTable] - 描述: 附加到 <table> 的 className（用于紧凑布局等场景）
  tableClassName?: string
  // [Presets] - 描述: 策略 key（提供时启用视图配置保存/应用功能）
  strategyKey?: string | null
  // [StickyHeader] - 描述: 表头 sticky 模式
  // - container: 在 .table-wrap 局部滚动容器内吸附（默认，兼容历史行为）
  // - viewport: 在页面滚动时吸附到 topbar 下方（趋势选股页使用）
  stickyHeaderMode?: 'viewport' | 'container'
  // [StrategyDataTable] - 描述: 行点击回调（非链接区域点击时触发，用于 /market 选中行驱动右栏）
  onRowClick?: (row: Row) => void
  // [StrategyDataTable] - 描述: 当前选中行 key（用于高亮选中行）
  activeRowKey?: string | null
  // [StrategyDataTable] - 描述: 外部受控 keyword（提供时覆盖内部 globalQuery，用于 /market 顶部搜索框）
  externalKeyword?: string
  onKeywordChange?: (keyword: string) => void
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

// 列设置弹窗（支持显示/隐藏 + 上下调整顺序）
function ColumnManager({
  columns,
  hiddenColumns,
  onToggle,
  onReset,
  onClose,
  onMoveUp,
  onMoveDown,
  anchor,
}: {
  columns: DataTableColumn<unknown>[]
  hiddenColumns: Set<string>
  onToggle: (key: string) => void
  onReset: () => void
  onClose: () => void
  onMoveUp: (key: string) => void
  onMoveDown: (key: string) => void
  anchor: HTMLElement
}) {
  const rect = anchor.getBoundingClientRect()
  const left = Math.min(window.innerWidth - 300, Math.max(8, rect.left - 100))
  const top = Math.min(window.innerHeight - 380, rect.bottom + 6)

  const manageable = columns.filter((c) => !c.isAction && !c.isSelect)

  return (
    <div className="column-filter-popover column-manager-popover" style={{ left, top }}>
      <div className="filter-pop-title">显示列（可拖动调整顺序）</div>
      <div className="column-manager-list">
        {manageable.map((col, idx) => (
          <div key={col.key} className="column-manager-item">
            <label className="table-checkbox-wrapper" style={{ width: 24, height: 24 }}>
              <input
                type="checkbox"
                className="table-checkbox"
                checked={!hiddenColumns.has(col.key)}
                onChange={() => onToggle(col.key)}
              />
            </label>
            <span className="column-manager-label">{col.title}</span>
            <span className="column-manager-reorder">
              <button
                className="btn small columns-move-up"
                disabled={idx === 0}
                onClick={() => onMoveUp(col.key)}
                aria-label="上移"
                title="上移"
              >
                ↑
              </button>
              <button
                className="btn small columns-move-down"
                disabled={idx === manageable.length - 1}
                onClick={() => onMoveDown(col.key)}
                aria-label="下移"
                title="下移"
              >
                ↓
              </button>
            </span>
          </div>
        ))}
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
    activeRunId,
    searchable = true,
    selectable = false,
    selectedKeys,
    onSelectionChange,
    rowKey,
    emptyText = '没有符合筛选条件的数据',
    initialPageSize = 10,
    tableClassName,
    strategyKey,
    stickyHeaderMode = 'container',
    onRowClick,
    activeRowKey,
    externalKeyword,
    onKeywordChange,
  } = props

  const [searchParams, setSearchParams] = useSearchParams()

  // ===== 状态 =====
  const [sortColumn, setSortColumn] = useState<number | null>(null)
  const [sortDirection, setSortDirection] = useState<SortDirection>(null)
  const [filters, setFilters] = useState<Record<number, DataTableFilter>>({})
  const [globalQuery, setGlobalQuery] = useState('')

  // [StrategyDataTable] - 描述: 受控 keyword 模式 — externalKeyword 提供时覆盖内部 globalQuery
  const effectiveKeyword = externalKeyword !== undefined ? externalKeyword : globalQuery
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(initialPageSize)
  const [hiddenColumns, setHiddenColumns] = useState<Set<string>>(new Set())
  const [columnOrder, setColumnOrder] = useState<string[] | null>(null)
  const [filterPopover, setFilterPopover] = useState<{
    columnIndex: number
    anchor: HTMLElement
  } | null>(null)
  const [columnManagerAnchor, setColumnManagerAnchor] = useState<HTMLElement | null>(null)

  // [StrategyDataTable] - 描述: 切换运行批次时重置分页到第 1 页
  useEffect(() => {
    setPage(1)
  }, [activeRunId])

  // ===== URL 状态同步 =====
  const urlHydratedRef = useRef(false)
  const urlHadStateRef = useRef(false)
  const skipNextUrlSyncRef = useRef(false)

  // 从 URL 恢复状态（mount 时执行一次）；丢弃当前 columns 中不存在的陈旧 key
  useEffect(() => {
    // [StrategyDataTable] - 描述: 跳过 mount 后同一轮 render 的 URL sync，避免默认 state 覆盖 URL
    skipNextUrlSyncRef.current = true
    const validKeys = new Set(columns.map((c) => c.key))
    const state = decodeScreenerUrlState(searchParams, validKeys, {
      defaultPageSize: initialPageSize,
    })
    if (
      state.keyword ||
      (state.filters && state.filters.length > 0) ||
      state.sort ||
      (state.page !== undefined && state.page !== 1) ||
      (state.pageSize !== undefined && state.pageSize !== initialPageSize)
    ) {
      urlHadStateRef.current = true
    }
    if (state.sort) {
      const idx = columns.findIndex((c) => c.key === state.sort!.key)
      if (idx >= 0) {
        setSortColumn(idx)
        setSortDirection(state.sort.direction)
      }
    }
    if (state.keyword) {
      setGlobalQuery(state.keyword)
      if (onKeywordChange) onKeywordChange(state.keyword)
    }
    if (state.filters && state.filters.length > 0) {
      const next: Record<number, DataTableFilter> = {}
      for (const f of state.filters) {
        const idx = columns.findIndex((c) => c.key === f.key)
        if (idx < 0) continue
        const ops = operatorsForDataType(columns[idx].dataType)
        if (!ops.includes(f.op as FilterOperator)) continue
        next[idx] = {
          key: f.key,
          operator: f.op as FilterOperator,
        }
        if (f.value !== undefined) next[idx].value = f.value as string | number
        if (f.value2 !== undefined) next[idx].value2 = f.value2 as string | number
      }
      if (Object.keys(next).length > 0) setFilters(next)
    }
    if (state.page !== undefined) setPage(state.page)
    if (state.pageSize !== undefined) setPageSize(state.pageSize)
    urlHydratedRef.current = true
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ===== localStorage 恢复（按 column key 持久化，列顺序变化后不会错列）=====
  useEffect(() => {
    try {
      const saved = localStorage.getItem(`table-columns:${tableId}`)
      if (saved) {
        const validKeys = new Set(columns.map((c) => c.key))
        const parsed: unknown = JSON.parse(saved)
        if (Array.isArray(parsed)) {
          // [StrategyDataTable] - 描述: 仅保留当前列中仍存在的 key，丢弃陈旧 key
          const next = new Set<string>(
            parsed.filter((k): k is string => typeof k === 'string' && validKeys.has(k)),
          )
          setHiddenColumns(next)
        }
      }
    } catch {
      // ignore
    }
    // [StrategyDataTable] - 描述: 恢复列顺序（columnOrder）
    try {
      const savedOrder = localStorage.getItem(`table-column-order:${tableId}`)
      if (savedOrder) {
        const validKeys = new Set(columns.map((c) => c.key))
        const parsedOrder: unknown = JSON.parse(savedOrder)
        if (Array.isArray(parsedOrder)) {
          const next = parsedOrder.filter(
            (k): k is string => typeof k === 'string' && validKeys.has(k),
          )
          setColumnOrder(next.length > 0 ? next : null)
        }
      }
    } catch {
      // ignore
    }
  }, [tableId, columns])

  // 保存列设置到 localStorage（按 column key）
  const saveColumns = useCallback(
    (hidden: Set<string>) => {
      try {
        localStorage.setItem(`table-columns:${tableId}`, JSON.stringify([...hidden]))
      } catch {
        // ignore
      }
    },
    [tableId],
  )

  // [StrategyDataTable] - 描述: 保存列顺序到 localStorage
  const saveColumnOrder = useCallback(
    (order: string[] | null) => {
      try {
        if (order && order.length > 0) {
          localStorage.setItem(`table-column-order:${tableId}`, JSON.stringify(order))
        } else {
          localStorage.removeItem(`table-column-order:${tableId}`)
        }
      } catch {
        // ignore
      }
    },
    [tableId],
  )

  // ===== 列可见性 =====
  const applyColumnVisibility = useCallback(
    (hidden: Set<string>) => {
      setHiddenColumns(hidden)
      saveColumns(hidden)
    },
    [saveColumns],
  )

  // [StrategyDataTable] - 描述: 可见列派生（携带 originalIndex，保留 columns 原始索引用于排序/筛选 state 定位）
  // 说明：sortColumn / filters / filterPopover.columnIndex 均基于 columns 原始索引，故 visibleColumns 必须保留该映射
  // columnOrder 非空时按其顺序排列列（仅管理列，action/select 列固定在末尾）；否则按 columns 原始顺序
  // 逻辑提取到 columnOrdering.ts 的 reorderVisibleColumns 纯函数，便于 P0 列对齐测试
  const visibleColumns = useMemo(
    () => reorderVisibleColumns(columns, hiddenColumns, columnOrder),
    [columns, hiddenColumns, columnOrder],
  )

  // [StrategyDataTable] - 描述: 可见列宽度之和（用于 table min-width，避免隐藏列后表格被压缩）
  const visibleColumnsWidthSum = useMemo(
    () => visibleColumns.reduce((sum, { col }) => sum + (col.width ?? 80), 0),
    [visibleColumns],
  )

  // ===== 排序切换（三态：无 → 降序 → 升序 → 无，默认最新/最大在前）=====
  const toggleSort = useCallback(
    (index: number) => {
      setSortColumn((prev) => {
        if (prev !== index) {
          setSortDirection('desc')
          return index
        }
        setSortDirection((prevDir) => {
          if (prevDir === 'desc') return 'asc'
          if (prevDir === 'asc') return null
          return 'desc'
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

  // ===== 视图配置 Preset =====
  // [Presets] - 描述: 从内部 state 构建当前配置快照（keyword/sort/filters/hiddenColumns/columnOrder/pageSize）
  const currentConfig: TableViewPresetConfig = useMemo(() => ({
    keyword: effectiveKeyword.trim() || null,
    sort: sortColumn !== null && sortDirection
      ? { key: columns[sortColumn]?.key ?? '', direction: sortDirection }
      : null,
    filters: Object.values(filters).map((f) => ({
      key: f.key,
      op: f.operator,
      value: f.value ?? '',
      ...(f.value2 !== undefined ? { value2: f.value2 } : {}),
    })),
    hiddenColumns: [...hiddenColumns],
    columnOrder: columnOrder ?? null,
    pageSize,
  }), [effectiveKeyword, sortColumn, sortDirection, filters, hiddenColumns, columnOrder, pageSize, columns])

  // [Presets] - 描述: 应用 preset 配置到内部 state（重置所有筛选/排序/分页/隐藏列/列顺序）
  const applyPresetConfig = useCallback((config: TableViewPresetConfig) => {
    setGlobalQuery(config.keyword ?? '')
    if (onKeywordChange) onKeywordChange(config.keyword ?? '')
    if (config.sort) {
      const idx = columns.findIndex((c) => c.key === config.sort!.key)
      setSortColumn(idx >= 0 ? idx : null)
      setSortDirection(config.sort.direction)
    } else {
      setSortColumn(null)
      setSortDirection(null)
    }
    if (config.filters) {
      const next: Record<number, DataTableFilter> = {}
      for (const f of config.filters) {
        const idx = columns.findIndex((c) => c.key === f.key)
        if (idx >= 0) {
          next[idx] = {
            key: f.key,
            operator: f.op as FilterOperator,
            value: f.value,
            ...(f.value2 !== undefined ? { value2: f.value2 } : {}),
          }
        }
      }
      setFilters(next)
    } else {
      setFilters({})
    }
    if (config.hiddenColumns) {
      setHiddenColumns(new Set(config.hiddenColumns))
    } else {
      setHiddenColumns(new Set())
    }
    // [StrategyDataTable] - 描述: 应用列顺序（columnOrder）
    if (config.columnOrder && config.columnOrder.length > 0) {
      setColumnOrder(config.columnOrder)
      saveColumnOrder(config.columnOrder)
    } else {
      setColumnOrder(null)
      saveColumnOrder(null)
    }
    if (config.pageSize != null) setPageSize(config.pageSize)
    setPage(1)
  }, [columns, saveColumnOrder, onKeywordChange])

  // [Presets] - 描述: 自动应用默认配置（进入页面时，每个 strategyKey 只应用一次）
  const presetsQuery = useTableViewPresets(strategyKey ? tableId : undefined, strategyKey ?? undefined)
  const defaultAppliedRef = useRef<string>('')
  useEffect(() => {
    if (!strategyKey || !presetsQuery.data) return
    const appliedKey = `${tableId}:${strategyKey}`
    if (defaultAppliedRef.current === appliedKey) return
    // [StrategyDataTable] - 描述: URL 中已有排序/筛选/关键词/页码时，不覆盖为默认 preset
    if (urlHadStateRef.current) {
      defaultAppliedRef.current = appliedKey
      return
    }
    const defaultPreset = presetsQuery.data.items.find((p) => p.is_default)
    if (defaultPreset) {
      const cfg = defaultPreset.config as Record<string, unknown>
      applyPresetConfig({
        keyword: (cfg.keyword as string | null | undefined) ?? null,
        sort: (cfg.sort as TableViewPresetConfig['sort']) ?? null,
        filters: (cfg.filters as TableViewPresetConfig['filters']) ?? null,
        hiddenColumns: (cfg.hiddenColumns as string[] | null | undefined) ?? null,
        columnOrder: (cfg.columnOrder as string[] | null | undefined) ?? null,
        pageSize: (cfg.pageSize as number | null | undefined) ?? null,
      })
    }
    defaultAppliedRef.current = appliedKey
  }, [strategyKey, tableId, presetsQuery.data, applyPresetConfig])

  // ===== URL 同步 =====
  useEffect(() => {
    if (!urlHydratedRef.current) return
    if (skipNextUrlSyncRef.current) {
      skipNextUrlSyncRef.current = false
      return
    }
    const state = {
      keyword: effectiveKeyword.trim() || undefined,
      sort:
        sortColumn !== null && sortDirection
          ? { key: columns[sortColumn]?.key || '', direction: sortDirection }
          : undefined,
      filters: Object.values(filters).map((f) => ({
        key: f.key,
        op: f.operator,
        ...(f.value !== undefined ? { value: f.value } : {}),
        ...(f.value2 !== undefined ? { value2: f.value2 } : {}),
      })),
      page,
      pageSize,
    }
    const encoded = encodeScreenerUrlState(state, { defaultPageSize: initialPageSize })
    const nextParams = new URLSearchParams(searchParams)
    const managedKeys = ['sort', 'dir', 'keyword', 'filters', 'page', 'page_size']
    for (const key of managedKeys) {
      if (encoded.has(key)) {
        nextParams.set(key, encoded.get(key)!)
      } else {
        nextParams.delete(key)
      }
    }
    setSearchParams(nextParams, { replace: true })
  }, [sortColumn, sortDirection, page, pageSize, columns, searchParams, setSearchParams, initialPageSize, effectiveKeyword, filters])

  // ===== 服务端查询回调 =====
  useEffect(() => {
    if (serverSide && onQueryChange) {
      onQueryChange({
        page,
        pageSize,
        // [StrategyDataTable] - 描述: 透传全文搜索关键词至服务端
        keyword: effectiveKeyword.trim() || undefined,
        sort:
          sortColumn !== null && sortDirection
            ? { key: columns[sortColumn]?.key || '', direction: sortDirection }
            : undefined,
        filters: Object.values(filters),
      })
    }
  }, [page, pageSize, sortColumn, sortDirection, filters, serverSide, onQueryChange, columns, effectiveKeyword])

  // ===== 客户端排序和筛选 =====
  const processedRows = useMemo(() => {
    if (serverSide) return rows

    let visible = rows.filter((row) => {
      // 全文搜索
      if (effectiveKeyword) {
        const rowText = JSON.stringify(row).toLocaleLowerCase('zh-CN')
        if (!rowText.includes(effectiveKeyword.toLocaleLowerCase('zh-CN'))) return false
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
  }, [rows, effectiveKeyword, filters, sortColumn, sortDirection, columns, serverSide])

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
  const hasActiveState = filterCount > 0 || sortColumn !== null || effectiveKeyword !== ''

  // 找到第一个可排序的列作为 sticky-col
  let stickyAssigned = false

  return (
    <div className={clsx('table-wrap', stickyHeaderMode === 'viewport' && 'viewport-sticky')}>
      {/* 元信息栏 */}
      <div className="table-meta-bar">
        <div>
          <span className="table-result-count">
            结果 {totalCount}{total != null && !serverSide ? ` / 服务端 ${total}` : ''}
          </span>
          <span className="table-active-state">
            {[
              effectiveKeyword ? '全文搜索' : null,
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
          {strategyKey && (
            <TablePresetMenu
              tableId={tableId}
              strategyKey={strategyKey}
              currentConfig={currentConfig}
              onApply={applyPresetConfig}
            />
          )}
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
              value={effectiveKeyword}
              onChange={(e) => {
                const v = e.target.value.trim().toLocaleLowerCase('zh-CN')
                setGlobalQuery(v)
                if (onKeywordChange) onKeywordChange(v)
                setPage(1)
              }}
            />
          </div>
        </div>
      )}

      {/* 表格 */}
      <table
        className={clsx('data-table interactive-table', tableClassName)}
        style={{ minWidth: `${visibleColumnsWidthSum + (selectable ? 40 : 0)}px` }}
      >
        <colgroup>
          {selectable && <col />}
          {visibleColumns.map(({ col }) => (
            <col
              key={col.key}
              style={col.width !== undefined ? { width: `${col.width}px` } : undefined}
            />
          ))}
        </colgroup>
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
            {visibleColumns.map(({ col, originalIndex: i }) => {
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
                        <span className="th-label" title={col.shortTitle ? col.title : undefined}>
                          {col.shortTitle ?? col.title}
                        </span>
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
                    {!col.sortable && (
                      <span className="th-label" title={col.shortTitle ? col.title : undefined}>
                        {col.shortTitle ?? col.title}
                      </span>
                    )}
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
              <td colSpan={visibleColumns.length + (selectable ? 1 : 0)}>
                <div className="table-empty-state">
                  <b>加载中…</b>
                  <span>正在获取数据</span>
                </div>
              </td>
            </tr>
          )}
          {!loading && error && (
            <tr className="table-empty-row">
              <td colSpan={visibleColumns.length + (selectable ? 1 : 0)}>
                <div className="table-empty-state">
                  <b>加载失败</b>
                  <span>{error}</span>
                </div>
              </td>
            </tr>
          )}
          {!loading && !error && pageRows.length === 0 && (
            <tr className="table-empty-row">
              <td colSpan={visibleColumns.length + (selectable ? 1 : 0)}>
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
                <tr
                  key={key}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={clsx(activeRowKey === key && 'row-active')}
                >
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
                  {visibleColumns.map(({ col }) => {
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
          onToggle={(key) => {
            const next = new Set(hiddenColumns)
            if (next.has(key)) next.delete(key)
            else next.add(key)
            applyColumnVisibility(next)
          }}
          onMoveUp={(key) => {
            // [StrategyDataTable] - 描述: 上移列 — 在当前序列中交换 key 与前一项
            const manageableKeys = columns
              .filter((c) => !c.isAction && !c.isSelect)
              .map((c) => c.key)
            const currentOrder = columnOrder ?? manageableKeys
            const idx = currentOrder.indexOf(key)
            if (idx <= 0) return
            const next = [...currentOrder]
            ;[next[idx - 1], next[idx]] = [next[idx], next[idx - 1]]
            setColumnOrder(next)
            saveColumnOrder(next)
          }}
          onMoveDown={(key) => {
            // [StrategyDataTable] - 描述: 下移列 — 在当前序列中交换 key 与后一项
            const manageableKeys = columns
              .filter((c) => !c.isAction && !c.isSelect)
              .map((c) => c.key)
            const currentOrder = columnOrder ?? manageableKeys
            const idx = currentOrder.indexOf(key)
            if (idx < 0 || idx >= currentOrder.length - 1) return
            const next = [...currentOrder]
            ;[next[idx + 1], next[idx]] = [next[idx], next[idx + 1]]
            setColumnOrder(next)
            saveColumnOrder(next)
          }}
          onReset={() => {
            applyColumnVisibility(new Set())
            setColumnOrder(null)
            saveColumnOrder(null)
            setColumnManagerAnchor(null)
          }}
          onClose={() => setColumnManagerAnchor(null)}
          anchor={columnManagerAnchor}
        />
      )}
    </div>
  )
}
