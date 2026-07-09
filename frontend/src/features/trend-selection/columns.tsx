// [趋势选股] - 桌面端表格列定义
// 职责：提供唯一列定义，首页"最新趋势快照"与趋势选股页共用
// 唯一性：spec 第七节要求 features/trend-selection 是趋势选股列定义唯一实现
//         禁止 IndexPage/ScreenerPage 重新定义同字段列
import type { ReactNode } from 'react'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import type { TrendSelectionRow } from './types'
import {
  pickPayload,
  toNum,
  fmtNum,
  fmtPct,
  fmtRatioAsPct,
  fmtChange,
  changePctColorClass,
  getStockDisplay,
  DIR_BARS_KEYS,
  VWAP_RET_AVG_KEYS,
  VWAP_RET_TOTAL_KEYS,
  OFFSET_MEAN_KEYS,
  OFFSET_PERCENTILE_KEYS,
} from './adapters'

// [趋势选股] - 描述: 仅供 columns.tsx 内部使用的候选 key（不导出，不散落到页面）
// 趋势附近波动幅度（后端存储为小数，显示需 ×100）
const OFFSET_STD_KEYS = ['offset_std', 'shift_std'] as const
// 趋势参考价（VWAP）
const DSA_VWAP_KEYS = ['dsa_vwap', 'vwap', 'anchor_vwap'] as const
// 距趋势参考价偏差（后端存储为百分比数值，不 ×100）
const DSA_VWAP_DEV_PCT_KEYS = ['dsa_vwap_dev_pct', 'vwap_dev_pct', 'close_vwap_dev_pct'] as const
// 趋势波动程度（后端存储为百分比数值，不 ×100）
const OFFSET_VARIANCE_RATE_KEYS = ['offset_variance_rate', 'offset_var_rate', 'shift_var'] as const
// 最新价格
const PRICE_KEYS = ['last_close', 'price', 'current_price', 'close'] as const
// 涨跌幅（后端存储为百分比数值，不 ×100，用于股票列展示）
const CHANGE_PCT_KEYS = ['change_pct', 'pct_change', 'change_percent'] as const

export interface TrendSelectionColumnOptions {
  // 主页操作列：加入自选（提供时操作列渲染为"已自选/+ 自选"）
  onAddToWatchlist?: (row: TrendSelectionRow) => void
  addPending?: boolean
  // 趋势选股页操作列：查看详情（提供时操作列渲染为"详情"按钮）
  onDetail?: (row: TrendSelectionRow) => void
}

/** 股票列渲染（复用）：第一行=名称+涨跌幅（涨红跌绿），第二行=代码·市场 */
function renderStock(row: TrendSelectionRow): ReactNode {
  const { name, symbol, market } = getStockDisplay(row)
  const changePct = pickPayload(row.payload, CHANGE_PCT_KEYS)
  return (
    <div>
      <div className="symbol">
        {name}
        <span className={changePctColorClass(changePct)} style={{ marginLeft: 6 }}>
          {fmtChange(changePct)}
        </span>
      </div>
      <div className="symbol-sub">
        {symbol}
        {market ? ` · ${market}` : ''}
      </div>
    </div>
  )
}

/** 趋势列渲染：上涨 N天 / 下跌 N天 / 方向未形成（涨红跌绿） */
function renderDirBars(row: TrendSelectionRow): ReactNode {
  const v = pickPayload(row.payload, DIR_BARS_KEYS)
  const n = toNum(v)
  if (n === null || n === 0) {
    return <span className="market-flat">方向未形成</span>
  }
  if (n > 0) {
    return <span className="market-up">上涨 {n.toFixed(0)}天</span>
  }
  return <span className="market-down">下跌 {Math.abs(n).toFixed(0)}天</span>
}

/**
 * [趋势选股] - 描述: 趋势选股统一列定义（spec 第七节唯一实现）
 * 主页与 ScreenerPage 共用；主页通过 visibleColumnKeys 显示子集
 * 同 key 的 title/unit/format/颜色规则完全一致，禁止页面层覆盖
 */
export function getTrendSelectionColumns(
  options: TrendSelectionColumnOptions = {},
): DataTableColumn<TrendSelectionRow>[] {
  const { onAddToWatchlist, onDetail, addPending = false } = options

  const columns: DataTableColumn<TrendSelectionRow>[] = [
    {
      key: 'stock',
      title: '股票',
      dataType: 'text',
      sortable: true,
      filterable: false,
      width: 150,
      sortValue: (row) => getStockDisplay(row).name,
      filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
      render: renderStock,
    },
    {
      // [趋势选股] - 描述: 当日涨跌幅独立列（后端存储为百分比数值，不 ×100；筛选输入 3% 传 3）
      key: 'change_pct',
      title: '当日涨跌幅',
      shortTitle: '涨跌幅',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 86,
      sortValue: (row) => Number(pickPayload(row.payload, CHANGE_PCT_KEYS) ?? 0),
      render: (row) => {
        const v = pickPayload(row.payload, CHANGE_PCT_KEYS)
        return <span className={changePctColorClass(v)}>{fmtChange(v)}</span>
      },
    },
    {
      // [趋势选股] - 描述: 趋势列 key=dsa_dir_bars，筛选直接透传后端 metric_filters（多头>0/空头<0/持续天数）
      key: 'dsa_dir_bars',
      title: '当前趋势',
      shortTitle: '趋势',
      dataType: 'number',
      sortable: true,
      filterable: true,
      width: 90,
      sortValue: (row) => {
        const v = pickPayload(row.payload, DIR_BARS_KEYS)
        const n = toNum(v)
        return n === null ? 0 : Math.abs(n)
      },
      render: renderDirBars,
    },
    {
      key: 'vwap_ret_avg',
      title: '日均趋势变化',
      shortTitle: '日均',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 88,
      sortValue: (row) => Number(pickPayload(row.payload, VWAP_RET_AVG_KEYS) ?? 0),
      render: (row) => {
        const v = pickPayload(row.payload, VWAP_RET_AVG_KEYS)
        return <span className={changePctColorClass(v)}>{fmtRatioAsPct(v)}</span>
      },
    },
    {
      key: 'vwap_ret_total',
      title: '本轮趋势涨跌',
      shortTitle: '累计',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 88,
      sortValue: (row) => Number(pickPayload(row.payload, VWAP_RET_TOTAL_KEYS) ?? 0),
      render: (row) => {
        const v = pickPayload(row.payload, VWAP_RET_TOTAL_KEYS)
        return <span className={changePctColorClass(v)}>{fmtRatioAsPct(v)}</span>
      },
    },
    {
      key: 'offset_mean',
      title: '平均偏离趋势线',
      shortTitle: '均偏',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 90,
      sortValue: (row) => Number(pickPayload(row.payload, OFFSET_MEAN_KEYS) ?? 0),
      render: (row) => {
        const v = pickPayload(row.payload, OFFSET_MEAN_KEYS)
        return <span className={changePctColorClass(v)}>{fmtRatioAsPct(v)}</span>
      },
    },
    {
      key: 'offset_std',
      title: '趋势附近波动幅度',
      shortTitle: '波动',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 90,
      sortValue: (row) => Number(pickPayload(row.payload, OFFSET_STD_KEYS) ?? 0),
      render: (row) => fmtRatioAsPct(pickPayload(row.payload, OFFSET_STD_KEYS)),
    },
    {
      key: 'offset_percentile',
      title: '当前强弱位置',
      shortTitle: '分位',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 86,
      sortValue: (row) => Number(pickPayload(row.payload, OFFSET_PERCENTILE_KEYS) ?? 0),
      render: (row) => fmtRatioAsPct(pickPayload(row.payload, OFFSET_PERCENTILE_KEYS)),
    },
    {
      key: 'dsa_vwap',
      title: '趋势参考价',
      shortTitle: '参考价',
      dataType: 'number',
      sortable: true,
      filterable: true,
      width: 82,
      sortValue: (row) => Number(pickPayload(row.payload, DSA_VWAP_KEYS) ?? 0),
      render: (row) => fmtNum(pickPayload(row.payload, DSA_VWAP_KEYS), 2),
    },
    {
      key: 'dsa_vwap_dev_pct',
      title: '距趋势参考价',
      shortTitle: '价差',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 86,
      sortValue: (row) => Number(pickPayload(row.payload, DSA_VWAP_DEV_PCT_KEYS) ?? 0),
      render: (row) => {
        const v = pickPayload(row.payload, DSA_VWAP_DEV_PCT_KEYS)
        return <span className={changePctColorClass(v)}>{fmtPct(v)}</span>
      },
    },
    {
      key: 'offset_variance_rate',
      title: '趋势波动程度',
      shortTitle: '变异',
      dataType: 'percent',
      sortable: true,
      filterable: true,
      width: 88,
      sortValue: (row) => Number(pickPayload(row.payload, OFFSET_VARIANCE_RATE_KEYS) ?? 0),
      render: (row) => fmtPct(pickPayload(row.payload, OFFSET_VARIANCE_RATE_KEYS)),
    },
    {
      key: 'price',
      title: '最新价格',
      shortTitle: '现价',
      dataType: 'number',
      sortable: true,
      filterable: true,
      width: 76,
      sortValue: (row) => Number(pickPayload(row.payload, PRICE_KEYS) ?? 0),
      render: (row) => fmtNum(pickPayload(row.payload, PRICE_KEYS)),
    },
    {
      key: 'action',
      title: '操作',
      dataType: 'text',
      sortable: false,
      filterable: false,
      width: 60,
      isAction: true,
      render: (row) => {
        // [趋势选股] - 描述: 主页操作列=加入自选，ScreenerPage 操作列=查看详情
        if (onAddToWatchlist) {
          return row.watched ? (
            <span className="tag info">已自选</span>
          ) : (
            <button
              className="btn small"
              onClick={() => onAddToWatchlist(row)}
              disabled={addPending}
            >
              ＋ 自选
            </button>
          )
        }
        if (onDetail) {
          return (
            <div className="actions">
              <button className="btn small" onClick={() => onDetail(row)}>
                详情
              </button>
            </div>
          )
        }
        return null
      },
    },
  ]

  return columns
}
