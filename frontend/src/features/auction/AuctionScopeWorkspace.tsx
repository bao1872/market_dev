// [AuctionScopeWorkspace] - 描述: V3.2 List-first Workspace（工具条 + 左列表 + 右 Detail）
//
// URL 单一事实源：trade_date / family / scope / sort / direction / search / preset / page
// 前端只承载展示 + 本地 filter/sort/paginate，不重算业务指标。
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  useAuctionScopes,
  useAuctionScopeDetail,
  useAuctionScopeDates,
} from './api'
import {
  AUCTION_PRESETS,
  buildAuctionScopeView,
  toScopeRows,
  type AuctionScopeSortField,
} from './auctionScopeViewModel'
import {
  parseAuctionUrlState,
  serializeAuctionUrlState,
  type AuctionFamily,
} from './auctionUrlState'
import { AuctionScopeTable } from './AuctionScopeTable'
import { AuctionScopeDetail } from './AuctionScopeDetail'
import styles from './auction.module.scss'

export function AuctionScopeWorkspace() {
  const [searchParams, setSearchParams] = useSearchParams()
  const state = useMemo(
    () => parseAuctionUrlState(searchParams),
    [searchParams],
  )

  const { data: list, isLoading, isError, error } = useAuctionScopes(
    state.family,
    state.tradeDate,
  )
  const { data: dates } = useAuctionScopeDates()
  const { data: detail, isLoading: detailLoading } = useAuctionScopeDetail(
    state.family,
    state.scope,
    state.tradeDate,
  )

  const rows = useMemo(() => toScopeRows(list), [list])
  const view = useMemo(
    () =>
      buildAuctionScopeView(rows, {
        search: state.search,
        presetId: state.preset,
        sort: state.sort,
        direction: state.direction,
        page: state.page,
      }),
    [rows, state.search, state.preset, state.sort, state.direction, state.page],
  )

  function patch(next: Partial<typeof state>) {
    const merged = { ...state, ...next }
    setSearchParams(serializeAuctionUrlState(merged))
  }

  function handleSort(field: AuctionScopeSortField) {
    const direction =
      state.sort === field && state.direction === 'desc' ? 'asc' : 'desc'
    patch({ sort: field, direction, page: 1 })
  }

  function handleSelect(scopeKey: string) {
    patch({ scope: scopeKey, page: 1 })
  }

  function handleFamily(family: AuctionFamily) {
    patch({ family, scope: undefined, page: 1 })
  }

  function handleSearch(value: string) {
    patch({ search: value, page: 1 })
  }

  function handlePreset(presetId: string) {
    patch({ preset: presetId, page: 1 })
  }

  function handleTradeDate(value: string) {
    patch({ tradeDate: value || undefined, scope: undefined, page: 1 })
  }

  function handlePage(delta: number) {
    patch({ page: Math.min(Math.max(1, state.page + delta), view.pageCount) })
  }

  return (
    <div className={styles.workspace}>
      <div className={styles.toolbar}>
        <label className={styles.toolbarLabel}>
          交易日
          <select
            className={styles.select}
            value={state.tradeDate ?? ''}
            onChange={(e) => handleTradeDate(e.target.value)}
          >
            <option value="">当日（默认）</option>
            {(dates?.trade_dates ?? []).map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>

        <div className={styles.segmented}>
          <button
            type="button"
            className={state.family === 'industry' ? styles.segActive : styles.segBtn}
            onClick={() => handleFamily('industry')}
          >
            行业
          </button>
          <button
            type="button"
            className={state.family === 'concept' ? styles.segActive : styles.segBtn}
            onClick={() => handleFamily('concept')}
          >
            概念
          </button>
        </div>

        <input
          className={styles.searchInput}
          placeholder="搜索板块…"
          value={state.search}
          onChange={(e) => handleSearch(e.target.value)}
        />

        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>schema</span>
          <span className={styles.metaValue}>{list?.schema_version ?? '—'}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>覆盖</span>
          <span className={styles.metaValue}>
            {list ? `${view.total}/${list.total_scopes}` : '—'}
          </span>
        </span>
      </div>

      <div className={styles.presetBar}>
        {AUCTION_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            className={state.preset === p.id ? styles.presetActive : styles.presetChip}
            onClick={() => handlePreset(p.id)}
            title={p.description}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className={styles.workspaceBody}>
        <div className={styles.listPane}>
          {isLoading && <div className={styles.stateBox}>加载板块列表…</div>}
          {isError && (
            <div className={styles.stateBox}>
              <span className={styles.stateTitle}>无法加载板块列表</span>
              <span className={styles.stateDesc}>
                {(error as Error)?.message ?? '未知错误'}
              </span>
            </div>
          )}
          {!isLoading && !isError && (
            <>
              <AuctionScopeTable
                rows={view.rows}
                sort={state.sort ?? 'equalWeightGap'}
                direction={state.direction}
                selectedKey={state.scope}
                onSort={handleSort}
                onSelect={(row) => handleSelect(row.scopeKey)}
              />
              <div className={styles.pager}>
                <button
                  type="button"
                  className={styles.btn}
                  onClick={() => handlePage(-1)}
                  disabled={state.page <= 1}
                >
                  上一页
                </button>
                <span className={styles.pagerInfo}>
                  第 {view.page} / {view.pageCount} 页 · 共 {view.total} 板块
                </span>
                <button
                  type="button"
                  className={styles.btn}
                  onClick={() => handlePage(1)}
                  disabled={state.page >= view.pageCount}
                >
                  下一页
                </button>
              </div>
            </>
          )}
        </div>

        <div className={styles.detailPane}>
          <AuctionScopeDetail
            detail={detail}
            loading={detailLoading && !!state.scope}
            error={
              state.scope && !detail && !detailLoading
                ? '未找到该板块的已发布结果'
                : null
            }
          />
        </div>
      </div>
    </div>
  )
}

export default AuctionScopeWorkspace
