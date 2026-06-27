// [自选监控] - 移动端卡片列表
// 职责：在窄屏下以卡片形式展示自选监控数据
import type { WatchlistMonitorRow } from './types'
import { MonitorStatusBadge, translateEventType } from './columns'
import { fmtNum, fmtPct } from './adapters'

interface WatchlistMonitorCardProps {
  row: WatchlistMonitorRow
  readonly?: boolean
  onDetail?: (row: WatchlistMonitorRow) => void
  onRemove?: (row: WatchlistMonitorRow) => void
  removePending?: boolean
}

function WatchlistMonitorCard({
  row,
  readonly = false,
  onDetail,
  onRemove,
  removePending = false,
}: WatchlistMonitorCardProps) {
  return (
    <div className="watchlist-monitor-card">
      <div className="watchlist-card-head">
        <div>
          <div className="symbol">{row.name}</div>
          <div className="symbol-sub">{row.symbol}</div>
        </div>
        <MonitorStatusBadge status={row.monitor_status} />
      </div>

      <div className="watchlist-card-grid">
        <div>
          <span>当前价</span>
          <b className="num">{fmtNum(row.current_price)}</b>
        </div>
        <div>
          <span>近期波动上沿</span>
          <b className="num">{fmtNum(row.bb_upper)}</b>
        </div>
        <div>
          <span>近期价格中枢</span>
          <b className="num">{fmtNum(row.bb_mid)}</b>
        </div>
        <div>
          <span>近期波动下沿</span>
          <b className="num">{fmtNum(row.bb_lower)}</b>
        </div>
        <div>
          <span>上方成交密集区</span>
          <b className="num">{fmtNum(row.upper_node_price)}</b>
        </div>
        <div>
          <span>下方成交密集区</span>
          <b className="num">{fmtNum(row.lower_node_price)}</b>
        </div>
        <div>
          <span>当前区间位置</span>
          <b className="num">{fmtPct(row.position_0_1)}</b>
        </div>
        <div>
          <span>最密集成交价</span>
          <b className="num">{fmtNum(row.poc_price)}</b>
        </div>
      </div>

      <div className="watchlist-card-meta">
        <span>
          最近监控提示: {row.latest_event ? `${translateEventType(row.latest_event.event_type)} · ${row.latest_event.event_time.slice(11, 16)}` : '-'}
        </span>
        <span>更新时间: {row.updated_at ?? '-'}</span>
      </div>

      {!readonly && (onDetail || onRemove) && (
        <div className="watchlist-card-actions">
          {onDetail && (
            <button className="btn small" onClick={() => onDetail(row)}>
              详情
            </button>
          )}
          {onRemove && (
            <button
              className="btn small danger"
              onClick={() => onRemove(row)}
              disabled={removePending}
            >
              移出自选
            </button>
          )}
        </div>
      )}
    </div>
  )
}

interface WatchlistMonitorCardsProps {
  rows: WatchlistMonitorRow[]
  readonly?: boolean
  onDetail?: (row: WatchlistMonitorRow) => void
  onRemove?: (row: WatchlistMonitorRow) => void
  removePending?: boolean
  emptyText?: string
}

export function WatchlistMonitorCards({
  rows,
  readonly = false,
  onDetail,
  onRemove,
  removePending = false,
  emptyText = '暂无监控数据',
}: WatchlistMonitorCardsProps) {
  if (rows.length === 0) {
    return <div className="empty">{emptyText}</div>
  }

  return (
    <div className="watchlist-cards">
      {rows.map((row) => (
        <WatchlistMonitorCard
          key={row.instrument_id}
          row={row}
          readonly={readonly}
          onDetail={onDetail}
          onRemove={onRemove}
          removePending={removePending}
        />
      ))}
    </div>
  )
}
