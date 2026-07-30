// [TrackingReviewPanel] - 描述: 阶段五追踪复核（PRD §14.7）
// 三个子 Tab：过去发现 / 自选映射 / 事件演化
// 过去发现：首次日期/信号/范围/当前状态/连续天数/状态变化/后续证据
// 自选映射：自选股属于哪些今日命中范围；与板块同步/背离；新增事件；确认/失效条件
// 事件演化：选定追踪查看逐日 evaluation
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useWatchlist } from '@/hooks/useApi'
import {
  getReviewTrackings,
  getTrackingEvaluations,
  closeReviewTracking,
  getReviewSignals,
  extractReviewError,
} from './api'
import { reviewKeys } from './queryKeys'
import type { TrackingTab, ReviewTracking } from './types'
import styles from './review.module.scss'

const TRACKING_STATUS_META: Record<string, { label: string; cls: string }> = {
  active: { label: '进行中', cls: 'chipInfo' },
  confirmed: { label: '已确认', cls: 'chipSuccess' },
  invalidated: { label: '已失效', cls: 'chipDanger' },
  closed: { label: '已关闭', cls: 'chipDefault' },
}

const SUB_TABS: Array<{ value: TrackingTab; label: string }> = [
  { value: 'history', label: '过去发现' },
  { value: 'watchlist', label: '自选映射' },
  { value: 'events', label: '事件演化' },
]

export interface TrackingReviewPanelProps {
  tradeDate: string
  tab: TrackingTab
  onTabChange: (tab: TrackingTab) => void
  showToast?: (title: string, desc: string) => void
}

export default function TrackingReviewPanel({
  tradeDate,
  tab,
  onTabChange,
  showToast,
}: TrackingReviewPanelProps) {
  const queryClient = useQueryClient()
  const [selectedTrackingId, setSelectedTrackingId] = useState<string | null>(null)

  // 用户追踪列表
  const trackingsQuery = useQuery({
    queryKey: reviewKeys.trackings({ page: 1, page_size: 50 }),
    queryFn: () => getReviewTrackings({ page: 1, page_size: 50 }),
    staleTime: 30 * 1000,
  })

  // 当日信号（自选映射用：展示今日命中范围）
  const signalsQuery = useQuery({
    queryKey: reviewKeys.signals(tradeDate, { page: 1, page_size: 100 }),
    queryFn: () => getReviewSignals(tradeDate, { page: 1, page_size: 100 }),
    enabled: !!tradeDate && tab === 'watchlist',
    staleTime: 60 * 1000,
  })

  // 自选列表（自选映射用）
  const watchlistQuery = useWatchlist()
  const watchlistItems = watchlistQuery.data?.items ?? []

  // 选中追踪的逐日 evaluation
  const evalQuery = useQuery({
    queryKey: reviewKeys.evaluations(selectedTrackingId ?? '', { page: 1, page_size: 50 }),
    queryFn: () =>
      getTrackingEvaluations(selectedTrackingId!, { page: 1, page_size: 50 }),
    enabled: !!selectedTrackingId && tab === 'events',
    staleTime: 30 * 1000,
  })

  const closeMutation = useMutation({
    mutationFn: (trackingId: string) =>
      closeReviewTracking(trackingId, `review-close-${trackingId}-${Date.now()}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: reviewKeys.trackings() })
    },
  })

  const handleCloseTracking = (tracking: ReviewTracking) => {
    closeMutation.mutate(tracking.id, {
      onSuccess: () => showToast?.('已关闭追踪', ''),
      onError: (err) => {
        const e = extractReviewError(err)
        showToast?.('关闭追踪失败', e.message)
      },
    })
  }

  const trackings = trackingsQuery.data?.items ?? []

  return (
    <div className={styles.panel}>
      <div className={styles.subTabs}>
        {SUB_TABS.map((t) => (
          <button
            key={t.value}
            type="button"
            className={`${styles.subTab} ${tab === t.value ? styles.subTabActive : ''}`}
            onClick={() => onTabChange(t.value)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'history' && (
        <div className={styles.panelSection}>
          <div className={styles.panelSectionHeader}>
            <span className={styles.panelSectionTitle}>过去发现</span>
            <span className={styles.pagination}>共 {trackingsQuery.data?.total ?? 0} 条追踪</span>
          </div>
          {trackingsQuery.isLoading ? (
            <div className={styles.stateBox}>
              <div className={styles.stateDesc}>加载追踪列表...</div>
            </div>
          ) : trackingsQuery.isError ? (
            (() => {
              const err = extractReviewError(trackingsQuery.error)
              return (
                <div className={styles.stateBox}>
                  <div className={styles.stateDesc}>追踪列表加载失败：{err.message}</div>
                </div>
              )
            })()
          ) : trackings.length === 0 ? (
            <div className={styles.stateBox}>
              <div className={styles.stateTitle}>暂无追踪</div>
              <div className={styles.stateDesc}>
                在「筛选发现」或「个股验证」阶段将信号、范围或股票加入追踪，次日复盘将自动生成 evaluation
              </div>
            </div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>首次日期</th>
                  <th>类型</th>
                  <th>关联</th>
                  <th>当前状态</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {trackings.map((t) => {
                  const meta = TRACKING_STATUS_META[t.status] ?? {
                    label: t.status,
                    cls: 'chipDefault',
                  }
                  return (
                    <tr key={t.id}>
                      <td>{t.createdAt.slice(0, 10)}</td>
                      <td>{t.trackingType}</td>
                      <td>
                        {t.trackingType === 'signal' && t.sourceSignalId
                          ? `信号 ${t.sourceSignalId.slice(0, 8)}`
                          : t.trackingType === 'scope'
                            ? `范围 ${t.scopeType ?? ''}/${t.scopeKey ?? ''}`
                            : t.trackingType === 'instrument' && t.instrumentId
                              ? `个股 ${t.instrumentId.slice(0, 8)}`
                              : '-'}
                      </td>
                      <td>
                        <span className={`${styles.chip} ${styles[meta.cls]}`}>{meta.label}</span>
                      </td>
                      <td>{t.createdAt.slice(0, 16).replace('T', ' ')}</td>
                      <td>
                        {t.status !== 'closed' && (
                          <button
                            type="button"
                            className={styles.btn}
                            disabled={closeMutation.isPending}
                            onClick={() => handleCloseTracking(t)}
                          >
                            关闭
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'watchlist' && (
        <div className={styles.panelSection}>
          <div className={styles.panelSectionHeader}>
            <span className={styles.panelSectionTitle}>自选映射</span>
            <span className={styles.pagination}>
              自选 {watchlistItems.length} · 今日命中范围 {signalsQuery.data?.total ?? 0}
            </span>
          </div>
          <div className={styles.panelSectionBody}>
            <div className={styles.stateDesc}>
              自选股与今日命中范围的精确映射（同步/背离、新增事件、确认/失效条件）
              需结合个股第一金字塔与信号归因，请在「板块归因」与「个股验证」阶段按信号下钻查看。
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
              <div>
                <div className={styles.panelSectionTitle} style={{ marginBottom: 6 }}>自选股</div>
                {watchlistItems.length === 0 ? (
                  <div className={styles.metricUnavailable}>暂无自选股</div>
                ) : (
                  <ul style={{ margin: 0, paddingLeft: 16, fontSize: 13, color: '#98A1B3' }}>
                    {watchlistItems.slice(0, 20).map((w) => (
                      <li key={w.instrument_id}>
                        {w.instrument_id.slice(0, 8)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <div className={styles.panelSectionTitle} style={{ marginBottom: 6 }}>今日命中范围</div>
                {signalsQuery.isLoading ? (
                  <div className={styles.stateDesc}>加载信号...</div>
                ) : (signalsQuery.data?.items ?? []).length === 0 ? (
                  <div className={styles.metricUnavailable}>今日未命中偏差筛选器</div>
                ) : (
                  <ul style={{ margin: 0, paddingLeft: 16, fontSize: 13, color: '#98A1B3' }}>
                    {(signalsQuery.data?.items ?? []).slice(0, 20).map((s) => (
                      <li key={s.id}>
                        {s.scopeName}（{s.filterFamily}/{s.signalType}）
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === 'events' && (
        <div className={styles.panelSection}>
          <div className={styles.panelSectionHeader}>
            <span className={styles.panelSectionTitle}>事件演化</span>
            <select
              className={styles.select}
              value={selectedTrackingId ?? ''}
              onChange={(e) => setSelectedTrackingId(e.target.value || null)}
            >
              <option value="">选择追踪</option>
              {trackings.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.trackingType} · {t.id.slice(0, 8)} · {t.status}
                </option>
              ))}
            </select>
          </div>
          {!selectedTrackingId ? (
            <div className={styles.stateBox}>
              <div className={styles.stateDesc}>请选择一条追踪，查看其逐日状态演化</div>
            </div>
          ) : evalQuery.isLoading ? (
            <div className={styles.stateBox}>
              <div className={styles.stateDesc}>加载逐日评估...</div>
            </div>
          ) : evalQuery.isError ? (
            (() => {
              const err = extractReviewError(evalQuery.error)
              return (
                <div className={styles.stateBox}>
                  <div className={styles.stateDesc}>评估加载失败：{err.message}</div>
                </div>
              )
            })()
          ) : (evalQuery.data?.items ?? []).length === 0 ? (
            <div className={styles.stateBox}>
              <div className={styles.stateTitle}>暂无逐日评估</div>
              <div className={styles.stateDesc}>
                该追踪尚无 evaluation 记录，下一交易日复盘完成后将自动生成
              </div>
            </div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>交易日</th>
                  <th>前日状态</th>
                  <th>当日状态</th>
                  <th>评估详情</th>
                </tr>
              </thead>
              <tbody>
                {evalQuery.data?.items.map((ev) => (
                  <tr key={ev.id}>
                    <td>{ev.tradeDate}</td>
                    <td>{ev.previousState ?? '-'}</td>
                    <td>{ev.currentState}</td>
                    <td>
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 11, color: '#98A1B3' }}>
                        {JSON.stringify(ev.evaluationPayload)}
                      </pre>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
