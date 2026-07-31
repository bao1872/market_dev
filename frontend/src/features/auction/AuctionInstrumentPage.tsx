// [AuctionInstrumentPage] - 描述: 竞价分析个股级页面
// 路由 /auction/stock/:symbol
// 后端完成全部计算，前端只展示、过滤和展开证据
// 接口必须显示：trade_date、algorithm_version、scan_run_id、instrument_id、reason_codes
//
// 展示：
// - 个股锚点列表（structure / chip / composite，含价格区间、强度、新鲜度、有效性、distance_at_close）
// - 个股竞价结果（final_auction_price / prev_close / change_pct / auction_amount / participation_level / 事件类型等）
// - 个股事件追踪（formed / confirmed / weakened / failed / expired 时间线）

import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useAuctionInstrument, extractAuctionError } from './api'
import {
  ANCHOR_DIRECTION_LABELS,
  ANCHOR_TYPE_LABELS,
  EVENT_LIFECYCLE_LABELS,
  PARTICIPATION_LABELS,
  type AnchorItem,
  type EventTracking,
  type InstrumentResult,
} from './types'
import { formatShanghaiTime } from '@/utils/datetime'
import styles from './auction.module.scss'

type EventFilter = 'all' | 'formed' | 'confirmed' | 'weakened' | 'failed' | 'expired'

const EVENT_FILTERS: Array<{ value: EventFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'formed', label: '形成' },
  { value: 'confirmed', label: '确认' },
  { value: 'weakened', label: '减弱' },
  { value: 'failed', label: '失效' },
  { value: 'expired', label: '过期' },
]

type AnchorFilter = 'all' | 'structure' | 'chip' | 'composite'

const ANCHOR_FILTERS: Array<{ value: AnchorFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'structure', label: '结构' },
  { value: 'chip', label: '筹码' },
  { value: 'composite', label: '复合' },
]

/** 涨跌幅颜色 class */
function changePctClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return styles.neutral
  return v > 0 ? styles.up : v < 0 ? styles.down : styles.neutral
}

/** 安全格式化百分比 */
function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(digits)}%`
}

/** 安全格式化数字 */
function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return v.toFixed(digits)
}

/** 安全格式化金额（万元） */
function fmtAmount(v: string | null | undefined): string {
  if (!v) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return v
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(2)}万`
  return n.toFixed(0)
}

/** 安全截断 UUID（前 8 位） */
function fmtId(v: string | null | undefined): string {
  if (!v) return '—'
  return v.slice(0, 8)
}

/** 参与度 chip 样式 */
function participationChipClass(level: string | null | undefined): string {
  if (!level) return styles.chipDefault
  const l = level.toLowerCase()
  if (l.includes('high')) return styles.chipSuccess
  if (l.includes('medium') || l.includes('mid')) return styles.chipWarning
  if (l.includes('low')) return styles.chipDanger
  return styles.chipDefault
}

/** 锚点类型 chip 样式 */
function anchorTypeChipClass(type: string | null | undefined): string {
  if (!type) return styles.chipDefault
  const t = type.toLowerCase()
  if (t === 'composite') return styles.chipBrand
  if (t === 'structure') return styles.chipInfo
  if (t === 'chip') return styles.chipWarning
  return styles.chipDefault
}

/** 锚点方向 chip 样式 */
function directionChipClass(direction: string | null | undefined): string {
  if (!direction) return styles.chipDefault
  const d = direction.toLowerCase()
  if (d === 'up') return styles.chipDanger
  if (d === 'down') return styles.chipSuccess
  return styles.chipDefault
}

/** 生命周期 chip 样式 */
function lifecycleChipClass(lifecycle: string | null | undefined): string {
  if (!lifecycle) return styles.chipDefault
  const l = lifecycle.toLowerCase()
  if (l === 'confirmed') return styles.chipSuccess
  if (l === 'formed') return styles.chipInfo
  if (l === 'weakened') return styles.chipWarning
  if (l === 'failed' || l === 'expired') return styles.chipDanger
  return styles.chipDefault
}

/** KPI 卡片 */
function KpiCard({
  label,
  value,
  sub,
  valueClassName,
}: {
  label: string
  value: string
  sub?: string
  valueClassName?: string
}) {
  return (
    <div className={styles.kpiCard}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={`${styles.kpiValue} ${valueClassName ?? ''}`}>{value}</div>
      {sub && <div className={styles.kpiSub}>{sub}</div>}
    </div>
  )
}

/** 锚点卡片 */
function AnchorCard({ anchor }: { anchor: AnchorItem }) {
  return (
    <div className={styles.anchorCard}>
      <div className={styles.anchorHeader}>
        <span className={`${styles.chip} ${anchorTypeChipClass(anchor.anchor_type)}`}>
          {ANCHOR_TYPE_LABELS[anchor.anchor_type] ?? anchor.anchor_type}
        </span>
        <span className={`${styles.chip} ${directionChipClass(anchor.direction)}`}>
          {ANCHOR_DIRECTION_LABELS[anchor.direction] ?? anchor.direction}
        </span>
        <span className={styles.anchorType}>
          strength={anchor.strength.toFixed(2)}
        </span>
        {anchor.is_active ? (
          <span className={`${styles.chip} ${styles.chipSuccess}`}>活跃</span>
        ) : (
          <span className={`${styles.chip} ${styles.chipDefault}`}>失效</span>
        )}
      </div>
      <div className={styles.anchorPriceRow}>
        <div className={styles.anchorPriceItem}>
          <span>下沿</span>
          <b>{anchor.lower_price}</b>
        </div>
        <div className={styles.anchorPriceItem}>
          <span>中心</span>
          <b>{anchor.center_price}</b>
        </div>
        <div className={styles.anchorPriceItem}>
          <span>上沿</span>
          <b>{anchor.upper_price}</b>
        </div>
        <div className={styles.anchorPriceItem}>
          <span>distance_at_close</span>
          <b>{anchor.distance_at_close ?? '—'}</b>
        </div>
      </div>
      <div className={styles.anchorMeta}>
        <span>freshness: {anchor.freshness}</span>
        <span>validity: {anchor.validity}</span>
        <span>price_adjustment_version: {anchor.price_adjustment_version}</span>
        <span>snapshot: <code>{fmtId(anchor.snapshot_id)}</code></span>
      </div>
      {anchor.reason_codes.length > 0 && (
        <div className={styles.reasonCodes}>
          {anchor.reason_codes.map((rc) => (
            <span key={rc} className={styles.reasonCode}>{rc}</span>
          ))}
        </div>
      )}
    </div>
  )
}

/** 事件卡片 */
function EventCard({ event }: { event: EventTracking }) {
  return (
    <div className={styles.eventCard}>
      <div className={styles.eventHeader}>
        <span className={styles.eventType}>{event.event_type}</span>
        <span className={`${styles.chip} ${lifecycleChipClass(event.lifecycle)}`}>
          {EVENT_LIFECYCLE_LABELS[event.lifecycle] ?? event.lifecycle}
        </span>
      </div>
      <div className={styles.eventMeta}>
        {event.anchor_id && <span>anchor: <code>{fmtId(event.anchor_id)}</code></span>}
        {event.formed_at && <span>形成: {formatShanghaiTime(event.formed_at)}</span>}
        {event.confirmed_at && <span>确认: {formatShanghaiTime(event.confirmed_at)}</span>}
        {event.weakened_at && <span>减弱: {formatShanghaiTime(event.weakened_at)}</span>}
        {event.failed_at && <span>失效: {formatShanghaiTime(event.failed_at)}</span>}
        {event.expired_at && <span>过期: {formatShanghaiTime(event.expired_at)}</span>}
      </div>
      {event.trigger_price && (
        <div className={styles.eventTrigger}>
          trigger_price={event.trigger_price}
          {event.trigger_condition ? ` · ${event.trigger_condition}` : ''}
        </div>
      )}
      {event.reason_codes.length > 0 && (
        <div className={styles.reasonCodes}>
          {event.reason_codes.map((rc) => (
            <span key={rc} className={styles.reasonCode}>{rc}</span>
          ))}
        </div>
      )}
    </div>
  )
}

/** 个股竞价结果块 */
function InstrumentResultBlock({ result }: { result: InstrumentResult }) {
  const changeCls = changePctClass(result.change_pct)
  return (
    <>
      <div className={styles.kpiGrid}>
        <KpiCard
          label="竞价价"
          value={result.final_auction_price ?? '—'}
          sub={`前收 ${result.prev_close ?? '—'}`}
        />
        <KpiCard
          label="涨跌幅"
          value={
            result.change_pct === null
              ? '—'
              : `${result.change_pct >= 0 ? '+' : ''}${result.change_pct.toFixed(2)}%`
          }
          valueClassName={changeCls}
        />
        <KpiCard
          label="竞价金额"
          value={fmtAmount(result.auction_amount)}
          sub={`竞价量 ${result.auction_volume ?? '—'}`}
        />
        <KpiCard
          label="相对量比"
          value={fmtNum(result.relative_volume_median_20d)}
          sub={`量分位 ${fmtNum(result.volume_percentile)}`}
        />
        <KpiCard
          label="ATR 距离"
          value={fmtPct(result.atr_distance_pct)}
        />
        <KpiCard
          label="参与度"
          value={
            result.participation_level
              ? PARTICIPATION_LABELS[result.participation_level] ?? result.participation_level
              : '—'
          }
        />
        <KpiCard
          label="结构位置"
          value={result.structure_position ?? '—'}
        />
        <KpiCard
          label="筹码位置"
          value={result.chip_position ?? '—'}
        />
        <KpiCard
          label="趋势背景"
          value={result.trend_background ?? '—'}
        />
        <KpiCard
          label="事件类型"
          value={result.event_type ?? '—'}
          sub={
            result.event_lifecycle
              ? EVENT_LIFECYCLE_LABELS[result.event_lifecycle] ?? result.event_lifecycle
              : undefined
          }
        />
      </div>

      {/* 状态标记 */}
      <div className={styles.toolbar}>
        <span className={styles.toolbarLabel}>状态标记：</span>
        {result.is_limit_up && (
          <span className={`${styles.chip} ${styles.chipDanger}`}>涨停</span>
        )}
        {result.is_limit_down && (
          <span className={`${styles.chip} ${styles.chipSuccess}`}>跌停</span>
        )}
        {result.is_suspended && (
          <span className={`${styles.chip} ${styles.chipDefault}`}>停牌</span>
        )}
        {result.is_ex_right && (
          <span className={`${styles.chip} ${styles.chipWarning}`}>除权</span>
        )}
        {!result.is_limit_up &&
          !result.is_limit_down &&
          !result.is_suspended &&
          !result.is_ex_right && (
            <span className={`${styles.chip} ${styles.chipDefault}`}>无</span>
          )}
        <span className={`${styles.chip} ${participationChipClass(result.participation_level)}`}>
          参与度 {result.participation_level ?? '—'}
        </span>
      </div>

      {/* 锚点引用 */}
      {result.anchor_ids && result.anchor_ids.length > 0 && (
        <div className={styles.anchorMeta}>
          <span className={styles.toolbarLabel}>关联锚点：</span>
          {result.anchor_ids.map((aid) => (
            <code key={aid} className={styles.metaValue}>{fmtId(aid)}</code>
          ))}
        </div>
      )}

      {result.reason_codes.length > 0 && (
        <div className={styles.reasonCodes}>
          {result.reason_codes.map((rc) => (
            <span key={rc} className={styles.reasonCode}>{rc}</span>
          ))}
        </div>
      )}
    </>
  )
}

export default function AuctionInstrumentPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams] = useSearchParams()
  const tradeDate = searchParams.get('trade_date') ?? undefined
  const [anchorFilter, setAnchorFilter] = useState<AnchorFilter>('all')
  const [eventFilter, setEventFilter] = useState<EventFilter>('all')

  const query = useAuctionInstrument(symbol, tradeDate)
  const data = query.data

  // 锚点筛选（前端只过滤，不重算）
  const filteredAnchors = useMemo(() => {
    if (!data?.anchors) return []
    if (anchorFilter === 'all') return data.anchors
    return data.anchors.filter((a) => a.anchor_type === anchorFilter)
  }, [data?.anchors, anchorFilter])

  // 事件筛选（前端只过滤，不重算）
  const filteredEvents = useMemo(() => {
    if (!data?.events) return []
    if (eventFilter === 'all') return data.events
    return data.events.filter((e) => e.lifecycle === eventFilter)
  }, [data?.events, eventFilter])

  // 缺少 symbol 参数
  if (!symbol) {
    return (
      <div className={styles.auctionPage}>
        <div className={styles.stateBox}>
          <div className={styles.stateTitle}>缺少股票代码</div>
          <div className={styles.stateDesc}>URL 路径必须包含 symbol，例如 /auction/stock/{`{symbol}`}</div>
          <Link to="/auction" className={styles.linkBtn}>返回市场页 →</Link>
        </div>
      </div>
    )
  }

  // 错误态
  if (query.isError) {
    const err = extractAuctionError(query.error)
    return (
      <div className={styles.auctionPage}>
        <div className={styles.breadcrumb}>
          <Link to="/auction" className={styles.breadcrumbItem}>市场</Link>
          <span className={styles.breadcrumbSep}>/</span>
          <span className={styles.breadcrumbCurrent}>{symbol}</span>
        </div>
        <div className={styles.stateBox}>
          <div className={styles.stateTitle}>竞价个股页加载失败</div>
          <div className={styles.stateDesc}>{err.message}</div>
          {err.requestId && <div className={styles.stateRequestId}>request_id={err.requestId}</div>}
        </div>
      </div>
    )
  }

  // 加载态
  if (query.isLoading || !data) {
    return (
      <div className={styles.auctionPage}>
        <div className={styles.breadcrumb}>
          <Link to="/auction" className={styles.breadcrumbItem}>市场</Link>
          <span className={styles.breadcrumbSep}>/</span>
          <span className={styles.breadcrumbCurrent}>{symbol}</span>
        </div>
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>加载竞价个股数据...</div>
        </div>
      </div>
    )
  }

  return (
    <div className={styles.auctionPage}>
      {/* 面包屑 */}
      <div className={styles.breadcrumb}>
        <Link to="/auction" className={styles.breadcrumbItem}>市场</Link>
        <span className={styles.breadcrumbSep}>/</span>
        <span className={styles.breadcrumbCurrent}>{symbol}</span>
      </div>

      {/* 顶部信息栏：trade_date / algorithm_version / scan_run_id / instrument_id / reason_codes */}
      <div className={styles.metaBar}>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>交易日:</span>
          <span className={styles.metaValue}>{data.trade_date}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>算法版本:</span>
          <span className={styles.metaValue}>{data.algorithm_version}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>scan_run_id:</span>
          <span className={styles.metaValue}>{fmtId(data.scan_run_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>instrument_id:</span>
          <span className={styles.metaValue}>{fmtId(data.instrument_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>symbol:</span>
          <span className={styles.metaValue}>{symbol}</span>
        </span>
        {data.reason_codes.length > 0 && (
          <div className={styles.reasonCodes}>
            {data.reason_codes.map((rc) => (
              <span key={rc} className={styles.reasonCode}>{rc}</span>
            ))}
          </div>
        )}
      </div>

      {/* 个股竞价结果 */}
      <div>
        <div className={styles.sectionTitle}>竞价结果</div>
        {data.result ? (
          <InstrumentResultBlock result={data.result} />
        ) : (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>该股票在当日竞价 scan 中无结果</div>
          </div>
        )}
      </div>

      {/* 锚点列表 */}
      <div>
        <div className={styles.sectionTitle}>
          锚点（{data.anchors.length}）
        </div>
        {data.anchors.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无锚点数据</div>
          </div>
        ) : (
          <>
            <div className={styles.toolbar}>
              <span className={styles.toolbarLabel}>锚点筛选：</span>
              {ANCHOR_FILTERS.map((f) => (
                <button
                  key={f.value}
                  type="button"
                  className={`${styles.btn} ${anchorFilter === f.value ? styles.btnPrimary : ''}`}
                  onClick={() => setAnchorFilter(f.value)}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <div className={styles.anchorList}>
              {filteredAnchors.map((anchor) => (
                <AnchorCard key={anchor.id} anchor={anchor} />
              ))}
            </div>
          </>
        )}
      </div>

      {/* 事件追踪 */}
      <div>
        <div className={styles.sectionTitle}>
          事件追踪（{data.events.length}）
        </div>
        {data.events.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无事件追踪</div>
          </div>
        ) : (
          <>
            <div className={styles.toolbar}>
              <span className={styles.toolbarLabel}>事件筛选：</span>
              {EVENT_FILTERS.map((f) => (
                <button
                  key={f.value}
                  type="button"
                  className={`${styles.btn} ${eventFilter === f.value ? styles.btnPrimary : ''}`}
                  onClick={() => setEventFilter(f.value)}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <div className={styles.eventList}>
              {filteredEvents.map((ev) => (
                <EventCard key={ev.id} event={ev} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
