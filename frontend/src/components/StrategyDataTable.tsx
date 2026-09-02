// StrategyDataTable：通用数据表组件（V1.5.1）
// 对应原型 app.js InteractiveTable 类
// 必需能力：三态排序、逐列筛选、服务端分页、固定表头首列、列设置、空态/错误态/过期态
// 所有用户端和管理员端数据表必须使用同一表格组件
import { useState, useMemo, useCallback, useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import { TablePresetMenu } from './TablePresetMenu'
import { useTableViewPresets } from '@/hooks/useApi'
import { decodeScreenerUrlState, encodeScreenerUrlState } from './screenerUrlState'
import { reorderVisibleColumns } from './columnOrdering'
import { canonicalizeFilterOperator } from './filterOperators'
import type { TableViewPresetConfig } from '@/api/endpoints'

// ===== 类型定义（对应 UI_DEVELOPMENT_SPEC.md 3.3 推荐组件输入）=====
export type DataType = 'text' | 'number' | 'percent' | 'datetime' | 'enum' | 'range'
export type SortDirection = 'asc' | 'desc' | null
// [CHANGE-20260730-013] 扩展操作符类型，覆盖后端 SSOT 全部操作符合同：
// text: contains/not_contains/eq/neq/empty/not_empty
// enum: eq/neq/in/not_in/empty/not_empty
// boolean: eq/empty/not_empty
// number/percent: eq/neq/gt/gte/lt/lte/between/empty/not_empty
// datetime: date_eq/before/after/between/empty/not_empty
export type FilterOperator =
  | 'contains' | 'not_contains' | 'eq' | 'neq'
  | 'gt' | 'gte' | 'lt' | 'lte' | 'between'
  | 'in' | 'not_in'
  | 'has_any' | 'has_all' | 'not_has_any'
  | 'date_eq' | 'before' | 'after'
  | 'empty' | 'not_empty'

// [CHANGE-20260730-013] 列级筛选元数据（来自 /market/filter-specs API）
// 当 filterSpec 存在时，FilterPopover 优先使用其 operators/enum_values/input_control
// 而非 dataType 默认值；用于按 data_type 动态生成类型化控件（enum 下拉、日期选择器等）
export interface ColumnFilterSpec {
  /** 后端 SSOT data_type（text/enum/boolean/number/percent/datetime/multi_enum） */
  data_type: string
  /** 允许的操作符列表（来自 FP_QUERY_FIELD_SPECS.<key>.operators） */
  operators: string[]
  /** 枚举值列表（enum/multi_enum 类型字段非空） */
  enum_values: string[]
  /** 输入控件类型（text_input/single_select/multi_select/number_input/date_picker/boolean_toggle） */
  input_control: string
  /** 值规范化器（trim/upper/lower/none） */
  value_normalizer: string
}

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
  // CHANGE-20260713-011: filterAlias 已移除——stock 列改用普通筛选（contains/not_contains/eq），
  // 与顶部 keyword 搜索独立（顶部 keyword 负责 symbol/name/pinyin 正向搜索）
  // [CHANGE-20260730-013] 类型化筛选器元数据（来自 /market/filter-specs API）
  // 存在时 FilterPopover 按 data_type/input_control/enum_values 动态生成控件
  filterSpec?: ColumnFilterSpec
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
  // [PRD §三 列表视图第一金字塔全量字段] 默认隐藏列 key 集合
  // 仅在无 preset 加载时作为初始 hiddenColumns；preset 应用后由 preset.hiddenColumns 覆盖
  defaultHiddenColumns?: string[]
  // [StickyHeader] - 描述: 表头 sticky 模式
  // - container: 在 .table-scroll 局部滚动容器内吸附（默认，兼容历史行为）
  // - viewport: 在页面滚动时吸附到 topbar 下方（趋势选股页使用）
  stickyHeaderMode?: 'viewport' | 'container'
  // [StrategyDataTable] - 描述: 行点击回调（非链接区域点击时触发，用于 /market 选中行驱动右栏）
  onRowClick?: (row: Row) => void
  // [StrategyDataTable] - 描述: 当前选中行 key（用于高亮选中行）
  activeRowKey?: string | null
  // [StrategyDataTable] - 描述: 外部受控 keyword（提供时覆盖内部 globalQuery，用于 /market 顶部搜索框）
  externalKeyword?: string
  onKeywordChange?: (keyword: string) => void
  // CHANGE-20260713-006: 外部受控 industry/concept（/market 顶部板块筛选，preset 持久化）
  externalIndustry?: string
  onIndustryChange?: (industry: string) => void
  externalConcept?: string
  onConceptChange?: (concept: string) => void
  // CHANGE-20260713-006: preset 应用时校验 industry/concept 是否仍在当前板块目录中
  // 提供 boardsValidation 时，applyPresetConfig 会检测失效字段并调用 onPresetStaleField
  // CHANGE-20260716-007: industry 改为关键词匹配，不再校验是否在目录中；
  //   保留 industryNames 字段仅为类型兼容，实际不再使用。
  //   concept 仍校验精确匹配。
  boardsValidation?: {
    available: boolean
    industryNames: Set<string>
    conceptNames: Set<string>
  } | null
  onPresetStaleField?: (field: 'industry' | 'concept', value: string) => void
  // CHANGE-20260713-010: 导出 Excel 回调（提供时显示"导出 Excel"按钮）
  onExport?: (ctx: ExportContext) => void
}

// CHANGE-20260713-010: 导出上下文（StrategyDataTable → 外部）
export interface ExportContext {
  visibleColumns: DataTableColumn<unknown>[]
  keyword: string
  industry: string
  concept: string
  metricFilters: DataTableFilter[]
  sortBy: string | null
  sortDesc: boolean
}

// [StrategyDataTable] - 描述: 按字段类型返回可选操作符列表（默认操作符为数组首项）
function operatorsForDataType(dataType: DataTableColumn<unknown>['dataType']): FilterOperator[] {
  switch (dataType) {
    case 'number':
    case 'percent':
      return ['gte', 'gt', 'lte', 'lt', 'eq', 'between']
    case 'text':
      // CHANGE-20260713-011: 文本列增加 not_contains（包含/不包含/等于）
      return ['contains', 'not_contains', 'eq']
    case 'enum':
      return ['eq']
    case 'datetime':
      return ['gte', 'gt', 'lte', 'lt', 'between']
    default:
      return ['contains', 'eq']
  }
}

// [CHANGE-20260730-013] 优先使用 filterSpec.operators（来自 /market/filter-specs API）
// 当列携带 filterSpec 时按后端 SSOT 渲染操作符；否则回退到 dataType 默认值
function getAvailableOperators(column: DataTableColumn<unknown>): FilterOperator[] {
  if (column.filterSpec && column.filterSpec.operators.length > 0) {
    return column.filterSpec.operators as FilterOperator[]
  }
  return operatorsForDataType(column.dataType)
}

// [StrategyDataTable] - 描述: 操作符下拉框中文标签
// [CHANGE-20260730-013] 补全新操作符标签（neq/in/not_in/date_eq/before/after）
const OPERATOR_LABELS: Record<FilterOperator, string> = {
  contains: '包含',
  not_contains: '不包含',
  eq: '等于',
  neq: '不等于',
  gt: '大于',
  gte: '大于等于',
  lt: '小于',
  lte: '小于等于',
  between: '区间',
  in: '属于',
  not_in: '不属于',
  has_any: '包含任一',
  has_all: '包含全部',
  not_has_any: '不包含任一',
  date_eq: '日期等于',
  before: '早于',
  after: '晚于',
  empty: '为空',
  not_empty: '不为空',
}

// [CHANGE-20260730-013] 规范化筛选值（按 value_normalizer）
function normalizeFilterValue(value: string, normalizer: string | undefined): string {
  if (!value || !normalizer) return value
  switch (normalizer) {
    case 'trim':
      return value.trim()
    case 'upper':
      return value.trim().toUpperCase()
    case 'lower':
      return value.trim().toLowerCase()
    default:
      return value
  }
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
// [CHANGE-20260730-013] 补全新操作符的客户端匹配（neq/in/not_in/date_eq/before/after）
// 服务端模式下此函数不执行（由后端 SQL 处理）；仅用于客户端筛选模式（如非 serverSide 表格）
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
    case 'neq':
      // neq: 不等于值 OR 为空（与后端语义一致：显示所有不匹配的，包括 NULL）
      if (!t) return true
      return a.type === 'number' && b.type === 'number'
        ? a.value !== b.value
        : t.toLocaleLowerCase('zh-CN') !== String(filter.value).toLocaleLowerCase('zh-CN')
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
    case 'in': {
      // in: 值在逗号分隔列表中
      const values = String(filter.value || '')
        .split(',')
        .map((v) => v.trim())
        .filter(Boolean)
      return values.includes(t)
    }
    case 'not_in': {
      // not_in: 值不在逗号分隔列表中 OR 为空（与后端语义一致）
      if (!t) return true
      const values = String(filter.value || '')
        .split(',')
        .map((v) => v.trim())
        .filter(Boolean)
      return !values.includes(t)
    }
    case 'has_any': {
      const actual = t.split(',').map((v) => v.trim()).filter(Boolean)
      const expected = String(filter.value || '').split(',').map((v) => v.trim()).filter(Boolean)
      return expected.some((value) => actual.includes(value))
    }
    case 'has_all': {
      const actual = t.split(',').map((v) => v.trim()).filter(Boolean)
      const expected = String(filter.value || '').split(',').map((v) => v.trim()).filter(Boolean)
      return expected.every((value) => actual.includes(value))
    }
    case 'not_has_any': {
      const actual = t.split(',').map((v) => v.trim()).filter(Boolean)
      const expected = String(filter.value || '').split(',').map((v) => v.trim()).filter(Boolean)
      return expected.every((value) => !actual.includes(value))
    }
    case 'date_eq':
      // date_eq: 日期相等（字符串前 10 位匹配 YYYY-MM-DD）
      return t.slice(0, 10) === String(filter.value || '').slice(0, 10)
    case 'before':
      return t < String(filter.value || '')
    case 'after':
      return t > String(filter.value || '')
    default: // contains
      return t.toLocaleLowerCase('zh-CN').includes(String(filter.value).toLocaleLowerCase('zh-CN'))
    case 'not_contains':
      // CHANGE-20260713-011: 文本不包含（大小写不敏感）
      return !t.toLocaleLowerCase('zh-CN').includes(String(filter.value).toLocaleLowerCase('zh-CN'))
  }
}

// 列筛选弹窗
// [CHANGE-20260730-013] 根据 filterSpec.data_type/input_control/enum_values 动态生成控件：
// - enum/single_select + eq/neq → 下拉单选
// - enum + in/not_in → 多选（逗号分隔文本 + datalist 提示）
// - boolean/boolean_toggle + eq → true/false 下拉
// - datetime/date_picker + date_eq/before/after/between → 日期输入
// - number/percent/number_input → 数字输入
// - text/text_input → 文本输入
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
  // [CHANGE-20260730-013] 优先使用 filterSpec.operators；回退到 dataType 默认
  const availableOps = useMemo(
    () => getAvailableOperators(column),
    [column],
  )
  const [operator, setOperator] = useState<FilterOperator>(
    current?.operator && availableOps.includes(current.operator)
      ? current.operator
      : availableOps[0],
  )
  const [value, setValue] = useState(String(current?.value || ''))
  const [value2, setValue2] = useState(String(current?.value2 || ''))
  const [error, setError] = useState('')
  const isEmptyOp = operator === 'empty' || operator === 'not_empty'
  const isBetween = operator === 'between'

  // [CHANGE-20260730-013] 控件类型派生（基于 filterSpec）
  const spec = column.filterSpec
  const enumValues = spec?.enum_values ?? []
  const inputControl = spec?.input_control ?? ''
  const normalizer = spec?.value_normalizer
  // enum 字段且操作符为 eq/neq 时使用下拉单选
  const isEnumSingleSelect =
    (inputControl === 'single_select' || enumValues.length > 0) &&
    (operator === 'eq' || operator === 'neq')
  // enum 字段且操作符为 in/not_in 时使用多选（逗号分隔）
  const isEnumMultiSelect =
    (inputControl === 'multi_select' || enumValues.length > 0) &&
    (operator === 'in' || operator === 'not_in' || operator === 'has_any' ||
      operator === 'has_all' || operator === 'not_has_any')
  // boolean 字段使用 true/false 下拉
  const isBooleanSelect = inputControl === 'boolean_toggle' && operator === 'eq'
  // datetime 字段使用日期输入
  const isDatePicker = inputControl === 'date_picker' ||
    operator === 'date_eq' || operator === 'before' || operator === 'after'
  // number/percent 字段使用数字输入
  // 注意：between 必须走下方双输入框分支，因此这里统一排除 between。
  // 否则 number_input 会先命中单输入框分支，导致「区间」只显示一个输入框。
  const isNumberInput = !isBetween &&
    (inputControl === 'number_input' ||
      (spec && (spec.data_type === 'number' || spec.data_type === 'percent') &&
        (operator === 'eq' || operator === 'neq' || operator === 'gt' ||
         operator === 'gte' || operator === 'lt' || operator === 'lte')))
  // between 是否按数值语义校验（number/percent 或数字输入控件）
  const isNumericBetween = isBetween &&
    (inputControl === 'number_input' ||
      spec?.data_type === 'number' || spec?.data_type === 'percent')

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
    // [CHANGE-20260730-013] 应用 value_normalizer 规范化筛选值
    const val = normalizeFilterValue(value, normalizer)
    const val2 = normalizeFilterValue(value2, normalizer)
    // between 需要两个值都非空；任一为空或区间非法时显示提示，不提交也不静默清空
    if (isBetween) {
      if (!val || !val2) {
        setError(
          isDatePicker
            ? '请同时填写起始日期与结束日期'
            : '请同时填写下界与上界',
        )
        return
      }
      if (isNumericBetween) {
        const lower = Number(val)
        const upper = Number(val2)
        if (Number.isNaN(lower) || Number.isNaN(upper)) {
          setError('下界与上界必须是数值')
          return
        }
        if (lower > upper) {
          setError('下界不能大于上界')
          return
        }
      } else if (isDatePicker && String(val) > String(val2)) {
        setError('起始日期不能晚于结束日期')
        return
      }
      setError('')
      onApply({ key: column.key, operator, value: val, value2: val2 })
      return
    }
    if (!isEmptyOp && !val) {
      setError('请输入筛选值')
      return
    }
    setError('')
    onApply({ key: column.key, operator, value: val })
  }

  // [CHANGE-20260730-013] 渲染值输入控件（根据 data_type/input_control/operator）
  const renderValueInput = () => {
    if (isEmptyOp) {
      return (
        <input
          className="input filter-value"
          placeholder="（无需输入）"
          value={value}
          disabled
          onChange={(e) => setValue(e.target.value)}
        />
      )
    }

    // enum 单选：下拉选择 enum_values
    if (isEnumSingleSelect && enumValues.length > 0) {
      return (
        <select
          className="select filter-value"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        >
          <option value="">请选择</option>
          {enumValues.map((v) => (
            <option key={v} value={v}>{v}</option>
          ))}
        </select>
      )
    }

    // boolean：true/false 下拉
    if (isBooleanSelect) {
      return (
        <select
          className="select filter-value"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        >
          <option value="">请选择</option>
          <option value="true">是 (true)</option>
          <option value="false">否 (false)</option>
        </select>
      )
    }

    // enum 多选（in/not_in）：逗号分隔文本 + datalist 提示可选值
    if (isEnumMultiSelect && enumValues.length > 0) {
      return (
        <div>
          <input
            className="input filter-value"
            placeholder="多个值用逗号分隔"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoFocus
            list={`filter-enum-list-${column.key}`}
          />
          <datalist id={`filter-enum-list-${column.key}`}>
            {enumValues.map((v) => (
              <option key={v} value={v} />
            ))}
          </datalist>
          <div className="filter-enum-hint">
            可选值：{enumValues.join(' / ')}
          </div>
        </div>
      )
    }

    // 日期输入（date_eq/before/after/between）
    if (isDatePicker) {
      if (isBetween) {
        return (
          <div className="filter-between-inputs">
            <input
              className="input filter-value"
              type="date"
              placeholder="起始日期"
              aria-label="起始日期"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              autoFocus
            />
            <span className="filter-between-sep">~</span>
            <input
              className="input filter-value"
              type="date"
              placeholder="结束日期"
              aria-label="结束日期"
              value={value2}
              onChange={(e) => setValue2(e.target.value)}
            />
          </div>
        )
      }
      return (
        <input
          className="input filter-value"
          type="date"
          placeholder="选择日期"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
      )
    }

    // 数字输入
    if (isNumberInput) {
      return (
        <input
          className="input filter-value"
          type="number"
          placeholder="输入数值"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
      )
    }

    // 区间输入：number/percent 使用数值输入，其余回退为文本
    if (isBetween) {
      return (
        <div className="filter-between-inputs">
          <input
            className="input filter-value"
            type={isNumericBetween ? 'number' : 'text'}
            placeholder="下界"
            aria-label="下界"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoFocus
          />
          <span className="filter-between-sep">~</span>
          <input
            className="input filter-value"
            type={isNumericBetween ? 'number' : 'text'}
            placeholder="上界"
            aria-label="上界"
            value={value2}
            onChange={(e) => setValue2(e.target.value)}
          />
        </div>
      )
    }

    // 默认：文本输入
    return (
      <input
        className="input filter-value"
        placeholder="输入筛选值"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        autoFocus
      />
    )
  }

  return (
    <div className="column-filter-popover" style={{ left, top }}>
      <div className="filter-pop-title">筛选：{column.title}</div>
      <select
        className="select filter-operator"
        value={operator}
        onChange={(e) => {
          setOperator(e.target.value as FilterOperator)
          setError('')
        }}
      >
        {availableOps.map((op) => (
          <option key={op} value={op}>
            {OPERATOR_LABELS[op]}
          </option>
        ))}
      </select>
      {renderValueInput()}
      {error ? (
        <div className="filter-error" role="alert">
          {error}
        </div>
      ) : null}
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

// CHANGE-20260713-011: KeywordFilterPopover 已移除——stock 列改用普通 FilterPopover
// （支持 contains/not_contains/eq 三种操作符，筛选值只取股票名称）

// CHANGE-20260715-005: sticky 列判断函数——只允许 col.key==='stock' 为 sticky 列
// 禁止"第一个可见非操作列"自动 sticky；股票列隐藏时不自动把其他列套用 sticky
function isStickyColumn<Row>(col: DataTableColumn<Row>): boolean {
  return col.key === 'stock'
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

// [CHANGE-20260902] 排序合同统一校验（实现见 ./sortGuard，纯函数便于单测）
import { resolveValidSort } from './sortGuard'

// [CHANGE-20260902] 表头 ? 帮助 tooltip：Portal 到 document.body，避免被 table-scroll/table-shell 的
// overflow 裁剪（单纯加 z-index 无效）。position: fixed 基于触发元素的 getBoundingClientRect；
// 靠近视口顶部时显示在下方；左右边缘 clamp 不跑出 viewport；hover 与键盘 focus 均可触发，leave/blur 关闭。
const HELP_TOOLTIP_WIDTH = 260
function ColumnHelpTooltip({ text, title }: { text: string; title?: string }) {
  const triggerRef = useRef<HTMLSpanElement>(null)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number; placement: 'top' | 'bottom' }>({
    top: 0,
    left: 0,
    placement: 'top',
  })

  const show = () => {
    const el = triggerRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    let left = rect.left + rect.width / 2 - HELP_TOOLTIP_WIDTH / 2
    left = Math.max(8, Math.min(left, window.innerWidth - HELP_TOOLTIP_WIDTH - 8))
    const placement: 'top' | 'bottom' = rect.top < 140 ? 'bottom' : 'top'
    const top = placement === 'top' ? rect.top : rect.bottom
    setPos({ top, left, placement })
    setOpen(true)
  }

  return (
    <>
      <span
        ref={triggerRef}
        className="th-help"
        tabIndex={0}
        role="button"
        aria-label={title ? `${title} 说明` : '列说明'}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onFocus={show}
        onBlur={() => setOpen(false)}
      >
        ?
      </span>
      {open &&
        createPortal(
          <div
            className={`th-help-portal ${pos.placement === 'bottom' ? 'th-help-bottom' : 'th-help-top'}`}
            style={{
              position: 'fixed',
              top: pos.top,
              left: pos.left,
              width: HELP_TOOLTIP_WIDTH,
              zIndex: 9999,
              transform:
                pos.placement === 'top'
                  ? 'translateY(calc(-100% - 8px))'
                  : 'translateY(8px)',
            }}
            onMouseEnter={() => setOpen(true)}
            onMouseLeave={() => setOpen(false)}
          >
            {title && <div className="th-help-title">{title}</div>}
            <div className="th-help-body">{text}</div>
          </div>,
          document.body,
        )}
    </>
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
    // [PRD §三 列表视图第一金字塔全量字段] 默认隐藏列 key 集合
    // 仅在无 preset 加载时生效；preset 应用后由 preset.hiddenColumns 覆盖
    defaultHiddenColumns,
    stickyHeaderMode = 'container',
    onRowClick,
    activeRowKey,
    externalKeyword,
    onKeywordChange,
    externalIndustry,
    onIndustryChange,
    externalConcept,
    onConceptChange,
    boardsValidation,
    onPresetStaleField,
    onExport,
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
  const [hiddenColumns, setHiddenColumns] = useState<Set<string>>(
    () => new Set(defaultHiddenColumns ?? []),
  )
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
      // [CHANGE-20260902] 统一校验：非法（非 sortable）的排序键忽略，不发给 API
      const validIdx = resolveValidSort(state.sort, columns)
      if (validIdx >= 0) {
        setSortColumn(validIdx)
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
        // [CHANGE-20260730-013] 优先使用 filterSpec.operators 校验（支持新操作符 in/not_in/date_eq 等）
        const ops = getAvailableOperators(columns[idx] as DataTableColumn<unknown>)
        const operator = canonicalizeFilterOperator(f.op) as FilterOperator
        if (!ops.includes(operator)) continue
        next[idx] = {
          key: f.key,
          operator,
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
    // CHANGE-20260713-011: 清除排序与筛选时必须同时清外部受控状态（keyword/industry/concept）
    // 并在 URL 写入 preset=none，禁止默认 preset 在组件 remount 后自动应用
    setFilters({})
    setSortColumn(null)
    setSortDirection(null)
    setGlobalQuery('')
    setPage(1)
    // 同步外部受控 keyword/industry/concept（MarketWorkspacePage 顶部搜索 + 板块筛选）
    if (onKeywordChange) onKeywordChange('')
    if (onIndustryChange) onIndustryChange('')
    if (onConceptChange) onConceptChange('')
    // URL: 删除 keyword/filters/sort/dir/page/industry/concept，写入 preset=none
    // managedKeys 由 URL sync effect 自动清理（state 已清空，encoded 不会有这些 key）
    // industry/concept 不在 managedKeys 中，需手动删除
    const nextParams = new URLSearchParams(searchParams)
    nextParams.delete('industry')
    nextParams.delete('concept')
    nextParams.set('preset', 'none')
    setSearchParams(nextParams, { replace: false })
  }, [onKeywordChange, onIndustryChange, onConceptChange, searchParams, setSearchParams])

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
    industry: externalIndustry?.trim() || null,
    concept: externalConcept?.trim() || null,
  }), [effectiveKeyword, sortColumn, sortDirection, filters, hiddenColumns, columnOrder, pageSize, columns, externalIndustry, externalConcept])

  // [Presets] - 描述: 应用 preset 配置到内部 state（重置所有筛选/排序/分页/隐藏列/列顺序）
  // CHANGE-20260713-011: 用户显式点击 preset 时删除 URL 中的 preset=none（解除"清除后不自动应用"门控）
  const applyPresetConfig = useCallback((config: TableViewPresetConfig) => {
    // 删除 preset=none（用户显式应用，恢复默认 preset 自动应用机制）
    if (searchParams.get('preset') === 'none') {
      const nextParams = new URLSearchParams(searchParams)
      nextParams.delete('preset')
      setSearchParams(nextParams, { replace: false })
    }
    setGlobalQuery(config.keyword ?? '')
    if (onKeywordChange) onKeywordChange(config.keyword ?? '')
    if (config.sort) {
      // [CHANGE-20260902] 统一校验：非法（非 sortable）的排序键忽略，不发给 API
      const validIdx = resolveValidSort(config.sort, columns)
      setSortColumn(validIdx >= 0 ? validIdx : null)
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
          const operator = canonicalizeFilterOperator(f.op) as FilterOperator
          const ops = getAvailableOperators(columns[idx] as DataTableColumn<unknown>)
          if (!ops.includes(operator)) continue
          next[idx] = {
            key: f.key,
            operator,
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
    // CHANGE-20260713-006: 恢复 industry/concept 到外部受控 state（MarketWorkspacePage URL）
    // 同时校验 preset 中的值是否仍在当前板块目录中：
    // - boardsValidation.available=true 且值不在目录中 → 视为失效字段，跳过应用并通知父组件 toast
    // - boardsValidation.available=false → 保留 preset 值但禁用输入（父组件处理 disabled 状态）
    // - 无 boardsValidation → 直接应用（兼容 ScreenerPage 等不传板块校验的场景）
    // CHANGE-20260716-007: industry 改为关键词匹配，不再校验是否在目录中（任何关键词都合法）；
    //   只校验 concept 是否在概念目录中。
    const staleFields: Array<{ field: 'industry' | 'concept'; value: string }> = []
    let effectiveIndustry = config.industry ?? ''
    let effectiveConcept = config.concept ?? ''
    if (boardsValidation && boardsValidation.available) {
      if (effectiveConcept && !boardsValidation.conceptNames.has(effectiveConcept)) {
        staleFields.push({ field: 'concept', value: effectiveConcept })
        effectiveConcept = ''
      }
    }
    if (onIndustryChange) onIndustryChange(effectiveIndustry)
    if (onConceptChange) onConceptChange(effectiveConcept)
    // 通知父组件显示 toast（每个失效字段 toast 一次）
    if (onPresetStaleField) {
      for (const sf of staleFields) onPresetStaleField(sf.field, sf.value)
    }
    setPage(1)
  }, [columns, saveColumnOrder, onKeywordChange, onIndustryChange, onConceptChange, boardsValidation, onPresetStaleField, searchParams, setSearchParams])

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
    // CHANGE-20260714-001: 用户显式"清除排序与筛选"后 URL 含 preset=none，
    // 禁止默认 preset 自动应用（即使 URL 中没有其他状态字段）。
    // 用户主动点击某个 preset 时由 applyPresetConfig 删除 preset=none 解除门控。
    if (searchParams.get('preset') === 'none') {
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
        // CHANGE-20260713-006: 恢复 industry/concept（preset 持久化）
        industry: (cfg.industry as string | null | undefined) ?? null,
        concept: (cfg.concept as string | null | undefined) ?? null,
      })
    }
    defaultAppliedRef.current = appliedKey
  }, [strategyKey, tableId, presetsQuery.data, applyPresetConfig, searchParams])

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
    // [Round 2026-07-28] 防止 searchParams → setSearchParams → searchParams 无限循环：
    // 仅在 managed keys 实际变化时才 setSearchParams
    let changed = false
    for (const key of managedKeys) {
      const oldVal = searchParams.get(key)
      const newVal = nextParams.get(key)
      if (oldVal !== newVal) {
        changed = true
        break
      }
    }
    if (changed) {
      setSearchParams(nextParams, { replace: true })
    }
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

  return (
    <div className={clsx('table-shell', stickyHeaderMode === 'viewport' && 'viewport-sticky')}>
      {/* 元信息栏（CHANGE-20260715-005: 移出横向滚动容器，右边界等于 table-scroll 右边界） */}
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
          {onExport && (
            <button
              className="btn small secondary export-btn"
              disabled={!activeRunId}
              onClick={() => {
                const exportableColumns = visibleColumns
                  .filter(({ col }) => !col.isAction && !col.isSelect)
                  .map(({ col }) => col as DataTableColumn<unknown>)
                onExport({
                  visibleColumns: exportableColumns,
                  keyword: effectiveKeyword,
                  industry: externalIndustry ?? '',
                  concept: externalConcept ?? '',
                  metricFilters: Object.values(filters),
                  sortBy: sortColumn !== null ? columns[sortColumn]?.key ?? null : null,
                  sortDesc: sortDirection === 'desc',
                })
              }}
            >
              导出 Excel
            </button>
          )}
        </div>
      </div>

      {/* 全文搜索（CHANGE-20260715-005: 移出横向滚动容器） */}
      {searchable && (
        <div className="table-search-bar">
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

      {/* 表格滚动容器（CHANGE-20260715-005: 只有 table-scroll 设置 overflow-x: auto） */}
      <div className="table-scroll">
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
                if (col.isAction) {
                  return (
                    <th key={col.key} className="table-action-column">
                      {col.title}
                    </th>
                  )
                }

                // CHANGE-20260715-005: 只允许 col.key==='stock' 为 sticky 列
                const isSticky = isStickyColumn(col)

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
                        <ColumnHelpTooltip text={col.helpText} title={col.title} />
                      )}
                      {col.filterable && (
                        <button
                          className={clsx(
                            'th-filter',
                            filters[i] && 'active',
                          )}
                          aria-label={`筛选${col.title}`}
                          title={`筛选${col.title}`}
                          onClick={(e) => {
                            e.stopPropagation()
                            setFilterPopover({
                              columnIndex: i,
                              anchor: e.currentTarget,
                            })
                          }}
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
                return (
                  <tr
                    key={key}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    className={clsx(activeRowKey === key && 'row-active')}
                  >
                    {selectable && (
                      <td className="table-select-column" onClick={(e) => e.stopPropagation()}>
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
                      // CHANGE-20260715-005: 只允许 col.key==='stock' 为 sticky 列（header 和 body 用同一判断函数）
                      const isSticky = isStickyColumn(col)
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
      </div>

      {/* 分页（CHANGE-20260715-005: 移出横向滚动容器，右边界等于 table-scroll 右边界） */}
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
