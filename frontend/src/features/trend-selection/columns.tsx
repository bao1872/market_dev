// [趋势选股] - 桌面端表格列定义
// 职责：提供唯一列定义，首页"最新趋势快照"与趋势选股页共用
// 唯一性：spec 第七节要求 features/trend-selection 是趋势选股列定义唯一实现
//         禁止 IndexPage/ScreenerPage 重新定义同字段列
// [CHANGE-20260731-REMOVE-DSA] 删除旧 DSA-only 列：趋势、连续天、VWAP差、段涨跌、斜率、强度、
//   主要结构、短线结构、对齐、OB数、事件、新鲜度、动量。列表只保留基础列+第一金字塔列+自选操作。
import type { ReactNode } from 'react'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import type { TrendSelectionRow } from './types'
import {
  fmtChange,
  changePctColorClass,
  getStockDisplay,
} from './adapters'

export interface TrendSelectionColumnOptions {
  // 主页操作列：加入自选（提供时操作列渲染为"已自选/+ 自选"）
  onAddToWatchlist?: (row: TrendSelectionRow) => void
  addPending?: boolean
  // 趋势选股页操作列：查看详情（提供时操作列渲染为"详情"按钮）
  onDetail?: (row: TrendSelectionRow) => void
  // /market 股票名称链接：点击进入 /stock/:symbol?returnTo=...
  onNavigateToStock?: (row: TrendSelectionRow) => void
  // /market 自选操作列：加入/移除自选
  watchlistInstrumentIds?: Set<string>
  onToggleWatchlist?: (row: TrendSelectionRow, add: boolean) => void
  watchlistPendingIds?: Set<string>
  /**
   * [CHANGE-20260729-009] 股票名称旁内联 +/- 自选按钮（22×22）。
   * - true: renderStock 在名称右侧渲染 +/- 按钮，且不返回独立 action 列
   * - false/省略: 保持原有独立 action 列行为（其他复用页面不受影响）
   */
  inlineWatchlistToggle?: boolean
}

/** 股票列渲染（复用）：第一行=名称（可点击按钮）+/- 自选按钮，第二行=代码·市场 */
function renderStock(
  row: TrendSelectionRow,
  onNavigate?: (row: TrendSelectionRow) => void,
  inlineWatchlist?: {
    watched: boolean
    pending: boolean
    onToggle: (row: TrendSelectionRow, add: boolean) => void
  },
): ReactNode {
  const { name, symbol, market } = getStockDisplay(row)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="symbol">
          {onNavigate ? (
            <button
              type="button"
              className="stock-name-btn"
              onClick={(e) => { e.stopPropagation(); onNavigate(row) }}
              aria-label={`查看${name}详情`}
            >
              {name}
              <span className="stock-name-arrow" aria-hidden="true">›</span>
            </button>
          ) : name}
        </div>
        <div className="symbol-sub">
          {symbol}
          {market ? ` · ${market}` : ''}
        </div>
      </div>
      {inlineWatchlist && (
        <button
          type="button"
          className="btn inline-watchlist-btn"
          onClick={(e) => {
            e.stopPropagation()
            inlineWatchlist.onToggle(row, !inlineWatchlist.watched)
          }}
          disabled={inlineWatchlist.pending}
          aria-label={inlineWatchlist.watched ? '移除自选' : '加入自选'}
          aria-pressed={inlineWatchlist.watched}
          title={inlineWatchlist.watched ? '移除自选' : '加入自选'}
          style={{
            width: 22,
            height: 22,
            minWidth: 22,
            padding: 0,
            border: '1px solid #2a3042',
            borderRadius: 4,
            background: inlineWatchlist.watched ? '#1f6feb' : 'transparent',
            color: inlineWatchlist.watched ? '#fff' : '#8ea0b7',
            fontSize: 14,
            lineHeight: '20px',
            cursor: inlineWatchlist.pending ? 'wait' : 'pointer',
            flexShrink: 0,
          }}
        >
          {inlineWatchlist.pending ? '…' : (inlineWatchlist.watched ? '−' : '+')}
        </button>
      )}
    </div>
  )
}

/**
 * [趋势选股] - 描述: 趋势选股统一列定义（spec 第七节唯一实现）
 * [CHANGE-20260731-REMOVE-DSA] 删除所有旧 DSA-only 列：趋势、连续天、VWAP差、段涨跌、斜率、
 *   强度、主要结构、短线结构、对齐、OB数、事件、新鲜度、动量。
 *   列表只保留：股票/价格/涨跌/行业等基础列 + 第一金字塔列（由 MarketWorkspacePage 追加） + 自选操作。
 * 主页与 ScreenerPage 共用；主页通过 visibleColumnKeys 显示子集
 * 同 key 的 title/unit/format/颜色规则完全一致，禁止页面层覆盖
 */
export function getTrendSelectionColumns(
  options: TrendSelectionColumnOptions = {},
): DataTableColumn<TrendSelectionRow>[] {
  const {
    onAddToWatchlist,
    onDetail,
    addPending = false,
    onNavigateToStock,
    watchlistInstrumentIds,
    onToggleWatchlist,
    watchlistPendingIds,
    inlineWatchlistToggle = false,
  } = options

  // [CHANGE-20260729-009] inlineWatchlistToggle=true 时，股票名称旁渲染 +/- 按钮，
  // 且不返回独立 action 列（/market 专用）。其他页面不受影响。
  const buildInlineWatchlist = (row: TrendSelectionRow) => {
    if (!inlineWatchlistToggle || !onToggleWatchlist) return undefined
    const instrumentId = row.instrumentId
    return {
      watched: watchlistInstrumentIds?.has(instrumentId) ?? false,
      pending: watchlistPendingIds?.has(instrumentId) ?? false,
      onToggle: onToggleWatchlist,
    }
  }

  const columns: DataTableColumn<TrendSelectionRow>[] = [
    {
      key: 'stock',
      title: '股票',
      dataType: 'text',
      sortable: true,
      filterable: true,
      width: inlineWatchlistToggle ? 170 : 150,
      sortValue: (row) => getStockDisplay(row).name,
      filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
      render: (row) => renderStock(row, onNavigateToStock, buildInlineWatchlist(row)),
    },
    {
      // CHANGE-20260714-001: 当日涨跌幅独立列
      // 数据源：latest_change_pct（从 bars_daily 最新两根日线计算，与 DSA run payload 分离）
      // 无两根有效日线显示 "--"，不得静默回退旧 run 值
      // 表头 tooltip 显示"最新完成交易日"，单元格 title 显示具体 trade_date
      key: 'change_pct',
      title: '当日涨跌幅',
      shortTitle: '涨跌幅',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 86,
      sortValue: (row) => Number(row.latestChangePct ?? 0),
      render: (row) => {
        const v = row.latestChangePct
        const td = row.latestChangeTradeDate
        if (v === null || v === undefined) {
          return <span className="market-flat" title={td ?? undefined}>--</span>
        }
        return (
          <span className={changePctColorClass(v)} title={td ?? undefined}>
            {fmtChange(v)}
          </span>
        )
      },
    },
    {
      // [CHANGE-20260731-REMOVE-DSA] 保留最新价独立列（数据源：latest_price 直接从 row 取，不再读旧 DSA payload）
      key: 'price',
      title: '最新价格',
      shortTitle: '现价',
      dataType: 'number',
      sortable: true,
      filterable: true,
      width: 76,
      sortValue: (row) => Number(row.latestPrice ?? 0),
      render: (row) => {
        const v = row.latestPrice
        if (v === null || v === undefined) return <span className="market-flat">--</span>
        return <span>{Number(v).toFixed(2)}</span>
      },
    },
    {
      key: 'industry',
      title: '行业',
      shortTitle: '行业',
      dataType: 'text',
      sortable: true,
      filterable: true,
      width: 90,
      sortValue: (row) => row.industry ?? '',
      filterValue: (row) => row.industry ?? '',
      render: (row) => row.industry ?? <span className="market-flat">--</span>,
    },
    {
      // [趋势选股] - 描述: /market 操作列改名"自选"（onToggleWatchlist 模式）
      // 旧版 onAddToWatchlist（主页）和 onDetail（ScreenerPage）作为兼容保留
      // stopPropagation 防止按钮点击冒泡到 <tr onClick>，避免行选中副作用
      key: 'action',
      title: onToggleWatchlist ? '自选' : '操作',
      dataType: 'text',
      sortable: false,
      filterable: false,
      width: 76,
      isAction: true,
      render: (row) => {
        // /market 自选操作：加入/移除自选（按 instrument_id 维护 watched/pending 状态）
        if (onToggleWatchlist) {
          const instrumentId = row.instrumentId
          const watched = watchlistInstrumentIds?.has(instrumentId) ?? false
          const pending = watchlistPendingIds?.has(instrumentId) ?? false
          return watched ? (
            <button
              className="btn small"
              onClick={(e) => { e.stopPropagation(); onToggleWatchlist(row, false) }}
              disabled={pending}
              title="移除自选"
            >
              {pending ? '…' : '移除自选'}
            </button>
          ) : (
            <button
              className="btn small"
              onClick={(e) => { e.stopPropagation(); onToggleWatchlist(row, true) }}
              disabled={pending}
              title="加入自选"
            >
              {pending ? '…' : '加入自选'}
            </button>
          )
        }
        // 主页兼容：onAddToWatchlist 单按钮模式
        if (onAddToWatchlist) {
          return row.watched ? (
            <span className="tag info">已自选</span>
          ) : (
            <button
              className="btn small"
              onClick={(e) => { e.stopPropagation(); onAddToWatchlist(row) }}
              disabled={addPending}
            >
              ＋ 自选
            </button>
          )
        }
        // ScreenerPage 兼容：onDetail 详情按钮
        if (onDetail) {
          return (
            <div className="actions">
              <button className="btn small" onClick={(e) => { e.stopPropagation(); onDetail(row) }}>
                详情
              </button>
            </div>
          )
        }
        return null
      },
    },
  ]

  // [CHANGE-20260729-009] inlineWatchlistToggle=true 时移除独立 action 列
  // （+/- 按钮已内联到股票名称旁，/market 专用；其他页面不受影响）
  if (inlineWatchlistToggle) {
    return columns.filter((c) => c.key !== 'action')
  }

  return columns
}
