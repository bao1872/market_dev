/** [V2] Discovery Workspace — Discovery-first market structure workbench. */

import { useState, useMemo } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getDiscoveries, getReviewOverview, getReviewLatest } from './api'
import { reviewKeys } from './queryKeys'
import type { Discovery } from './types'
import { DiscoveryCard } from './DiscoveryCard'
import { DiscoveryDetail } from './DiscoveryDetail'

type ViewMode = 'overview' | 'detail'

export function DiscoveryWorkspace() {
  const { tradeDate: routeTradeDate } = useParams<{ tradeDate?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const discoveryId = searchParams.get('discoveryId') || undefined
  const scopeFilter = searchParams.get('scopeType') || undefined
  const scopeFamily = searchParams.get('scopeFamily') || undefined
  const statusFilter = searchParams.get('status') || undefined
  const [page, setPage] = useState(1)
  const pageSize = 20

  // Resolve trade date
  const { data: latest } = useQuery({
    queryKey: reviewKeys.latest(),
    queryFn: getReviewLatest,
  })
  const tradeDate = routeTradeDate || latest?.trade_date || ''
  const [viewMode, setViewMode] = useState<ViewMode>(discoveryId ? 'detail' : 'overview')

  // Overview
  const { data: overview } = useQuery({
    queryKey: reviewKeys.overview(tradeDate),
    queryFn: () => getReviewOverview(tradeDate),
    enabled: !!tradeDate,
  })

  // Discoveries
  const { data: discoveryData, isLoading } = useQuery({
    queryKey: reviewKeys.discoveries(tradeDate, { scopeFilter, scopeFamily, statusFilter, page, pageSize }),
    queryFn: () => getDiscoveries(tradeDate, {
      scope_type: scopeFilter,
      scope_family: scopeFamily,
      status: statusFilter,
      sort: 'rank',
      page,
      page_size: pageSize,
    }),
    enabled: !!tradeDate,
  })

  const discoveries = discoveryData?.items || []
  const total = discoveryData?.total || 0

  const scopeFamilies = useMemo(() => {
    const families = new Set<string>()
    discoveries.forEach(d => families.add(d.scope.type))
    return ['market', 'major_index', 'style', 'industry_l1', 'industry_l2', 'industry_l3', 'concept']
      .filter(f => families.has(f) || scopeFilter === f)
  }, [discoveries, scopeFilter])

  const selectedDiscovery = useMemo(() => {
    if (!discoveryId || !discoveries.length) return null
    return discoveries.find(d => d.discoveryId === discoveryId) || null
  }, [discoveryId, discoveries])

  const navigateToDetail = (d: Discovery) => {
    setSearchParams(prev => {
      prev.set('discoveryId', d.discoveryId)
      return prev
    })
    setViewMode('detail')
  }

  const navigateBack = () => {
    setSearchParams(prev => {
      prev.delete('discoveryId')
      return prev
    })
    setViewMode('overview')
  }

  const setFilter = (key: string, value: string | undefined) => {
    setSearchParams(prev => {
      if (value) prev.set(key, value)
      else prev.delete(key)
      return prev
    })
    setPage(1)
  }

  // Loading
  if (!tradeDate) {
    return <div className="review-empty">等待加载交易日数据...</div>
  }

  if (isLoading) {
    return <div className="review-loading">加载中...</div>
  }

  // Detail view
  if (viewMode === 'detail' && selectedDiscovery) {
    return (
      <DiscoveryDetail
        discovery={selectedDiscovery}
        onBack={navigateBack}
        tradeDate={tradeDate}
      />
    )
  }

  // Overview + List
  return (
    <div className="discovery-workspace">
      {/* Header */}
      <div className="discovery-header">
        <h2>市场发现</h2>
        <span className="discovery-trade-date">{tradeDate}</span>
        {overview?.status && (
          <span className={`discovery-status ${overview.status}`}>
            {overview.status === 'published' ? '已发布' : overview.status}
          </span>
        )}
      </div>

      {/* Scope family filters */}
      <div className="discovery-filters">
        <button
          className={!scopeFamily ? 'active' : ''}
          onClick={() => setFilter('scopeFamily', undefined)}
        >
          全部 ({total})
        </button>
        {scopeFamilies.map(f => (
          <button
            key={f}
            className={scopeFamily === f ? 'active' : ''}
            onClick={() => setFilter('scopeFamily', f)}
          >
            {f}
          </button>
        ))}
      </div>

      {/* Empty state */}
      {discoveries.length === 0 && (
        <div className="review-empty-state">
          <p>今日无满足当前 Discovery 条件的市场发现</p>
          {overview?.degradedReasons && overview.degradedReasons.length > 0 && (
            <div className="review-degraded">
              {overview.degradedReasons.map((r, i) => (
                <p key={i} className="review-degraded-item">{r}</p>
              ))}
            </div>
          )}
          <p className="review-empty-hint">
            可查看 Signal evidence diagnostics 了解详细信号命中情况
          </p>
        </div>
      )}

      {/* Discovery cards */}
      <div className="discovery-list">
        {discoveries.map(d => (
          <DiscoveryCard
            key={d.discoveryId}
            discovery={d}
            onClick={() => navigateToDetail(d)}
          />
        ))}
      </div>

      {/* Pagination */}
      {total > pageSize && (
        <div className="discovery-pagination">
          <button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</button>
          <span>第 {page} 页 / 共 {Math.ceil(total / pageSize)} 页</span>
          <button disabled={page * pageSize >= total} onClick={() => setPage(p => p + 1)}>下一页</button>
        </div>
      )}
    </div>
  )
}
