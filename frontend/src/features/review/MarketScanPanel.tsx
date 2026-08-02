// [MarketScanPanel] - 描述: 阶段一市场扫描（PRD §14.3）
// 主表：范围名称/类型/P/Q/U/C/V（值+方向+历史分位细条）/命中数量/coverage/数据状态
// 点击一行：更新 URL scope，进入该范围信号列表，不直接跳转个股
// 服务端分页；只读已发布快照，前端不计算聚合变量
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getReviewScopes, extractReviewError } from './api'
import { reviewKeys } from './queryKeys'
import ScopeMetricsTable from './ScopeMetricsTable'
import type { ReviewScopeMetrics, ReviewScopeListParams } from './types'
import styles from './review.module.scss'

const SCOPE_TYPE_OPTIONS = [
  { value: '', label: '全部范围' },
  { value: 'market', label: '全市场' },
  { value: 'major_index', label: '主要指数' },
  { value: 'style', label: '风格' },
  { value: 'industry_l1', label: '一级行业' },
  { value: 'industry_l2', label: '二级行业' },
  { value: 'industry_l3', label: '三级行业' },
  { value: 'concept', label: '概念' },
]

const PAGE_SIZE = 20

export interface MarketScanPanelProps {
  tradeDate: string
  /** 当前选中 scopeKey（高亮行） */
  activeScopeKey: string | null
  /** 点击范围行：更新 URL scope 并切换到信号阶段 */
  onSelectScope: (scope: ReviewScopeMetrics) => void
  /** 打开证据抽屉（点击指标表头时） */
  onOpenEvidence?: (scope: ReviewScopeMetrics) => void
}

export default function MarketScanPanel({
  tradeDate,
  activeScopeKey,
  onSelectScope,
}: MarketScanPanelProps) {
  const [scopeType, setScopeType] = useState('')
  const [page, setPage] = useState(1)

  const params: ReviewScopeListParams = {
    scope_type: scopeType || undefined,
    page,
    page_size: PAGE_SIZE,
  }

  const query = useQuery({
    queryKey: reviewKeys.scopes(tradeDate, params),
    queryFn: () => getReviewScopes(tradeDate, params),
    enabled: !!tradeDate,
    staleTime: 60 * 1000,
  })

  const items = query.data?.items ?? []
  const total = query.data?.total ?? 0
  const hasMore = query.data?.has_more ?? false

  const handleScopeTypeChange = (v: string) => {
    setScopeType(v)
    setPage(1)
  }

  if (query.isError) {
    const err = extractReviewError(query.error)
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>市场扫描加载失败</div>
        <div className={styles.stateDesc}>{err.message}</div>
        {err.requestId && <div className={styles.stateRequestId}>request_id={err.requestId}</div>}
      </div>
    )
  }

  return (
    <div className={styles.panel}>
      <div className={styles.toolbar}>
        <select
          className={styles.select}
          value={scopeType}
          onChange={(e) => handleScopeTypeChange(e.target.value)}
        >
          {SCOPE_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <span className={styles.pagination}>共 {total} 个范围</span>
      </div>
      {query.isLoading ? (
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>加载范围扫描数据...</div>
        </div>
      ) : (
        <ScopeMetricsTable
          items={items}
          activeScopeKey={activeScopeKey}
          onRowClick={onSelectScope}
        />
      )}
      {(total > PAGE_SIZE || hasMore) && (
        <div className={styles.pagination}>
          <button
            type="button"
            className={styles.btn}
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            上一页
          </button>
          <span>第 {page} 页</span>
          <button
            type="button"
            className={styles.btn}
            disabled={!hasMore}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
