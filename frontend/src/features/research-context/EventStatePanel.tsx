// [EventStatePanel] - 描述: 用户侧状态/事件面板（market + detail 共用）
// PRD V1.1 §7.3: 只使用 StockContext 单一接口（/api/v1/stocks/{symbol}/context）
// market 和 detail 复用同一 query key（['stock-context', symbol, params]）
// 普通用户面板只展示：
//   - 数据日期与质量
//   - 当前价格结构（StateValue code/label）
//   - 成交密集区关系
//   - SQZMOM 动量
//   - DSA 方向 + 趋势对齐（时序摘要）
//   - 波动位置
//   - 最近状态变化时间线（event_id 高亮 + 证据展开）
// 不显示原始 key、JSON、MACD 假值或"筹码共识区"。
// 面板关闭时由父组件不渲染本组件，StockContext 请求 enabled=false。
import { useState, useRef, useEffect } from 'react'
import { useStockContext } from '@/hooks/useApi'
import type { StockContextResponse, StateValue, StateEventDTO } from '@/api/endpoints'
import styles from './EventStatePanel.module.scss'

export type EventStatePanelStyles = typeof styles

interface EventStatePanelProps {
  /** 股票代码（symbol），用于调用 StockContext API */
  symbol: string | undefined
  /** 历史查询日期（可选，不传则查最新） */
  asOf?: string | null
  /** 事件 ID（可选，从 URL event_id 传入，高亮定位对应事件） */
  eventId?: string | null
}

/** 将 StateValue 渲染为人类可读的标签 + 值 */
function StateValueRow({ label, value }: { label: string; value: StateValue }) {
  return (
    <div className={styles.stateRow}>
      <span className={styles.stateLabel}>{label}</span>
      <span className={styles.stateValue}>{value.label}</span>
    </div>
  )
}

/** 渲染 DSA 方向 + 趋势对齐时序摘要 */
function TemporalSummary({ temporal }: { temporal: StateValue[] }) {
  if (temporal.length === 0) {
    return null
  }
  return (
    <div className={styles.temporalSummary}>
      {temporal.map((tv, idx) => (
        <StateValueRow key={idx} label={tv.label} value={tv} />
      ))}
    </div>
  )
}

/** 渲染单条事件的证据列表（可展开/收起） */
function EventEvidence({ evidence }: { evidence: StateEventDTO['evidence'] }) {
  if (evidence.length === 0) {
    return null
  }
  return (
    <div className={styles.eventEvidence}>
      {evidence.map((ev, idx) => (
        <div key={idx} className={styles.evidenceItem}>
          <span className={styles.evidenceField}>{ev.fieldName}</span>
          <span className={styles.evidenceChange}>
            {ev.previousValue ?? '—'} → {ev.currentValue ?? '—'}
          </span>
        </div>
      ))}
    </div>
  )
}

/** 渲染最近状态变化事件时间线（支持 event_id 高亮 + 证据展开） */
function EventTimeline({
  events,
  highlightEventId,
}: {
  events: StateEventDTO[]
  highlightEventId?: string | null
}) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const highlightedRef = useRef<HTMLDivElement>(null)

  // 当 highlightEventId 变化时，自动展开对应事件并滚动到它
  useEffect(() => {
    if (highlightEventId) {
      setExpandedIds((prev) => new Set(prev).add(highlightEventId))
    }
  }, [highlightEventId])

  // 滚动到高亮事件
  useEffect(() => {
    if (highlightEventId && highlightedRef.current) {
      highlightedRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [highlightEventId, events])

  if (events.length === 0) {
    return (
      <div className={styles.emptyEvents}>暂无近期状态变化</div>
    )
  }

  const toggleExpand = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  return (
    <div className={styles.eventTimeline}>
      {events.slice(0, 10).map((ev) => {
        const isHighlighted = highlightEventId === ev.id
        const isExpanded = expandedIds.has(ev.id)
        const hasEvidence = ev.evidence.length > 0
        return (
          <div
            key={ev.id}
            ref={isHighlighted ? highlightedRef : null}
            className={`${styles.eventItem} ${isHighlighted ? styles.eventItemHighlighted : ''}`}
          >
            <div
              className={styles.eventHeader}
              onClick={hasEvidence ? () => toggleExpand(ev.id) : undefined}
              role={hasEvidence ? 'button' : undefined}
              tabIndex={hasEvidence ? 0 : undefined}
            >
              <div className={styles.eventTime}>{ev.occurredAt}</div>
              <div className={styles.eventTitle}>{ev.title}</div>
              {ev.description && (
                <div className={styles.eventDesc}>{ev.description}</div>
              )}
              {hasEvidence && (
                <div className={styles.eventExpandHint}>
                  {isExpanded ? '收起证据' : '展开证据'}
                </div>
              )}
            </div>
            {isExpanded && hasEvidence && (
              <EventEvidence evidence={ev.evidence} />
            )}
          </div>
        )
      })}
    </div>
  )
}

/** 数据质量提示 */
function DataQualityBanner({ response }: { response: StockContextResponse }) {
  const { dataQuality } = response
  if (dataQuality.hasSucceededRun && dataQuality.hasSnapshot && dataQuality.degradedReasons.length === 0) {
    return null
  }
  return (
    <div className={styles.qualityBanner}>
      {dataQuality.degradedReasons.map((reason) => (
        <div key={reason} className={styles.qualityReason}>{reason}</div>
      ))}
    </div>
  )
}

export function EventStatePanel({
  symbol,
  asOf,
  eventId,
}: EventStatePanelProps) {
  const query = useStockContext(symbol, asOf ? { as_of: asOf } : undefined, {
    enabled: true, // 本组件只在面板打开时渲染，故始终 enabled
  })

  if (!symbol) {
    return (
      <div className={styles.empty}>
        <div className={styles.emptyText}>请选择一只股票查看状态</div>
      </div>
    )
  }

  if (query.isLoading) {
    return (
      <div className={styles.loading}>
        <div className={styles.loadingText}>加载中…</div>
      </div>
    )
  }

  if (query.isError || !query.data) {
    return (
      <div className={styles.error}>
        <div className={styles.errorText}>数据加载失败</div>
        <button
          className={styles.retryBtn}
          onClick={() => query.refetch()}
        >
          重试
        </button>
      </div>
    )
  }

  const { state, events, dataQuality } = query.data

  return (
    <div className={styles.panel}>
      <DataQualityBanner response={query.data} />

      {state && (
        <>
          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>数据日期</h4>
            <div className={styles.dataDate}>{state.asOf}</div>
            {dataQuality.runPublishedAt && (
              <div className={styles.dataMeta}>发布于 {dataQuality.runPublishedAt}</div>
            )}
          </section>

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>当前价格结构</h4>
            <StateValueRow label="价格位置" value={state.structure.price} />
          </section>

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>成交密集区关系</h4>
            <StateValueRow label="密集区关系" value={state.structure.consensusRelation} />
          </section>

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>SQZMOM 动量</h4>
            <StateValueRow label="SQZMOM" value={state.momentum.sqzmom} />
          </section>

          {state.momentum.temporal.length > 0 && (
            <section className={styles.section}>
              <h4 className={styles.sectionTitle}>时序摘要</h4>
              <TemporalSummary temporal={state.momentum.temporal} />
            </section>
          )}

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>波动位置</h4>
            <StateValueRow label="布林位置" value={state.volatility.bollPosition} />
          </section>

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>最近状态变化</h4>
            <EventTimeline events={events} highlightEventId={eventId} />
          </section>
        </>
      )}

      {!state && (
        <div className={styles.noState}>
          <div className={styles.noStateText}>暂无可用状态数据</div>
        </div>
      )}
    </div>
  )
}
