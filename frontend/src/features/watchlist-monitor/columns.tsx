// [自选监控] - 桌面端表格列定义
// 职责：提供唯一列定义，首页与自选页共用
import type { DataTableColumn } from '@/components/StrategyDataTable'
import type { WatchlistMonitorRow, MonitorStatus } from './types'
import { fmtNum, fmtTime } from './adapters'

/** 监控状态徽章渲染 */
export function MonitorStatusBadge({ status }: { status: MonitorStatus }) {
  switch (status) {
    case 'PRE_OPEN':
      return <span className="tag small">盘前等待开市</span>
    case 'MORNING_SESSION':
    case 'AFTERNOON_SESSION':
      return <span className="tag info small">交易中</span>
    case 'LUNCH_BREAK':
      return <span className="tag small">午间休市</span>
    case 'MARKET_CLOSED':
      return <span className="tag small">已收盘</span>
    case 'NON_TRADING_DAY':
      return <span className="tag small">非交易日</span>
    case 'WAITING_FIRST_RUN':
      return <span className="tag small">等待首次计算</span>
    case 'SUCCEEDED':
      return <span className="tag success small">已计算</span>
    case 'FAILED':
      return <span className="tag error small">计算失败</span>
    case 'STALE':
      return <span className="tag warn small">数据延迟</span>
    default:
      return <span className="tag small">未知</span>
  }
}

export interface ColumnOptions {
  readonly?: boolean
  onDetail?: (row: WatchlistMonitorRow) => void
  onRemove?: (row: WatchlistMonitorRow) => void
  removePending?: boolean
}

export function getWatchlistMonitorColumns(
  options: ColumnOptions = {},
): DataTableColumn<WatchlistMonitorRow>[] {
  const { readonly = false, onDetail, onRemove, removePending = false } = options

  const columns: DataTableColumn<WatchlistMonitorRow>[] = [
    {
      key: 'stock',
      title: '股票',
      dataType: 'text',
      sortable: true,
      filterable: true,
      sortValue: (row) => row.name,
      filterValue: (row) => `${row.name} ${row.symbol}`,
      render: (row) => (
        <div>
          <div className="symbol">{row.name}</div>
          <div className="symbol-sub">{row.symbol}</div>
        </div>
      ),
    },
    {
      key: 'status',
      title: '状态',
      dataType: 'text',
      sortable: true,
      filterable: true,
      sortValue: (row) => row.monitor_status,
      filterValue: (row) => {
        switch (row.monitor_status) {
          case 'PRE_OPEN':
            return '盘前等待开市'
          case 'MORNING_SESSION':
          case 'AFTERNOON_SESSION':
            return '交易中'
          case 'LUNCH_BREAK':
            return '午间休市'
          case 'MARKET_CLOSED':
            return '已收盘'
          case 'NON_TRADING_DAY':
            return '非交易日'
          case 'SUCCEEDED':
            return '已计算'
          case 'FAILED':
            return '计算失败'
          case 'STALE':
            return '数据延迟'
          case 'WAITING_FIRST_RUN':
            return '等待首次计算'
          default:
            return '未知'
        }
      },
      render: (row) => <MonitorStatusBadge status={row.monitor_status} />,
    },
    {
      key: 'currentPrice',
      title: '当前价',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.current_price ?? 0,
      render: (row) => <span className="num">{fmtNum(row.current_price)}</span>,
    },
    {
      key: 'bbUpper',
      title: 'BB上轨',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.bb_upper ?? 0,
      render: (row) => <span className="num">{fmtNum(row.bb_upper)}</span>,
    },
    {
      key: 'bbMid',
      title: 'BB中轨',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.bb_mid ?? 0,
      render: (row) => <span className="num">{fmtNum(row.bb_mid)}</span>,
    },
    {
      key: 'bbLower',
      title: 'BB下轨',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.bb_lower ?? 0,
      render: (row) => <span className="num">{fmtNum(row.bb_lower)}</span>,
    },
    {
      key: 'upperNode',
      title: '上节点',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.upper_node_price ?? 0,
      render: (row) => (
        <span
          className="num"
          title={
            row.upper_node_low != null && row.upper_node_high != null
              ? `${row.upper_node_low} ~ ${row.upper_node_high}`
              : undefined
          }
        >
          {fmtNum(row.upper_node_price)}
        </span>
      ),
    },
    {
      key: 'lowerNode',
      title: '下节点',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.lower_node_price ?? 0,
      render: (row) => (
        <span
          className="num"
          title={
            row.lower_node_low != null && row.lower_node_high != null
              ? `${row.lower_node_low} ~ ${row.lower_node_high}`
              : undefined
          }
        >
          {fmtNum(row.lower_node_price)}
        </span>
      ),
    },
    {
      key: 'position01',
      title: '位置',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.position_0_1 ?? 0,
      render: (row) => <span className="num">{fmtNum(row.position_0_1)}</span>,
    },
    {
      key: 'pocPrice',
      title: 'POC',
      dataType: 'number',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.poc_price ?? 0,
      render: (row) => <span className="num">{fmtNum(row.poc_price)}</span>,
    },
    {
      key: 'latestEvent',
      title: '最近触发',
      dataType: 'text',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.latest_event?.event_time ?? '',
      render: (row) => {
        if (!row.latest_event) return <span className="muted">-</span>
        const eventType = row.latest_event.event_type
        const time = fmtTime(row.latest_event.event_time)
        const boundary = row.latest_event.boundary
        return (
          <div>
            <div className="symbol">{eventType}</div>
            <div className="symbol-sub">
              {time}
              {boundary != null ? ` · ${boundary}` : ''}
            </div>
          </div>
        )
      },
    },
    {
      key: 'updatedAt',
      title: '更新时间',
      dataType: 'text',
      sortable: true,
      filterable: false,
      sortValue: (row) => row.updated_at ?? '',
      render: (row) => <span className="num">{row.updated_at ?? '-'}</span>,
    },
  ]

  if (!readonly) {
    columns.push({
      key: 'action',
      title: '操作',
      dataType: 'text',
      sortable: false,
      filterable: false,
      isAction: true,
      render: (row) => (
        <div className="actions">
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
      ),
    })
  }

  return columns
}
