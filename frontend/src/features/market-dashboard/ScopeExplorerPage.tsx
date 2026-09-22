// [R3C] Industry / Concept Explorer 主页面（共用组件，由 route 以 scopeType 区分）。
//
// 设计要点：
// - URL search params 是全部 applied server state 的 SSOT（parse/serialize 在 scopeExplorerUrlState.ts）。
// - 列表 / 排序 / 筛选 / 分页全部 server-side；前端只渲染 API items，绝不 items.sort/filter/slice。
// - 选中板块写入 URL board_id；详情用 useMarketScopeDetail，独立于列表 state。
// - 详情图表复用 R3A/R3B 的 unified chart 基础（BreadthChart + 6-series），不写第二套。
//
// [PANJI-REVIEW-UI-UNIFY] 筛选入口统一到表头：
// - 删除独立 filter-panel（MA5/MA10/5日Δ/成员数 min-max + 应用筛选 + 重置整块）；
// - 改为「列 funnel → 弹层（区间 / ≥ / ≤ / =）」+ 表格上方 slim meta bar（结果数 + 激活条件 chips
//   + 快速筛选 ▾ + 清除筛选）；
// - 搜索与 L1/L2/L3 仍留在表格外（它们是数据范围，不是列筛选）；
// - URL 仍是唯一正式状态：弹层可有 local draft，Apply 后一律走 applyPatch() → URL。
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMarketScopeExplorer, useMarketScopeDetail } from '@/hooks/useMarketDashboardApi'
import { useCompareBasketStore } from '@/store/compareBasket'
import { extractMarketDashboardError } from '@/api/marketDashboard'
import BreadthChart from './BreadthChart'
import type { BreadthLineSpec } from './BreadthChart'
import DashboardState from './DashboardState'
import DashboardTabs from './DashboardTabs'
import ScopeExplorerTable from './ScopeExplorerTable'
import { MARKET_OVERVIEW_SERIES } from './marketOverviewConfig'
import { BREADTH_REFERENCE_LINES } from './chartTheme'
import { classifyDashboardError } from './dashboardLogic'
import type { BreadthPoint, HierarchyLevel, ScopeType, ScopeExplorerItem } from './types'
import {
  activeExplorerFilterChips,
  clearFiltersPatch,
  draftFromFilterMode,
  EXPLORER_FILTERABLE_COLUMNS,
  EXPLORER_FILTER_COLUMN_SPECS,
  EXPLORER_FILTER_MODES,
  EXPLORER_PAGE_SIZE_OPTIONS,
  filterKeysForColumn,
  isExplorerColumnFiltered,
  MA5_PRESETS,
  NUMERIC_FILTER_KEYS,
  parseConceptExplorerSearch,
  parseIndustryExplorerSearch,
  ratioToUi,
  serializeExplorerState,
  uiToRatio,
  updateExplorerState,
  type ExplorerFilterColumn,
  type ExplorerFilterMode,
  type ExplorerParsed,
  type ExplorerStatePatch,
  type NumericFilterKey,
} from './scopeExplorerUrlState'
import styles from './dashboard.module.scss'

const DETAIL_DAYS = 250
const LEVELS: readonly HierarchyLevel[] = ['L1', 'L2', 'L3']
const POPOVER_WIDTH = 260

export default function ScopeExplorerPage({ scopeType }: { scopeType: ScopeType }) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const isIndustry = scopeType === 'industry'

  const parsed: ExplorerParsed = useMemo(
    () => (isIndustry ? parseIndustryExplorerSearch(searchParams) : parseConceptExplorerSearch(searchParams)),
    [searchParams, isIndustry],
  )

  // 规范化 URL：concept 剥离 hierarchy_level、非法值归位（刷新后稳定、可恢复）。
  useEffect(() => {
    const canonical = serializeExplorerState(parsed, scopeType).toString()
    if (searchParams.toString() !== canonical) setSearchParams(canonical, { replace: true })
  }, [parsed, scopeType, searchParams, setSearchParams])

  // 完整 server-state query（filter 直接用 backend ratio 单位；null 由 buildScopeExplorerParams 跳过）。
  const query = useMemo(
    () => ({
      scope_type: scopeType,
      hierarchy_level: isIndustry ? parsed.hierarchy_level : null,
      q: parsed.q || null,
      page: parsed.page,
      page_size: parsed.page_size,
      sort: parsed.sort,
      direction: parsed.direction,
      ...parsed.filters,
    }),
    [scopeType, isIndustry, parsed],
  )

  const explorer = useMarketScopeExplorer(query)
  const detail = useMarketScopeDetail(parsed.board_id, DETAIL_DAYS)

  // ---- 应用 patch（除显式 page 外一律回到第 1 页）----
  const applyPatch = (patch: ExplorerStatePatch) => {
    setSearchParams(serializeExplorerState(updateExplorerState(parsed, patch), scopeType))
  }

  // ---- 搜索（local draft → URL 唯一事实源）----
  const [draftQ, setDraftQ] = useState(parsed.q)
  useEffect(() => setDraftQ(parsed.q), [parsed.q])
  const submitSearch = (e: FormEvent) => {
    e.preventDefault()
    applyPatch({ q: draftQ.trim() })
  }
  const clearSearch = () => {
    setDraftQ('')
    applyPatch({ q: '' })
  }

  // ---- 数值筛选（UI 单位 % / pp / 整数 → backend ratio；URL 仍是唯一真源）----
  // 显式写出**每个** numeric key（含 null），否则 merge 后旧 URL filter 不会被清除。
  const buildFilters = (
    overrides: readonly (readonly [NumericFilterKey, string])[],
  ): Record<NumericFilterKey, number | null> => {
    const overrideMap = new Map<NumericFilterKey, string>(overrides)
    const filters: Record<NumericFilterKey, number | null> = {} as Record<NumericFilterKey, number | null>
    for (const k of NUMERIC_FILTER_KEYS) {
      const raw = overrideMap.has(k) ? overrideMap.get(k)! : ratioToUi(k, parsed.filters[k])
      filters[k] = uiToRatio(k, raw)
    }
    return filters
  }

  const applyColumnFilter = (
    column: ExplorerFilterColumn,
    mode: ExplorerFilterMode,
    lower: string,
    upper: string,
  ) => {
    const { min: minKey, max: maxKey } = filterKeysForColumn(column)
    const { min, max } = draftFromFilterMode(mode, lower, upper)
    applyPatch({ filters: buildFilters([[minKey, min], [maxKey, max]]) })
  }

  const clearColumnFilter = (column: ExplorerFilterColumn) => {
    const { min: minKey, max: maxKey } = filterKeysForColumn(column)
    applyPatch({ filters: buildFilters([[minKey, ''], [maxKey, '']]) })
  }

  const clearAllFilters = () => {
    setPresetOpen(false)
    applyPatch(clearFiltersPatch())
  }

  // ---- 表头筛选：激活列集合 + chips（都由 URL 派生）----
  const filteredColumns = useMemo(() => {
    const active = new Set<ExplorerFilterColumn>()
    for (const column of EXPLORER_FILTERABLE_COLUMNS) {
      if (isExplorerColumnFiltered(column, parsed.filters)) active.add(column)
    }
    return active
  }, [parsed.filters])

  const filterChips = useMemo(() => activeExplorerFilterChips(parsed.filters), [parsed.filters])

  // ---- 筛选弹层（local draft，Apply → URL）----
  const [filterPopover, setFilterPopover] = useState<{
    column: ExplorerFilterColumn
    anchor: HTMLElement
  } | null>(null)

  // ---- 快速筛选 ▾（沿用既有 MA5_PRESETS，不建第二套 preset 逻辑）----
  const [presetOpen, setPresetOpen] = useState(false)
  const quickFilterRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!presetOpen) return
    const onDocMouseDown = (e: MouseEvent) => {
      if (quickFilterRef.current && e.target instanceof Node && quickFilterRef.current.contains(e.target)) return
      setPresetOpen(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [presetOpen])

  // ---- 排序（点击 active 列切换方向；新列 numeric→desc / name→asc）----
  const onSort = (field: ExplorerParsed['sort']) => {
    if (parsed.sort === field) {
      applyPatch({ direction: parsed.direction === 'asc' ? 'desc' : 'asc' })
    } else {
      applyPatch({ sort: field, direction: field === 'name' ? 'asc' : 'desc' })
    }
  }

  // ---- 行选中（写入 board_id，保留当前页）----
  const onSelect = (id: string) => applyPatch({ board_id: id, page: parsed.page })

  // ---- 层级切换（industry：清 board_id、回到第 1 页）----
  const onLevel = (lv: HierarchyLevel) => applyPatch({ hierarchy_level: lv, board_id: null })

  // ---- 分页 ----
  const total = explorer.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / parsed.page_size))
  const goPage = (p: number) => applyPatch({ page: Math.min(Math.max(1, p), totalPages) })
  const changePageSize = (ps: number) => applyPatch({ page_size: ps, page: 1 })

  // ---- 比较篮 ----
  const addToBasket = useCompareBasketStore((s) => s.add)
  const [basketMsg, setBasketMsg] = useState<string | null>(null)
  const addCompare = (id: string, name: string, type: ScopeType) => {
    setBasketMsg(null)
    const result = addToBasket({ id, name, type })
    if (result === 'full') setBasketMsg('比较篮已满（最多 20 个板块）')
    else if (result === 'duplicate') setBasketMsg('该板块已加入比较')
  }

  // ---- type-aware canonicalization：detail 返回后若 route 与 type 不符则重定向 ----
  const meta = detail.data?.metadata
  useEffect(() => {
    if (!detail.data || !meta) return
    if (meta.type !== scopeType) {
      const target =
        meta.type === 'industry'
          ? `/review/industry?hierarchy_level=${meta.hierarchy_level}&board_id=${encodeURIComponent(meta.board_id)}`
          : `/review/concept?board_id=${encodeURIComponent(meta.board_id)}`
      navigate(target, { replace: true })
    }
  }, [detail.data, meta, scopeType, navigate])

  // ---- 详情图表 series（复用 unified 6-series 基础）----
  const detailSeries: BreadthLineSpec[] = useMemo(
    () =>
      MARKET_OVERVIEW_SERIES.map((s) => ({
        field: s.field as keyof BreadthPoint,
        label: s.label,
        color: s.color,
        scale: s.scale,
        lineWidth: s.lineWidth,
        breadthPercent: s.breadthPercent,
        fixedScaleRange: s.fixedScaleRange,
      })),
    [],
  )

  const items = explorer.data?.items ?? []
  const listError = explorer.error ? classifyDashboardError(extractMarketDashboardError(explorer.error)) : null
  const detailError = detail.error ? classifyDashboardError(extractMarketDashboardError(detail.error)) : null

  return (
    <div className={styles['explorer-page']} data-testid="explorer-page">
      <h1 className={styles['page-title']}>{isIndustry ? '行业' : '概念'}</h1>
      <DashboardTabs />

      {isIndustry ? (
        <div className={styles['level-selector']}>
          {LEVELS.map((lv) => (
            <button
              key={lv}
              type="button"
              className={parsed.hierarchy_level === lv ? styles['level-btn-active'] : styles['level-btn']}
              onClick={() => onLevel(lv)}
              aria-pressed={parsed.hierarchy_level === lv}
            >
              {lv}
            </button>
          ))}
        </div>
      ) : (
        <p className={styles['concept-note']}>同花顺概念 / 问财概念</p>
      )}

      {/* Toolbar：搜索（数据范围，保留在表格之外） */}
      <form className={styles.toolbar} onSubmit={submitSearch}>
        <input
          className={styles['search-input']}
          type="text"
          value={draftQ}
          placeholder={isIndustry ? '搜索行业' : '搜索概念'}
          onChange={(e) => setDraftQ(e.target.value)}
          data-testid="search-input"
        />
        <button type="submit" className={styles['btn-primary']}>
          搜索
        </button>
        {parsed.q && (
          <button type="button" className={styles['btn-ghost']} onClick={clearSearch}>
            清空
          </button>
        )}
      </form>

      {/* slim meta bar：结果数 + 激活筛选 chips + 快速筛选 + 清除筛选 */}
      <div className={styles['explorer-meta-bar']} data-testid="explorer-meta-bar">
        <div className={styles['explorer-meta-left']}>
          <span className={styles['explorer-result-count']}>结果 {total}</span>
          <span className={styles['explorer-filter-chips']} data-testid="explorer-filter-chips">
            {filterChips.map((chip) => (
              <button
                key={chip.column}
                type="button"
                className={styles['filter-chip']}
                title={`清除「${chip.label}」`}
                aria-label={`清除筛选 ${chip.label}`}
                onClick={() => clearColumnFilter(chip.column)}
              >
                <span className={styles['filter-chip-text']}>{chip.label}</span>
                <span className={styles['filter-chip-x']} aria-hidden="true">
                  ×
                </span>
              </button>
            ))}
          </span>
        </div>
        <div className={styles['explorer-meta-actions']}>
          <div className={styles['quick-filter']} ref={quickFilterRef}>
            <button
              type="button"
              className={styles['quick-filter-btn']}
              aria-haspopup="menu"
              aria-expanded={presetOpen}
              onClick={() => setPresetOpen((v) => !v)}
              data-testid="quick-filter-btn"
            >
              快速筛选 ▾
            </button>
            {presetOpen && (
              <div className={styles['quick-filter-menu']} role="menu">
                {MA5_PRESETS.map((p) => (
                  <button
                    key={p.key}
                    type="button"
                    role="menuitem"
                    className={styles['quick-filter-item']}
                    data-testid={`preset-${p.key}`}
                    onClick={() => {
                      setPresetOpen(false)
                      applyPatch({ filters: p.filters })
                    }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            type="button"
            className={styles['clear-filters-btn']}
            disabled={filterChips.length === 0}
            onClick={clearAllFilters}
            data-testid="clear-all-filters"
          >
            清除筛选
          </button>
        </div>
      </div>

      {/* 列筛选弹层（URL 为真源，弹层内仅 local draft） */}
      {filterPopover && (
        <ScopeFilterPopover
          key={filterPopover.column}
          column={filterPopover.column}
          anchor={filterPopover.anchor}
          filters={parsed.filters}
          onApply={(mode, lower, upper) => {
            applyColumnFilter(filterPopover.column, mode, lower, upper)
            setFilterPopover(null)
          }}
          onClear={() => {
            clearColumnFilter(filterPopover.column)
            setFilterPopover(null)
          }}
          onClose={() => setFilterPopover(null)}
        />
      )}

      {/* 列表 */}
      {explorer.isLoading ? (
        <DashboardState kind="loading" />
      ) : listError ? (
        <DashboardState kind={listError.kind} desc={listError.detail} />
      ) : items.length === 0 ? (
        <DashboardState kind="empty" desc="当前条件暂无板块" />
      ) : (
        <>
          <ScopeExplorerTable
            items={items}
            sort={parsed.sort}
            direction={parsed.direction}
            onSort={onSort}
            selectedId={parsed.board_id}
            onSelect={onSelect}
            onAddCompare={(it: ScopeExplorerItem) => addCompare(it.board_id, it.board_name, it.board_type as ScopeType)}
            filteredColumns={filteredColumns}
            onFilterClick={(column, anchor) => setFilterPopover({ column, anchor })}
          />

          <div className={styles.pagination}>
            <button
              type="button"
              className={styles['page-btn']}
              onClick={() => goPage(parsed.page - 1)}
              disabled={parsed.page <= 1}
            >
              上一页
            </button>
            <span className={styles['page-info']}>
              第 {parsed.page} / {totalPages} 页 · 共 {total} 项
            </span>
            <button
              type="button"
              className={styles['page-btn']}
              onClick={() => goPage(parsed.page + 1)}
              disabled={parsed.page >= totalPages}
            >
              下一页
            </button>
            <select
              className={styles['page-size']}
              value={parsed.page_size}
              onChange={(e) => changePageSize(Number(e.target.value))}
              aria-label="每页条数"
            >
              {EXPLORER_PAGE_SIZE_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o}/页
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {/* 选中板块详情 */}
      {parsed.board_id && (
        <div className={styles['detail-section']} data-testid="scope-detail">
          {detail.isLoading ? (
            <DashboardState kind="loading" />
          ) : detail.error && detailError ? (
            <DashboardState kind={detailError.kind} desc={detailError.detail} />
          ) : meta ? (
            <>
              <div className={styles['detail-head']}>
                <div>
                  <div className={styles['detail-name']}>{meta.name}</div>
                  <div className={styles['detail-sub']}>
                    {meta.type === 'industry' ? `行业 · ${meta.hierarchy_level}` : '概念'} · 成员数 {meta.member_count.toLocaleString()} · 数据日期{' '}
                    {detail.data?.projection_trade_date ?? '—'}
                  </div>
                </div>
                <button
                  type="button"
                  className={styles['btn-primary']}
                  onClick={() => addCompare(meta.board_id, meta.name, meta.type as ScopeType)}
                >
                  加入对比
                </button>
              </div>

              <BreadthChart
                points={detail.data?.series as unknown as BreadthPoint[]}
                series={detailSeries}
                referenceLines={BREADTH_REFERENCE_LINES}
                height={300}
              />

              {basketMsg && <div className={styles['compare-msg']}>{basketMsg}</div>}
              <div className={styles['compare-link-row']}>
                {/* SPA 导航：保留 Zustand compare basket（R3A 仅 session persistence，不 localStorage）。整页 reload 会清空 basket。 */}
                <Link className={styles['summary-link']} to="/review/compare">
                  查看对比
                </Link>
              </div>
            </>
          ) : null}
        </div>
      )}
    </div>
  )
}

// 列筛选弹层：条件（区间 / ≥ / ≤ / =）+ 最小值 / 最大值。
// 当前 backend 只有 min/max 区间模型，四种条件最终都映射为 <col>_min / <col>_max 两个 query param。
function ScopeFilterPopover({
  column,
  anchor,
  filters,
  onApply,
  onClear,
  onClose,
}: {
  column: ExplorerFilterColumn
  anchor: HTMLElement
  filters: Record<NumericFilterKey, number | null>
  onApply: (mode: ExplorerFilterMode, lower: string, upper: string) => void
  onClear: () => void
  onClose: () => void
}) {
  const spec = EXPLORER_FILTER_COLUMN_SPECS[column]
  const { min: minKey, max: maxKey } = filterKeysForColumn(column)
  const [mode, setMode] = useState<ExplorerFilterMode>(() => {
    const lo = filters[minKey]
    const hi = filters[maxKey]
    if (lo !== null && hi !== null) return lo === hi ? 'eq' : 'range'
    if (lo !== null) return 'gte'
    if (hi !== null) return 'lte'
    return 'range'
  })
  const [lower, setLower] = useState(() => ratioToUi(minKey, filters[minKey]))
  const [upper, setUpper] = useState(() => ratioToUi(maxKey, filters[maxKey]))
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent) => {
      if (rootRef.current && e.target instanceof Node && rootRef.current.contains(e.target)) return
      onClose()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [onClose])

  const rect = anchor.getBoundingClientRect()
  const left = Math.max(8, Math.min(rect.right - POPOVER_WIDTH, window.innerWidth - POPOVER_WIDTH - 8))
  const top = rect.bottom + 6
  const unit = spec.unit === 'count' ? '' : spec.unit === 'pp' ? 'pp' : '%'

  return (
    <div
      ref={rootRef}
      className={styles['filter-popover']}
      style={{ left, top }}
      role="dialog"
      aria-label={`${spec.filterLabel} 筛选`}
      data-testid={`filter-popover-${column}`}
    >
      <div className={styles['filter-pop-title']}>筛选：{spec.filterLabel}</div>

      <div className={styles['filter-mode-group']} role="group" aria-label="条件">
        {EXPLORER_FILTER_MODES.map((m) => (
          <button
            key={m.value}
            type="button"
            className={m.value === mode ? styles['filter-mode-active'] : styles['filter-mode']}
            aria-pressed={m.value === mode}
            data-testid={`filter-mode-${m.value}`}
            onClick={() => setMode(m.value)}
          >
            {m.label}
          </button>
        ))}
      </div>

      {(mode === 'range' || mode === 'gte') && (
        <label className={styles['filter-field']}>
          <span>最小值{unit ? `（${unit}）` : ''}</span>
          <input
            className={styles['filter-input']}
            type="text"
            inputMode="decimal"
            value={lower}
            onChange={(e) => setLower(e.target.value)}
            aria-label="最小值"
            data-testid="filter-min"
          />
        </label>
      )}
      {(mode === 'range' || mode === 'lte') && (
        <label className={styles['filter-field']}>
          <span>最大值{unit ? `（${unit}）` : ''}</span>
          <input
            className={styles['filter-input']}
            type="text"
            inputMode="decimal"
            value={upper}
            onChange={(e) => setUpper(e.target.value)}
            aria-label="最大值"
            data-testid="filter-max"
          />
        </label>
      )}
      {mode === 'eq' && (
        <label className={styles['filter-field']}>
          <span>值{unit ? `（${unit}）` : ''}</span>
          <input
            className={styles['filter-input']}
            type="text"
            inputMode="decimal"
            value={lower}
            onChange={(e) => setLower(e.target.value)}
            aria-label="值"
            data-testid="filter-eq"
          />
        </label>
      )}

      <div className={styles['filter-pop-actions']}>
        <button type="button" className={styles['btn-ghost']} onClick={onClear} data-testid="filter-clear">
          清除
        </button>
        <button
          type="button"
          className={styles['btn-primary']}
          onClick={() => onApply(mode, lower, upper)}
          data-testid="filter-apply"
        >
          应用
        </button>
      </div>
    </div>
  )
}
