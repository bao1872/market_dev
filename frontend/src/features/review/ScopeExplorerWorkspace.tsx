// [ScopeExplorerWorkspace] - 描述: canonical Scope Explorer 工作区（Slice D + Slice E）
// 布局：工具栏 + （Table | Trajectory）主区 + 右侧 Canonical Scope Detail 工作区。
// 数据流：paginated backend transport → 完整 family snapshot →
//         ViewModel（q → phase → readiness → 选定 sort 降序 → UI 分页）；
//         已选 Scope → 唯一 detail owner（useReviewScopeDetail）驱动右侧详情。
// ScopeExplorerTable / ScopeTrajectoryView / family snapshot 绝不在本 slice 请求 detail（无 N+1）。
import { useMemo } from 'react'
import type { ReviewScopeFamily } from './types'
import type { ReviewUrlState, ReviewExplorerView, ReviewDetailTab } from './urlState'
import { extractReviewError } from './api'
import { useReviewScopeFamilySnapshot } from './useReviewScopeFamilySnapshot'
import {
  applyScopeExplorerPipeline,
  buildScopeExplorerQuery,
  findScopeById,
  filterScopes,
  sortScopes,
  computeEffectivePage,
} from './scopeExplorerViewModel'
import ScopeExplorerToolbar from './ScopeExplorerToolbar'
import ScopeExplorerTable from './ScopeExplorerTable'
import ScopeTrajectoryView from './ScopeTrajectoryView'
import ScopeDetailWorkspace from './ScopeDetailWorkspace'
import styles from './review.module.scss'

export interface ScopeExplorerWorkspaceProps {
  tradeDate: string
  urlState: ReviewUrlState
  onFamilyChange: (family: ReviewScopeFamily) => void
  /** 过滤类变化：q/phase/readiness/pageSize（会重置 page=1） */
  onFilterChange: (patch: Partial<ReviewUrlState>) => void
  /** 翻页专用路径：只改 page，保留全部其他状态 */
  onPageChange: (page: number) => void
  onViewChange: (view: ReviewExplorerView) => void
  onSelectScope: (scopeKey: string) => void
  /** 详情子 Tab 切换：只 patch tab（URL SSOT，不改 detail query identity） */
  onTabChange: (tab: ReviewDetailTab) => void
}

function StateBox({ title, desc }: { title: string; desc: string }) {
  return (
    <div className={styles.stateBox}>
      <div className={styles.stateTitle}>{title}</div>
      <div className={styles.stateDesc}>{desc}</div>
    </div>
  )
}

export default function ScopeExplorerWorkspace({
  tradeDate,
  urlState,
  onFamilyChange,
  onFilterChange,
  onPageChange,
  onViewChange,
  onSelectScope,
  onTabChange,
}: ScopeExplorerWorkspaceProps) {
  const snapshotQuery = useReviewScopeFamilySnapshot(tradeDate, urlState.family)

  const snapshotItems = useMemo(() => snapshotQuery.data?.items ?? [], [snapshotQuery.data])

  const query = buildScopeExplorerQuery(urlState.q, urlState.phase, urlState.readiness)

  const filteredTotal = useMemo(() => filterScopes(snapshotItems, query).length, [snapshotItems, query])

  const filteredSorted = useMemo(
    () => sortScopes(filterScopes(snapshotItems, query), urlState.sort),
    [snapshotItems, query, urlState.sort],
  )

  const paged = useMemo(
    () => applyScopeExplorerPipeline(snapshotItems, query, urlState.page, urlState.pageSize, urlState.sort),
    [snapshotItems, query, urlState.page, urlState.pageSize, urlState.sort],
  )

  // 有效页码：URL 可能越界（?page=999），渲染与交互必须用同一钳制页
  const effectivePage = useMemo(
    () => computeEffectivePage(urlState.page, paged.pageCount),
    [urlState.page, paged.pageCount],
  )

  const selectedScope = useMemo(
    () => findScopeById(snapshotItems, urlState.scopeKey),
    [snapshotItems, urlState.scopeKey],
  )

  if (snapshotQuery.isLoading) {
    return <StateBox title="加载 Scope 列表" desc={`正在获取 ${urlState.family} 全部 Scope...`} />
  }
  if (snapshotQuery.isError) {
    const err = extractReviewError(snapshotQuery.error)
    return (
      <StateBox
        title="Scope 列表加载失败"
        desc={`${err.message}${err.requestId ? `（request_id=${err.requestId}）` : ''}`}
      />
    )
  }
  if (snapshotItems.length === 0) {
    return (
      <StateBox
        title="该 Scope 族暂无数据"
        desc={`${tradeDate} 尚未产出 ${urlState.family} 的 Scope 记录`}
      />
    )
  }

  const familyTotal = snapshotQuery.data?.total ?? snapshotItems.length

  return (
    <div className={styles.explorerWorkspace}>
      <ScopeExplorerToolbar
        family={urlState.family}
        view={urlState.view}
        q={urlState.q}
        phase={urlState.phase}
        onFamilyChange={onFamilyChange}
        onViewChange={onViewChange}
        onFilterChange={onFilterChange}
      />
      <div className={styles.explorerBody}>
        <div className={styles.explorerMain}>
          {urlState.view === 'trajectory' ? (
            <ScopeTrajectoryView
              rows={filteredSorted}
              selectedScopeKey={urlState.scopeKey}
              onSelectScope={onSelectScope}
            />
          ) : (
            <>
              <ScopeExplorerTable
                rows={paged.items}
                selectedScopeKey={urlState.scopeKey}
                sort={urlState.sort}
                onSortChange={(s) => onFilterChange({ sort: s, page: 1 })}
                onSelectScope={onSelectScope}
              />
              <div className={styles.explorerFooter}>
                <span className={styles.explorerCount}>
                  {filteredTotal} 匹配 / 共 {familyTotal} 个板块
                </span>
                <div className={styles.pagination}>
                  <select
                    className={styles.select}
                    value={urlState.pageSize}
                    onChange={(e) => onFilterChange({ pageSize: Number(e.target.value) })}
                    aria-label="每页条数"
                  >
                    {[25, 50, 100].map((n) => (
                      <option key={n} value={n}>
                        {n}/页
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className={styles.btn}
                    disabled={effectivePage <= 1}
                    onClick={() => onPageChange(effectivePage - 1)}
                    aria-label="上一页"
                  >
                    ‹
                  </button>
                  <span>
                    {paged.pageCount === 0 ? 0 : effectivePage} / {paged.pageCount}
                  </span>
                  <button
                    type="button"
                    className={styles.btn}
                    disabled={effectivePage >= paged.pageCount}
                    onClick={() => onPageChange(effectivePage + 1)}
                    aria-label="下一页"
                  >
                    ›
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
        <ScopeDetailWorkspace
          tradeDate={tradeDate}
          selectedScope={selectedScope}
          family={urlState.family}
          tab={urlState.tab}
          onTabChange={onTabChange}
        />
      </div>
    </div>
  )
}
