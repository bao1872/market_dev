// [FilterDiscoveryPanel] - 描述: 阶段二筛选发现（PRD §14.4）
// 固定三组：A 表面/质量偏差、B 状态/速度偏差、C 成交/参与偏差
// 一张 SignalCard 显示：范围/信号类型/生命周期状态/首次出现+持续日数/
//   触发变量/历史分位/coverage/结构化解释/查看归因/查看历史/加入追踪
// 不显示黑箱总分；禁止自由 AI 结论
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getReviewSignals, createReviewTracking, extractReviewError } from './api'
import { reviewKeys } from './queryKeys'
import SignalCard from './SignalCard'
import type { ReviewSignal, ReviewSignalListParams } from './types'
import styles from './review.module.scss'

const PAGE_SIZE = 50

export interface FilterDiscoveryPanelProps {
  tradeDate: string
  scopeType?: string | null
  scopeKey?: string | null
  /** 当前选中信号 ID（高亮卡片） */
  activeSignalId: string | null
  /** 选中信号：更新 URL signalId 并切换到归因阶段 */
  onSelectSignal: (signal: ReviewSignal) => void
  /** 查看归因：切换到归因阶段 */
  onViewAttribution: (signal: ReviewSignal) => void
  /** 查看历史：切换到追踪复核 history Tab */
  onViewHistory: (signal: ReviewSignal) => void
  /** 信号已加入追踪回调（toast 等） */
  onTrackingAdded?: (signal: ReviewSignal) => void
}

export default function FilterDiscoveryPanel({
  tradeDate,
  scopeType,
  scopeKey,
  activeSignalId,
  onSelectSignal,
  onViewAttribution,
  onViewHistory,
  onTrackingAdded,
}: FilterDiscoveryPanelProps) {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState('')

  const params: ReviewSignalListParams = {
    status: statusFilter || undefined,
    scope_type: scopeType || undefined,
    scope_key: scopeKey || undefined,
    page: 1,
    page_size: PAGE_SIZE,
  }

  const query = useQuery({
    queryKey: reviewKeys.signals(tradeDate, params),
    queryFn: () => getReviewSignals(tradeDate, params),
    enabled: !!tradeDate,
    staleTime: 60 * 1000,
  })

  const createTrackingMutation = useMutation({
    mutationFn: (signal: ReviewSignal) =>
      createReviewTracking({
        tracking_type: 'signal',
        source_signal_id: signal.id,
        idempotency_key: `review-signal-${signal.id}-${Date.now()}`,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: reviewKeys.trackings() })
    },
  })

  const handleAddTracking = (signal: ReviewSignal) => {
    createTrackingMutation.mutate(signal, {
      onSuccess: () => onTrackingAdded?.(signal),
      onError: (err) => {
        const e = extractReviewError(err)
        // 复用 toast：通过 window 事件由页面处理（避免直接耦合 store）
        // 这里简单使用 console，实际 toast 由页面 onTrackingAdded 之外处理
        // 不可静默失败，需明确提示
        window.console.error('[Review] 加入追踪失败:', e.message)
      },
    })
  }

  if (query.isError) {
    const err = extractReviewError(query.error)
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>信号列表加载失败</div>
        <div className={styles.stateDesc}>{err.message}</div>
        {err.requestId && <div className={styles.stateRequestId}>request_id={err.requestId}</div>}
      </div>
    )
  }

  const items = query.data?.items ?? []
  // 按筛选器族分组（A/B/C/D）
  const groups: Record<string, ReviewSignal[]> = { A: [], B: [], C: [], D: [] }
  for (const s of items) {
    if (groups[s.filterFamily]) {
      groups[s.filterFamily].push(s)
    } else {
      groups[s.filterFamily] = [s]
    }
  }

  const groupLabels: Record<string, string> = {
    A: 'A 表面/质量偏差',
    B: 'B 状态/速度偏差',
    C: 'C 成交/参与偏差',
    D: 'D 第二金字塔偏差',
  }

  if (!query.isLoading && items.length === 0) {
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>今日未命中已配置偏差筛选器</div>
        <div className={styles.stateDesc}>
          当日复盘未生成任何偏差信号。可切换其他交易日或检查筛选器配置版本
        </div>
      </div>
    )
  }

  return (
    <div className={styles.panel}>
      <div className={styles.toolbar}>
        <select
          className={styles.select}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">全部状态</option>
          <option value="new">新增</option>
          <option value="continuing">持续</option>
          <option value="confirmed">已确认</option>
          <option value="weakened">减弱</option>
          <option value="invalidated">失效</option>
          <option value="transformed">转化</option>
        </select>
        <span className={styles.pagination}>共 {query.data?.total ?? 0} 条信号</span>
      </div>
      {query.isLoading ? (
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>加载信号数据...</div>
        </div>
      ) : (
        ['A', 'B', 'C', 'D'].map((fam) => {
          const group = groups[fam] ?? []
          if (group.length === 0) return null
          return (
            <div key={fam} className={styles.panelSection}>
              <div className={styles.panelSectionHeader}>
                <span className={styles.panelSectionTitle}>{groupLabels[fam]}</span>
                <span className={styles.pagination}>{group.length} 条</span>
              </div>
              <div className={styles.panelSectionBody}>
                <div className={styles.signalGrid}>
                  {group.map((signal) => (
                    <SignalCard
                      key={signal.id}
                      signal={signal}
                      active={activeSignalId === signal.id}
                      onSelect={onSelectSignal}
                      onViewAttribution={onViewAttribution}
                      onViewHistory={onViewHistory}
                      onAddTracking={handleAddTracking}
                    />
                  ))}
                </div>
              </div>
            </div>
          )
        })
      )}
    </div>
  )
}
