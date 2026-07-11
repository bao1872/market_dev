// [EventStatePanel] - 描述: 用户侧状态/事件面板（market + detail 共用）
// PRD V1.1 §7.3: 只使用 StockContext 单一接口（/api/v1/stocks/{symbol}/context）
// market 和 detail 复用同一 query key（['stock-context', symbol, params]）
// 普通用户面板只展示：
//   - 数据日期与质量
//   - 当前价格结构（StateValue code/label）
//   - 成交密集区关系
//   - SQZMOM 动量
//   - 波动位置
//   - 最近状态变化时间线
// 不显示原始 key、JSON、MACD 假值或"筹码共识区"。
// 面板关闭时由父组件不渲染本组件，StockContext 请求 enabled=false。
import { useStockContext } from '@/hooks/useApi'
import type { StockContextResponse, StateValue, StateEventDTO } from '@/api/endpoints'
import styles from './EventStatePanel.module.scss'

export type EventStatePanelStyles = typeof styles

interface EventStatePanelProps {
  /** 股票代码（symbol），用于调用 StockContext API */
  symbol: string | undefined
  /** 历史查询日期（可选，不传则查最新） */
  asOf?: string | null
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

/** 渲染最近状态变化事件时间线 */
function EventTimeline({ events }: { events: StateEventDTO[] }) {
  if (events.length === 0) {
    return (
      <div className={styles.emptyEvents}>暂无近期状态变化</div>
    )
  }
  return (
    <div className={styles.eventTimeline}>
      {events.slice(0, 10).map((ev) => (
        <div key={ev.id} className={styles.eventItem}>
          <div className={styles.eventTime}>{ev.occurredAt}</div>
          <div className={styles.eventTitle}>{ev.title}</div>
          {ev.description && (
            <div className={styles.eventDesc}>{ev.description}</div>
          )}
        </div>
      ))}
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

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>波动位置</h4>
            <StateValueRow label="布林位置" value={state.volatility.bollPosition} />
          </section>

          <section className={styles.section}>
            <h4 className={styles.sectionTitle}>最近状态变化</h4>
            <EventTimeline events={events} />
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
