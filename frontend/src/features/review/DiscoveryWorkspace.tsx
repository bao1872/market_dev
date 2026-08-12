/** [V2] Discovery Workspace — Discovery-first market structure workbench.
 *
 * URL 是 SSOT（由 ReviewPage 拥有 canonical /review URL state）：
 * - tradeDate 来自正式 Review URL state（?date=），不新建第二套 trade date state
 * - discoveryId 存在即 detail 模式，否则 overview/list（无独立 local viewMode）
 * - scopeType / scopeFamily / status 作为 filter 保留在 URL
 *
 * Detail 消费真实 Detail API（GET /v1/review/discoveries/{id}），
 * 不依赖当前 page 的 paginated list 查找（deep link 独立于分页）。
 *
 * 状态明确区分：loading / empty / API error（error != empty）。
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getDiscoveries, getDiscoveryDetail, extractReviewError } from './api'
import { reviewKeys } from './queryKeys'
import type { ReviewOverview } from './types'
import { DiscoveryCard } from './DiscoveryCard'
import { DiscoveryDetail } from './DiscoveryDetail'
import styles from './review.module.scss'

export interface DiscoveryFilterPatch {
  scopeType?: string | null
  scopeFamily?: string | null
  status?: string | null
}

interface DiscoveryWorkspaceProps {
  /** 正式 Review URL state 的交易日 */
  tradeDate: string
  /** URL discoveryId（存在即 detail） */
  discoveryId: string | null
  scopeType: string | null
  scopeFamily: string | null
  status: string | null
  /** 当日总览（status 展示用） */
  overview: ReviewOverview | undefined
  onDiscoveryOpen: (id: string) => void
  onDiscoveryClose: () => void
  onFilterChange: (patch: DiscoveryFilterPatch) => void
  showToast: (title: string, desc?: string) => void
}

export function DiscoveryWorkspace({
  tradeDate,
  discoveryId,
  scopeType,
  scopeFamily,
  status,
  overview,
  onDiscoveryOpen,
  onDiscoveryClose,
  onFilterChange,
  showToast,
}: DiscoveryWorkspaceProps) {
  const [page, setPage] = useState(1)
  const pageSize = 20

  // URL discoveryId presence → detail mode（无独立 viewMode SSOT）
  const isDetail = !!discoveryId

  // Overview list（detail 时不请求，避免无谓分页加载）
  const listQuery = useQuery({
    queryKey: reviewKeys.discoveries(tradeDate, { scopeType, scopeFamily, status, page, pageSize }),
    queryFn: () => getDiscoveries(tradeDate, {
      scope_type: scopeType || undefined,
      scope_family: scopeFamily || undefined,
      status: status || undefined,
      sort: 'rank',
      page,
      page_size: pageSize,
    }),
    enabled: !!tradeDate && !isDetail,
  })

  // Detail via real Detail API（deep link 独立于分页）
  const detailQuery = useQuery({
    queryKey: reviewKeys.discovery(discoveryId ?? '', tradeDate),
    queryFn: () => getDiscoveryDetail(discoveryId as string, tradeDate),
    enabled: isDetail,
  })

  const discoveries = listQuery.data?.items || []
  const total = listQuery.data?.total || 0

  // [P1-A] Scope FAMILY chips represent the 5 semantic families, NOT raw scope
  // types. Wire values are scope_type prefixes accepted by the backend
  // (review.py: scope_type.startswith(scope_family)). They are derived from the
  // canonical full-scope taxonomy — independent of the current paginated
  // Discovery items, so pagination never makes a valid family chip disappear.
  const SCOPE_FAMILIES: { value: string; label: string }[] = useMemo(
    () => [
      { value: 'market', label: '市场' }, // MARKET
      { value: 'major_index', label: '指数' }, // INDEX
      { value: 'style', label: '风格' }, // STYLE
      { value: 'industry', label: '行业' }, // INDUSTRY (covers all industry levels via prefix)
      { value: 'concept', label: '概念' }, // CONCEPT
    ],
    [],
  )

  // Precise scope_type selector stays separate (7 raw types), never merged with family.
  const SCOPE_TYPES: { value: string; label: string }[] = useMemo(
    () => [
      { value: 'market', label: '市场' },
      { value: 'major_index', label: '指数' },
      { value: 'style', label: '风格' },
      { value: 'industry_l1', label: '一级行业' },
      { value: 'industry_l2', label: '二级行业' },
      { value: 'industry_l3', label: '三级行业' },
      { value: 'concept', label: '概念' },
    ],
    [],
  )

  const setFilter = (key: 'scopeType' | 'scopeFamily' | 'status', value: string | undefined) => {
    onFilterChange({ [key]: value || null } as DiscoveryFilterPatch)
    setPage(1)
  }

  // ------------------------------------------------------------------
  // Detail branch
  // ------------------------------------------------------------------
  if (isDetail) {
    if (detailQuery.isLoading) {
      return <div className={styles.reviewLoading}>加载 Discovery 详情...</div>
    }
    if (detailQuery.isError) {
      const err = extractReviewError(detailQuery.error)
      return (
        <div className={styles.discoveryError}>
          <div>Discovery 详情加载失败</div>
          <div>{err.message}{err.requestId ? `（request_id=${err.requestId}）` : ''}</div>
        </div>
      )
    }
    const detail = detailQuery.data?.discovery
    if (!detail) {
      return <div className={styles.discoveryError}>Discovery 详情为空</div>
    }
    return (
      <DiscoveryDetail
        discovery={detail}
        onBack={onDiscoveryClose}
        tradeDate={tradeDate}
        showToast={showToast}
      />
    )
  }

  // ------------------------------------------------------------------
  // Overview / List branch
  // ------------------------------------------------------------------
  if (listQuery.isLoading) {
    return <div className={styles.reviewLoading}>加载市场发现...</div>
  }

  if (listQuery.isError) {
    // API error ≠ empty：不能显示成「今日无市场发现」
    const err = extractReviewError(listQuery.error)
    return (
      <div className={styles.discoveryError}>
        <div>市场发现加载失败</div>
        <div>{err.message}{err.requestId ? `（request_id=${err.requestId}）` : ''}</div>
      </div>
    )
  }

  return (
    <div className={styles.discoveryWorkspace}>
      {/* Header */}
      <div className={styles.discoveryHeader}>
        <h2>市场发现</h2>
        <span className={styles.discoveryTradeDate}>{tradeDate}</span>
        {overview?.status && (
          <span className={`${styles.discoveryStatus} ${overview.status}`}>
            {overview.status === 'published' ? '已发布' : overview.status}
          </span>
        )}
      </div>

      {/* Scope FAMILY filters (5 semantic families; wire value = scope_type prefix) */}
      <div className={styles.discoveryFilters}>
        <button
          type="button"
          className={!scopeFamily ? styles.discoveryFilterActive : ''}
          onClick={() => setFilter('scopeFamily', undefined)}
        >
          全部 ({total})
        </button>
        {SCOPE_FAMILIES.map(f => (
          <button
            type="button"
            key={f.value}
            className={scopeFamily === f.value ? styles.discoveryFilterActive : ''}
            onClick={() => setFilter('scopeFamily', f.value)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Precise scope_type selector — kept separate from family (never merged) */}
      <div className={styles.discoveryFilters}>
        <span className={styles.discoveryFilterHint}>范围类型：</span>
        <button
          type="button"
          className={!scopeType ? styles.discoveryFilterActive : ''}
          onClick={() => setFilter('scopeType', undefined)}
        >
          全部
        </button>
        {SCOPE_TYPES.map(t => (
          <button
            type="button"
            key={t.value}
            className={scopeType === t.value ? styles.discoveryFilterActive : ''}
            onClick={() => setFilter('scopeType', t.value)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Empty state（仅当 API 成功且 0 条） */}
      {discoveries.length === 0 && (
        <div className={styles.reviewEmptyState}>
          <p>今日无满足当前 Discovery 条件的市场发现</p>
          {overview?.degradedReasons && overview.degradedReasons.length > 0 && (
            <div className={styles.reviewDegraded}>
              {overview.degradedReasons.map((r, i) => (
                <p key={i} className={styles.reviewDegradedItem}>{r}</p>
              ))}
            </div>
          )}
          <p className={styles.reviewEmptyHint}>
            可查看 Signal evidence diagnostics 了解详细信号命中情况
          </p>
        </div>
      )}

      {/* Discovery cards */}
      <div className={styles.discoveryList}>
        {discoveries.map(d => (
          <DiscoveryCard
            key={d.discoveryId}
            discovery={d}
            onClick={() => onDiscoveryOpen(d.discoveryId)}
          />
        ))}
      </div>

      {/* Pagination */}
      {total > pageSize && (
        <div className={styles.discoveryPagination}>
          <button type="button" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</button>
          <span>第 {page} 页 / 共 {Math.ceil(total / pageSize)} 页</span>
          <button type="button" disabled={page * pageSize >= total} onClick={() => setPage(p => p + 1)}>下一页</button>
        </div>
      )}
    </div>
  )
}
