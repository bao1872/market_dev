// [R3C] Industry / Concept Explorer 主页面（共用组件，由 route 以 scopeType 区分）。
//
// 设计要点：
// - URL search params 是全部 applied server state 的 SSOT（parse/serialize 在 scopeExplorerUrlState.ts）。
// - 列表 / 排序 / 筛选 / 分页全部 server-side；前端只渲染 API items，绝不 items.sort/filter/slice。
// - 选中板块写入 URL board_id；详情用 useMarketScopeDetail，独立于列表 state。
// - 详情图表复用 R3A/R3B 的 unified chart 基础（BreadthChart + 6-series），不写第二套。
//
// [PANJI-REVIEW-UI-UNIFY] 列表 / 详情统一工作区：
// - 进入 行业 / 概念 先渲染**完整 LIST VIEW**（列表即主工作区，不复用常驻主从分栏）。
// - 点击某一行 → 进入 **DETAIL VIEW**：主工作区切换为「左导航栏（仅名称）+ 右侧真实详情」，
//   列表不再占据主页面。返回（清除 board_id）精确恢复列表的 search/filters/sort/page/family/date。
// - 左导航栏（rail）只显示板块名称，来自与列表**同一份** server 过滤+排序结果（前端分页拉全）。
// - 右详情复用现有 canonical：useMarketScopeDetail + BreadthChart（6-series）。不新建详情实现 / API。
//
// [PANJI-REVIEW-UI-RUNTIME-PARITY-FIX] 运行期 UI 对齐行情工作台：
// - §2 顶部改为共享紧凑 ReviewTopBar（tabs + 数据日期同一行，**无页面级 H1**）。
// - §3 tab 专属控件行紧跟在顶部行下方（行业 L1/L2/L3 + 搜索；概念 搜索）。
// - §4 删除「快速筛选」：复盘不再发明第二套筛选系统，筛选只走列 funnel。
// - §5 表格 chrome 交给 global.scss（与 StrategyDataTable 同 owner），module 不再复制一份。
// - §7 新增「列设置」（列显隐，本地偏好，不共享 Market 命名空间；列顺序仍由契约锁死）。
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMarketScopeExplorer, useMarketScopeDetail, useMarketScopeExplorerUniverse } from '@/hooks/useMarketDashboardApi'
import { useCompareBasketStore } from '@/store/compareBasket'
import { extractMarketDashboardError } from '@/api/marketDashboard'
import BreadthChart, { type BreadthLineSpec } from './BreadthChart'
import type { BreadthPoint, HierarchyLevel, ScopeType, ScopeExplorerItem } from './types'
import {
  activeExplorerFilterChips,
  clearAllStatePatch,
  draftFromFilterMode,
  EXPLORER_FILTERABLE_COLUMNS,
  EXPLORER_FILTER_COLUMN_SPECS,
  EXPLORER_FILTER_MODES,
  EXPLORER_PAGE_SIZE_OPTIONS,
  EXPLORER_TABLE_COLUMNS,
  filterKeysForColumn,
  isExplorerColumnFiltered,
  isExplorerResetDisabled,
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
import { type ScopeExplorerQuery } from './scopeExplorerQuery'
import { MARKET_OVERVIEW_SERIES } from './marketOverviewConfig'
import { BREADTH_REFERENCE_LINES } from './chartTheme'
import { classifyDashboardError } from './dashboardLogic'
import DashboardState from './DashboardState'
import ScopeExplorerTable, { EXPLORER_COLUMN_LABELS } from './ScopeExplorerTable'
import ReviewTopBar from './ReviewTopBar'
import {
  readHiddenColumns,
  toggleHiddenColumn,
  writeHiddenColumns,
  type ExplorerTableColumn,
} from './reviewTablePrefs'
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

  // 列表主查询（server-side 分页；page/page_size 来自 URL SSOT）。
  const explorer = useMarketScopeExplorer(query)
  // 左导航栏「筛选排序 universe 全集」：与列表同一份 server 过滤+排序结果，前端分页拉全（不前端重排）。
  // query identity 不含 page / page_size / board_id → 切页、切选板块都不触发 rail 重取。
  const railUniverseQuery = useMemo<ScopeExplorerQuery>(
    () => ({
      scope_type: query.scope_type,
      hierarchy_level: isIndustry ? parsed.hierarchy_level : null,
      q: parsed.q || null,
      sort: parsed.sort,
      direction: parsed.direction,
      ...parsed.filters,
    }),
    [query.scope_type, isIndustry, parsed.hierarchy_level, parsed.q, parsed.sort, parsed.direction, parsed.filters],
  )
  const railUniverse = useMarketScopeExplorerUniverse(railUniverseQuery, !!parsed.board_id)
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

  const clearDisabled = isExplorerResetDisabled(parsed.filters, parsed.sort, parsed.direction)
  const clearAllFilters = () => {
    applyPatch(clearAllStatePatch())
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

  // ---- 列设置（§7）：列显隐是 Review 自有本地偏好；筛选/排序/分页仍 100% 在 URL ----
  const tableId = `review-${scopeType}`
  const [hiddenColumns, setHiddenColumns] = useState<ReadonlySet<ExplorerTableColumn>>(() =>
    readHiddenColumns(tableId),
  )
  useEffect(() => setHiddenColumns(readHiddenColumns(tableId)), [tableId])
  const [columnSettingsAnchor, setColumnSettingsAnchor] = useState<HTMLElement | null>(null)
  const toggleColumn = (key: ExplorerTableColumn) => {
    const next = toggleHiddenColumn(hiddenColumns, key)
    setHiddenColumns(next)
    writeHiddenColumns(tableId, next)
  }
  const resetColumns = () => {
    const empty = new Set<ExplorerTableColumn>()
    setHiddenColumns(empty)
    writeHiddenColumns(tableId, empty)
  }

  // ---- 排序（点击 active 列切换方向；新列 numeric→desc / name→asc）----
  const onSort = (field: ExplorerParsed['sort']) => {
    if (parsed.sort === field) {
      applyPatch({ direction: parsed.direction === 'asc' ? 'desc' : 'asc' })
    } else {
      applyPatch({ sort: field, direction: field === 'name' ? 'asc' : 'desc' })
    }
  }

  // ---- 行选中（写入 board_id，精确保留列表 page）----
  const onSelect = (id: string) => applyPatch({ board_id: id, page: parsed.page })

  // ---- 左导航栏选中（仅改变 board_id，不重置列表 query state）----
  const onRailSelect = (id: string) => applyPatch({ board_id: id, page: parsed.page })

  // ---- 返回列表（清除 board_id，精确保留列表 page）----
  const onBackToList = () => applyPatch({ board_id: null, page: parsed.page })

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

  // =========================================================================
  // 列表工作区（仅在未选中板块时渲染；选中板块 → 详情视图，列表不再占据主页面）
  // =========================================================================
  const listWorkspace = !parsed.board_id && (
    <>
      {/* 列设置弹层（§7）：只改列显隐；列顺序由契约锁死，不提供重排。 */}
      {columnSettingsAnchor && (
        <ExplorerColumnSettings
          hidden={hiddenColumns}
          onToggle={toggleColumn}
          onReset={resetColumns}
          onClose={() => setColumnSettingsAnchor(null)}
          anchor={columnSettingsAnchor}
        />
      )}

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
            hiddenColumns={hiddenColumns}
          />

          <div className="table-pager">
            <span className="table-page-info">
              第 {parsed.page} / {totalPages} 页 · 共 {total} 项
            </span>
            <label>
              每页{' '}
              <select
                className="select table-page-size"
                value={parsed.page_size}
                onChange={(e) => changePageSize(Number(e.target.value))}
                aria-label="每页条数"
              >
                {EXPLORER_PAGE_SIZE_OPTIONS.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="btn small table-prev"
              onClick={() => goPage(parsed.page - 1)}
              disabled={parsed.page <= 1}
            >
              上一页
            </button>
            <button
              type="button"
              className="btn small table-next"
              onClick={() => goPage(parsed.page + 1)}
              disabled={parsed.page >= totalPages}
            >
              下一页
            </button>
          </div>
        </>
      )}
    </>
  )

  // =========================================================================
  // 详情视图（选中板块 → 主工作区切换为：左导航栏「仅名称」+ 右侧真实详情）
  // =========================================================================
  const detailWorkspace = parsed.board_id && (
    <div className={styles.detailLayout} data-testid="scope-detail-view">
      {/* 左导航栏：仅板块名称，来自筛选排序 universe 全集（server 同过滤+排序，前端分页拉全）。 */}
      <nav className={styles.scopeRail} data-testid="scope-rail" aria-label="板块导航">
        {railUniverse.isLoading ? (
          <div className={styles.railLoading}>加载导航…</div>
        ) : railUniverse.error ? (
          <div className={styles.railEmpty}>导航加载失败</div>
        ) : (railUniverse.data ?? []).length === 0 ? (
          <div className={styles.railEmpty}>无匹配板块</div>
        ) : (
          (railUniverse.data ?? []).map((it: ScopeExplorerItem) => {
            const active = it.board_id === parsed.board_id
            return (
              <button
                key={it.board_id}
                type="button"
                className={active ? `${styles.railItem} ${styles.railItemActive}` : styles.railItem}
                aria-current={active ? 'true' : undefined}
                data-board-id={it.board_id}
                data-testid={`rail-item-${it.board_id}`}
                onClick={() => onRailSelect(it.board_id)}
              >
                {it.board_name}
              </button>
            )
          })
        )}
      </nav>

      {/* 右侧真实详情（复用 canonical useMarketScopeDetail + BreadthChart；不新建详情实现 / API） */}
      <div className={styles.detailMain} data-testid="scope-detail">
        <button type="button" className={styles.detailBack} onClick={onBackToList} data-testid="detail-back">
          ← 返回列表
        </button>
        {detail.isLoading ? (
          <DashboardState kind="loading" />
        ) : detail.error && detailError ? (
          <DashboardState kind={detailError.kind} desc={detailError.detail} />
        ) : meta ? (
          <>
            <div className={styles.detailHead}>
              <div>
                <div className={styles.detailName}>{meta.name}</div>
                {/* 数据日期已由共享顶部行统一展示，此处不再重复。 */}
                <div className={styles.detailSub}>
                  {meta.type === 'industry' ? `行业 · ${meta.hierarchy_level}` : '概念'} · 成员数{' '}
                  {meta.member_count.toLocaleString()}
                </div>
              </div>
              <button
                type="button"
                className={styles.btnPrimary}
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

            {basketMsg && <div className={styles.compareMsg}>{basketMsg}</div>}
            <div className={styles.compareLinkRow}>
              {/* SPA 导航：保留 Zustand compare basket（R3A 仅 session persistence，不 localStorage）。整页 reload 会清空 basket。 */}
              <Link className={styles.summaryLink} to="/review/compare">
                查看对比
              </Link>
            </div>
          </>
        ) : null}
      </div>
    </div>
  )

  // =========================================================================
  // [PANJI-REVIEW-EXPLORER-CONTROL-DECK-01] 注入 ReviewTopBar 的 tab 专属控件
  // 只搬运既有控件（层级 / 搜索）：不新增第二套 tabs，不改任何 URL / 搜索语义。
  // =========================================================================
  const explorerControls = (
    <>
      <span className={styles.deckDivider} aria-hidden="true" />

      {isIndustry ? (
        <div className={styles.deckLevel}>
          <span className={styles.controlLabel}>层级</span>
          <div className={styles.levelSelector} role="group" aria-label="行业层级">
            {LEVELS.map((lv) => (
              <button
                key={lv}
                type="button"
                className={parsed.hierarchy_level === lv ? styles.levelBtnActive : styles.levelBtn}
                onClick={() => onLevel(lv)}
                aria-pressed={parsed.hierarchy_level === lv}
              >
                {lv}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <p className={styles.conceptNote}>同花顺概念 / 问财概念</p>
      )}

      <form className={styles.searchForm} onSubmit={submitSearch}>
        <input
          className={styles.searchInput}
          type="text"
          value={draftQ}
          placeholder={isIndustry ? '搜索行业，例如：银行 / 汽车 / 医药' : '搜索概念'}
          onChange={(e) => setDraftQ(e.target.value)}
          data-testid="search-input"
        />
        <button type="submit" className={styles.btnPrimary}>
          搜索
        </button>
        {parsed.q && (
          <button type="button" className={styles.btnGhost} onClick={clearSearch} data-testid="clear-search">
            清空
          </button>
        )}
      </form>
    </>
  )

  return (
    <div className={styles.explorerPage} data-testid="explorer-page">
      {/* ===== Control Deck：两层紧凑控制区（tabs/层级/搜索/日期 + 结果/排序/chips/列设置/清除）===== */}
      <div className={styles.explorerControlDeck} data-testid="explorer-control-deck">
        <ReviewTopBar
          projectionDate={explorer.data?.projection_trade_date}
          variant="deck"
          controls={explorerControls}
        />

        {/* 第二层：列表态专属（结果 / 排序 / chips / 列设置 / 清除）；detail view 不渲染（已不是列表表格）。 */}
        {!parsed.board_id && (
          <div className={`table-meta-bar ${styles.explorerMetaRow}`} data-testid="explorer-meta-bar">
            <div className={styles.explorerMetaState}>
              <span className="table-result-count">结果 {total}</span>
              <span className="table-active-state">
                按 {EXPLORER_COLUMN_LABELS[parsed.sort]} {parsed.direction === 'asc' ? '升序' : '降序'} 排序
              </span>
              <span className="table-filter-chips" data-testid="explorer-filter-chips">
                {filterChips.map((chip) => (
                  <button
                    key={chip.column}
                    type="button"
                    className="filter-chip"
                    title={`清除「${chip.label}」`}
                    aria-label={`清除筛选 ${chip.label}`}
                    onClick={() => clearColumnFilter(chip.column)}
                  >
                    <span className="filter-chip-text">{chip.label}</span>
                    <span className="filter-chip-x" aria-hidden="true">
                      ×
                    </span>
                  </button>
                ))}
              </span>
            </div>
            <div className="table-meta-actions">
              <button
                type="button"
                className="table-columns-btn"
                onClick={(e) => setColumnSettingsAnchor(e.currentTarget)}
                data-testid="column-settings-btn"
              >
                列设置
              </button>
              <button
                type="button"
                className="table-reset-btn"
                disabled={clearDisabled}
                onClick={clearAllFilters}
                data-testid="clear-all-filters"
              >
                清除排序与筛选
              </button>
            </div>
          </div>
        )}
      </div>

      {listWorkspace}
      {detailWorkspace}
    </div>
  )
}

// 列设置弹层（§7）：复用 global.scss 的 column-manager-* / column-filter-popover chrome。
// 只提供列显隐（列顺序由 EXPLORER_TABLE_COLUMNS 契约锁死，故不提供重排）。
function ExplorerColumnSettings({
  hidden,
  onToggle,
  onReset,
  onClose,
  anchor,
}: {
  hidden: ReadonlySet<ExplorerTableColumn>
  onToggle: (key: ExplorerTableColumn) => void
  onReset: () => void
  onClose: () => void
  anchor: HTMLElement
}) {
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

  return (
    <div
      ref={rootRef}
      className="column-filter-popover column-manager-popover"
      style={{ left, top }}
      role="dialog"
      aria-label="列设置"
      data-testid="column-settings-popover"
    >
      <div className="filter-pop-title">显示列</div>
      <div className="column-manager-list">
        {EXPLORER_TABLE_COLUMNS.map((key) => {
          // name（身份列）/ operation（操作列）不可隐藏。
          if (key === 'name' || key === 'operation') return null
          return (
            <div key={key} className="column-manager-item">
              <label className="table-checkbox-wrapper" style={{ width: 24, height: 24 }}>
                <input
                  type="checkbox"
                  className="table-checkbox"
                  checked={!hidden.has(key)}
                  onChange={() => onToggle(key)}
                  data-testid={`column-toggle-${key}`}
                />
              </label>
              <span className="column-manager-label">{EXPLORER_COLUMN_LABELS[key]}</span>
            </div>
          )
        })}
      </div>
      <div className="filter-pop-actions">
        <button type="button" className="btn small" onClick={onReset} data-testid="column-settings-reset">
          恢复默认
        </button>
        <button type="button" className="btn small primary" onClick={onClose} data-testid="column-settings-done">
          完成
        </button>
      </div>
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
      className="column-filter-popover"
      style={{ left, top }}
      role="dialog"
      aria-label={`${spec.filterLabel} 筛选`}
      data-testid={`filter-popover-${column}`}
    >
      <div className="filter-pop-title">筛选：{spec.filterLabel}</div>

      <div className={styles.filterModeGroup} role="group" aria-label="条件">
        {EXPLORER_FILTER_MODES.map((m) => (
          <button
            key={m.value}
            type="button"
            className={m.value === mode ? styles.filterModeActive : styles.filterMode}
            aria-pressed={m.value === mode}
            data-testid={`filter-mode-${m.value}`}
            onClick={() => setMode(m.value)}
          >
            {m.label}
          </button>
        ))}
      </div>

      {(mode === 'range' || mode === 'gte') && (
        <label className={styles.filterField}>
          <span>最小值{unit ? `（${unit}）` : ''}</span>
          <input
            className={styles.filterInput}
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
        <label className={styles.filterField}>
          <span>最大值{unit ? `（${unit}）` : ''}</span>
          <input
            className={styles.filterInput}
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
        <label className={styles.filterField}>
          <span>值{unit ? `（${unit}）` : ''}</span>
          <input
            className={styles.filterInput}
            type="text"
            inputMode="decimal"
            value={lower}
            onChange={(e) => setLower(e.target.value)}
            aria-label="值"
            data-testid="filter-eq"
          />
        </label>
      )}

      <div className="filter-pop-actions">
        <button type="button" className={styles.btnGhost} onClick={onClear} data-testid="filter-clear">
          清除
        </button>
        <button
          type="button"
          className={styles.btnPrimary}
          onClick={() => onApply(mode, lower, upper)}
          data-testid="filter-apply"
        >
          应用
        </button>
      </div>
    </div>
  )
}
