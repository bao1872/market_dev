// [BoardAttributionPanel] - 描述: 阶段三板块归因（PRD §14.5）
// 四块：信号证据链 / 第二金字塔 / 子范围贡献表 / 代表股票预览
// 归因说明由模板根据结构化字段生成，禁止调用大模型自由编写结论
// 第二金字塔（趋势/结构/动量/内部分布）复用 /boards/analysis，不复制业务逻辑（PRD §16）
import { useQuery } from '@tanstack/react-query'
import { getReviewSignal, getSignalAttributions, getSignalInstruments, extractReviewError } from './api'
import { reviewKeys } from './queryKeys'
import AttributionTable from './AttributionTable'
import ReviewInstrumentTable from './ReviewInstrumentTable'
import type { ReviewAttribution, ReviewInstrument, ReviewSignal } from './types'
import styles from './review.module.scss'

export interface BoardAttributionPanelProps {
  signalId: string | null
  /** 跳转到 /boards/analysis 查看完整板块第二金字塔 */
  boardId?: string | null
  onOpenEvidence?: (signal: ReviewSignal) => void
  onOpenAttributionEvidence?: (attr: ReviewAttribution) => void
  onOpenInstrumentEvidence?: (inst: ReviewInstrument) => void
  onSelectAttributionScope?: (attr: ReviewAttribution) => void
}

/** 信号证据链模板化解释（根据结构化字段生成，非 AI 自由结论） */
function renderSignalChain(signal: ReviewSignal | undefined): string[] {
  if (!signal) return []
  const chain: string[] = []
  const ev = signal.evidencePayload
  const pattern = ev?.['pattern']
  if (typeof pattern === 'string' && pattern) {
    chain.push(pattern)
  }
  const triggerMetrics = signal.triggerPayload?.['metrics']
  if (Array.isArray(triggerMetrics)) {
    const names = triggerMetrics
      .map((m) =>
        typeof m === 'object' && m !== null && 'name' in m
          ? String((m as { name: string }).name)
          : '',
      )
      .filter(Boolean)
    if (names.length > 0) {
      chain.push(`触发变量：${names.join('、')}`)
    }
  }
  const pct = ev?.['historyPercentile120d']
  if (typeof pct === 'number') {
    chain.push(`120日历史分位 ${pct.toFixed(1)}`)
  }
  if (chain.length === 0) {
    chain.push(`信号 ${signal.signalType} 命中范围 ${signal.scopeName}`)
  }
  return chain
}

export default function BoardAttributionPanel({
  signalId,
  boardId,
  onOpenEvidence,
  onOpenAttributionEvidence,
  onOpenInstrumentEvidence,
  onSelectAttributionScope,
}: BoardAttributionPanelProps) {
  const signalQuery = useQuery({
    queryKey: reviewKeys.signal(signalId ?? ''),
    queryFn: () => getReviewSignal(signalId!),
    enabled: !!signalId,
    staleTime: 60 * 1000,
  })
  const attrQuery = useQuery({
    queryKey: reviewKeys.attributions(signalId ?? '', { page: 1, page_size: 20 }),
    queryFn: () => getSignalAttributions(signalId!, { page: 1, page_size: 20 }),
    enabled: !!signalId,
    staleTime: 60 * 1000,
  })
  const instQuery = useQuery({
    queryKey: reviewKeys.instruments(signalId ?? '', { page: 1, page_size: 5 }),
    queryFn: () => getSignalInstruments(signalId!, { page: 1, page_size: 5 }),
    enabled: !!signalId,
    staleTime: 60 * 1000,
  })

  if (!signalId) {
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>未选择信号</div>
        <div className={styles.stateDesc}>
          请在「筛选发现」阶段选择一条信号，查看其板块归因与子范围贡献
        </div>
      </div>
    )
  }

  if (signalQuery.isError) {
    const err = extractReviewError(signalQuery.error)
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>信号详情加载失败</div>
        <div className={styles.stateDesc}>{err.message}</div>
        {err.requestId && <div className={styles.stateRequestId}>request_id={err.requestId}</div>}
      </div>
    )
  }

  const signal = signalQuery.data
  const chain = renderSignalChain(signal)

  return (
    <div className={styles.panel}>
      {/* 信号证据链 */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>信号证据链</span>
          {signal && (
            <button
              type="button"
              className={styles.btnLink}
              onClick={() => onOpenEvidence?.(signal)}
            >
              查看完整证据
            </button>
          )}
        </div>
        <div className={styles.panelSectionBody}>
          {signalQuery.isLoading ? (
            <div className={styles.stateDesc}>加载信号...</div>
          ) : signal ? (
            <div className={styles.signalCardExplain}>
              <div>
                <strong>{signal.scopeName}</strong>（{signal.scopeType}） · {signal.filterFamily}
                {signal.signalType} · 状态 {signal.status} · 持续 {signal.durationDays} 日
              </div>
              {chain.map((c, i) => (
                <div key={i}>{c}</div>
              ))}
            </div>
          ) : (
            <div className={styles.stateDesc}>无信号数据</div>
          )}
        </div>
      </div>

      {/* 第二金字塔：复用 /boards/analysis，不复制业务逻辑 */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>第二金字塔（板块趋势/结构/动量/分布）</span>
          {boardId && (
            <a
              className={styles.btnLink}
              href={`/boards/${boardId}`}
              target="_blank"
              rel="noreferrer"
            >
              在 /boards/analysis 查看
            </a>
          )}
        </div>
        <div className={styles.panelSectionBody}>
          <div className={styles.stateDesc}>
            板块第二金字塔的完整趋势、结构、动量与内部分布在「板块分析」页面维护，
            复盘页不复制其计算与展示逻辑。请通过上方链接查看该板块原始分析。
          </div>
        </div>
      </div>

      {/* 子范围贡献表 */}
      <div>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>子范围贡献</span>
        </div>
        {attrQuery.isLoading ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>加载归因数据...</div>
          </div>
        ) : attrQuery.isError ? (
          (() => {
            const err = extractReviewError(attrQuery.error)
            return (
              <div className={styles.stateBox}>
                <div className={styles.stateDesc}>归因加载失败：{err.message}</div>
              </div>
            )
          })()
        ) : (
          <AttributionTable
            items={attrQuery.data?.items ?? []}
            onRowClick={(attr) => {
              onSelectAttributionScope?.(attr)
              onOpenAttributionEvidence?.(attr)
            }}
          />
        )}
      </div>

      {/* 代表股票预览（top 5） */}
      <div>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>代表股票预览</span>
        </div>
        {instQuery.isLoading ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>加载代表股票...</div>
          </div>
        ) : (
          <ReviewInstrumentTable
            items={instQuery.data?.items ?? []}
            onNavigateToStock={(inst) => onOpenInstrumentEvidence?.(inst)}
          />
        )}
      </div>
    </div>
  )
}
