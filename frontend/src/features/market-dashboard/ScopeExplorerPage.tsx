// [R3C] Industry / Concept Explorer 主页面（共用组件，由 route 以 scopeType 区分）。
//
// 设计要点：
// - URL search params 是全部 applied server state 的 SSOT（parse/serialize 在 scopeExplorerUrlState.ts）。
// - 列表 / 排序 / 筛选 / 分页全部 server-side；前端只渲染 API items，绝不 items.sort/filter/slice。
// - 选中板块写入 URL board_id；详情用 useMarketScopeDetail，独立于列表 state。
// - 详情图表复用 R3A/R3B 的 unified chart 基础（BreadthChart + 6-series），不写第二套。
import { useEffect, useMemo, useState, type Dispatch, type FormEvent, type SetStateAction } from 'react'
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
  clearFiltersPatch,
  EXPLORER_PAGE_SIZE_OPTIONS,
  MA5_PRESETS,
  NUMERIC_FILTER_KEYS,
  parseConceptExplorerSearch,
  parseIndustryExplorerSearch,
  ratioToUi,
  serializeExplorerState,
  uiToRatio,
  updateExplorerState,
  type ExplorerParsed,
  type ExplorerStatePatch,
  type FilterDraft,
  type NumericFilterKey,
} from './scopeExplorerUrlState'
import styles from './dashboard.module.scss'

const DETAIL_DAYS = 250
const LEVELS: readonly HierarchyLevel[] = ['L1', 'L2', 'L3']

function emptyDraft(): FilterDraft {
  return NUMERIC_FILTER_KEYS.reduce(
    (acc, k) => {
      acc[k] = ''
      return acc
    },
    {} as FilterDraft,
  )
}

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

  // ---- 数值筛选（UI 单位 % / pp / 整数 → backend ratio）----
  const [draftFilters, setDraftFilters] = useState<FilterDraft>(() =>
    NUMERIC_FILTER_KEYS.reduce(
      (acc, k) => {
        acc[k] = ratioToUi(k, parsed.filters[k])
        return acc
      },
      {} as FilterDraft,
    ),
  )
  useEffect(() => {
    setDraftFilters(
      NUMERIC_FILTER_KEYS.reduce(
        (acc, k) => {
          acc[k] = ratioToUi(k, parsed.filters[k])
          return acc
        },
        {} as FilterDraft,
      ),
    )
  }, [parsed.filters])

  const applyFilters = () => {
    // 每个 key 都显式写入（含 null），否则 merge 后旧 URL filter 不会被清除。
    const filters: Record<NumericFilterKey, number | null> = {} as Record<NumericFilterKey, number | null>
    for (const k of NUMERIC_FILTER_KEYS) {
      filters[k] = uiToRatio(k, draftFilters[k])
    }
    applyPatch({ filters })
  }
  const resetFilters = () => {
    setDraftFilters(emptyDraft())
    applyPatch(clearFiltersPatch())
  }

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

      {/* Toolbar：搜索 */}
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

      {/* 数值筛选 */}
      <div className={styles['filter-panel']}>
        <div className={styles['filter-row']}>
          <FilterGroup label="MA5" draft={draftFilters} setDraft={setDraftFilters} minKey="ma5_min" maxKey="ma5_max" />
          <FilterGroup label="MA10" draft={draftFilters} setDraft={setDraftFilters} minKey="ma10_min" maxKey="ma10_max" />
          <FilterGroup label="MA5 5日Δ" draft={draftFilters} setDraft={setDraftFilters} minKey="ma5_delta_min" maxKey="ma5_delta_max" />
          <FilterGroup label="MA10 5日Δ" draft={draftFilters} setDraft={setDraftFilters} minKey="ma10_delta_min" maxKey="ma10_delta_max" />
          <FilterGroup label="成员数" draft={draftFilters} setDraft={setDraftFilters} minKey="member_count_min" maxKey="member_count_max" />
        </div>
        <div className={styles['filter-actions']}>
          {MA5_PRESETS.map((p) => (
            <button key={p.key} type="button" className={styles['preset-btn']} onClick={() => applyPatch({ filters: p.filters })}>
              {p.label}
            </button>
          ))}
          <button type="button" className={styles['btn-ghost']} onClick={applyFilters}>
            应用筛选
          </button>
          <button type="button" className={styles['btn-ghost']} onClick={resetFilters}>
            重置
          </button>
        </div>
      </div>

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

// 单个筛选分组（min / max 输入，UI 单位：breadth/delta → %，member_count → 整数）。
function FilterGroup({
  label,
  draft,
  setDraft,
  minKey,
  maxKey,
}: {
  label: string
  draft: FilterDraft
  setDraft: Dispatch<SetStateAction<FilterDraft>>
  minKey: NumericFilterKey
  maxKey: NumericFilterKey
}) {
  const onChange = (k: NumericFilterKey, v: string) => setDraft((d) => ({ ...d, [k]: v }))
  return (
    <div className={styles['filter-group']}>
      <span className={styles['filter-label']}>{label}</span>
      <div className={styles['filter-input-pair']}>
        <input
          className={styles['filter-input']}
          type="text"
          inputMode="decimal"
          value={draft[minKey]}
          placeholder="min"
          onChange={(e) => onChange(minKey, e.target.value)}
          aria-label={`${label} 下限`}
        />
        <span className={styles['filter-sep']}>~</span>
        <input
          className={styles['filter-input']}
          type="text"
          inputMode="decimal"
          value={draft[maxKey]}
          placeholder="max"
          onChange={(e) => onChange(maxKey, e.target.value)}
          aria-label={`${label} 上限`}
        />
      </div>
    </div>
  )
}
