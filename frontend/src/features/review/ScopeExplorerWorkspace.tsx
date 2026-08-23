// [ScopeExplorerWorkspace] - 描述: canonical Scope Explorer 工作区（Slice D）
// 布局：工具栏 + （Table | Trajectory）主区 + 右侧已选 Scope 摘要面板。
// 数据流：paginated backend transport → 完整 family snapshot →
//         ViewModel（q → phase → readiness → velocity_desc → UI 分页）。
// 绝不在 Slice D 调用 getReviewScopeDetail。
import { useMemo } from 'react'
import type { ReviewScopeFamily } from './types'
import type { ReviewUrlState, ReviewExplorerView } from './urlState'
import { extractReviewError } from './api'
import { useReviewScopeFamilySnapshot } from './useReviewScopeFamilySnapshot'
import {
  applyScopeExplorerPipeline,
  buildScopeExplorerQuery,
  findScopeById,
  filterScopes,
  sortVelocityDesc,
} from './scopeExplorerViewModel'
import ScopeExplorerToolbar from './ScopeExplorerToolbar'
import ScopeExplorerTable from './ScopeExplorerTable'
import ScopeTrajectoryView from './ScopeTrajectoryView'
import { ScopeSelectedSummary } from './ScopeSelectedSummary'
import styles from './review.module.scss'

export interface ScopeExplorerWorkspaceProps {
  tradeDate: string
  urlState: ReviewUrlState
  onFamilyChange: (family: ReviewScopeFamily) => void
  onFilterChange: (patch: Partial<ReviewUrlState>) => void
  onViewChange: (view: ReviewExplorerView) => void
  onSelectScope: (scopeKey: string) => void
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
  onViewChange,
  onSelectScope,
}: ScopeExplorerWorkspaceProps) {
  const snapshotQuery = useReviewScopeFamilySnapshot(tradeDate, urlState.family)

  const snapshotItems = useMemo(() => snapshotQuery.data?.items ?? [], [snapshotQuery.data])

  const query = buildScopeExplorerQuery(urlState.q, urlState.phase, urlState.readiness)

  const filteredTotal = useMemo(() => filterScopes(snapshotItems, query).length, [snapshotItems, query])

  const filteredSorted = useMemo(
    () => sortVelocityDesc(filterScopes(snapshotItems, query)),
    [snapshotItems, query],
  )

  const paged = useMemo(
    () => applyScopeExplorerPipeline(snapshotItems, query, urlState.page, urlState.pageSize),
    [snapshotItems, query, urlState.page, urlState.pageSize],
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
        readiness={urlState.readiness}
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
                onSelectScope={onSelectScope}
              />
              <div className={styles.explorerFooter}>
                <span className={styles.explorerCount}>
                  {filteredTotal} matched / {familyTotal} total
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
                    disabled={urlState.page <= 1}
                    onClick={() => onFilterChange({ page: urlState.page - 1 })}
                  >
                    ‹
                  </button>
                  <span>
                    {paged.pageCount === 0 ? 0 : Math.min(urlState.page, paged.pageCount)} / {paged.pageCount}
                  </span>
                  <button
                    type="button"
                    className={styles.btn}
                    disabled={urlState.page >= paged.pageCount}
                    onClick={() => onFilterChange({ page: urlState.page + 1 })}
                  >
                    ›
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
        <ScopeSelectedSummary scope={selectedScope} family={urlState.family} />
      </div>
    </div>
  )
}
