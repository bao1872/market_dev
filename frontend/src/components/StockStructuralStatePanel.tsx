// [结构状态因子层] - 描述: 个股结构状态因子面板（双周期 1d/15m，5 组因子）
// 用法：在 StockDetailPage 右侧栏渲染，<StockStructuralStatePanel instrumentId={id} />
// 契约：前端只渲染后端 DTO，严禁重新计算因子。所有因子由后端 structural_factor_service 计算。
// 数据源：useStructuralFactors hook → GET /api/v1/instruments/{id}/structural-factors
// 降级策略：API 失败显示"暂无数据"；null 字段显示"-"；degraded_reasons 显示警告条
import { useState, type ReactNode } from 'react'
import { useStructuralFactors } from '../hooks/useApi'
import type { StructuralFactorResponse } from '../api/endpoints'

interface StockStructuralStatePanelProps {
  instrumentId: string
}

// 因子组键名（与后端 _compute_all_factors_for_bars 返回结构对齐）
type FactorGroup = 'dsa_segment' | 'swing_position' | 'cost_position' | 'volatility_momentum' | 'participation'

interface FactorRow {
  label: string
  key: string
  // 可选格式化器：默认按数字格式化
  format?: (value: unknown) => string
}

// ===== 格式化辅助 =====
function fmt(value: unknown, digits = 4): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'number') {
    if (!isFinite(value)) return '-'
    return value.toFixed(digits)
  }
  return String(value)
}

function fmtPrice(value: unknown): string {
  return fmt(value, 2)
}

function fmtPct(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'number' && isFinite(value)) {
    return (value * 100).toFixed(2) + '%'
  }
  return '-'
}

function fmtInt(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'number' && isFinite(value)) {
    return Math.round(value).toString()
  }
  return '-'
}

function fmtDir(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (value === 1 || value === '1') return '上升'
  if (value === -1 || value === '-1') return '下降'
  return String(value)
}

// ===== 卡片配置 =====
const CARDS: Array<{ title: string; group: FactorGroup; rows: FactorRow[] }> = [
  {
    title: 'DSA 段质量',
    group: 'dsa_segment',
    rows: [
      { label: '段 ID', key: 'segment_id', format: fmtInt },
      { label: '方向', key: 'segment_dir', format: fmtDir },
      { label: '起始价', key: 'segment_start_price', format: fmtPrice },
      { label: '持续 bar', key: 'age_bars', format: fmtInt },
      { label: '幅度/ATR', key: 'segment_extents_pct', format: (v) => fmt(v, 2) },
    ],
  },
  {
    title: 'Swing 结构位置',
    group: 'swing_position',
    rows: [
      { label: '最近 swing high', key: 'confirmed_swing_high', format: fmtPrice },
      { label: '最近 swing low', key: 'confirmed_swing_low', format: fmtPrice },
      { label: '距 high bar 数', key: 'bars_since_swing_high', format: fmtInt },
      { label: '距 low bar 数', key: 'bars_since_swing_low', format: fmtInt },
      { label: 'close vs high', key: 'swing_high_to_close_pct', format: fmtPct },
      { label: 'close vs low', key: 'swing_low_to_close_pct', format: fmtPct },
    ],
  },
  {
    title: '成本/节点',
    group: 'cost_position',
    rows: [
      { label: 'POC', key: 'poc_price', format: fmtPrice },
      { label: '上方节点', key: 'nearest_upper_node', format: (v) => {
        if (v === null || v === undefined) return '-'
        if (typeof v === 'object' && v !== null && 'price_mid' in v) {
          return fmtPrice((v as { price_mid: number }).price_mid)
        }
        return '-'
      } },
      { label: '下方节点', key: 'nearest_lower_node', format: (v) => {
        if (v === null || v === undefined) return '-'
        if (typeof v === 'object' && v !== null && 'price_mid' in v) {
          return fmtPrice((v as { price_mid: number }).price_mid)
        }
        return '-'
      } },
      { label: '位置 [0,1]', key: 'position_0_1', format: (v) => fmt(v, 3) },
      { label: 'close vs POC', key: 'close_to_poc_pct', format: fmtPct },
    ],
  },
  {
    title: '动量/波动',
    group: 'volatility_momentum',
    rows: [
      { label: 'BB %B', key: 'bb_percent_b', format: (v) => fmt(v, 3) },
      { label: 'BB 带宽', key: 'bb_bandwidth_pct', format: (v) => fmt(v, 3) },
      { label: 'BB 带宽分位', key: 'bb_bandwidth_percentile', format: fmtPct },
      { label: 'SQZMOM val', key: 'sqzmom_val', format: (v) => fmt(v, 4) },
      { label: 'SQZMOM Δ1', key: 'sqzmom_delta_1', format: (v) => fmt(v, 4) },
      { label: 'SQZMOM 分位', key: 'sqzmom_percentile', format: fmtPct },
    ],
  },
  {
    title: '成交参与',
    group: 'participation',
    rows: [
      { label: '量比 20', key: 'volume_ratio_20', format: (v) => fmt(v, 3) },
      { label: '量能 120 分位', key: 'volume_percentile_120', format: fmtPct },
    ],
  },
]

// ===== 子组件：单张卡片 =====
function FactorCard({
  title,
  factors,
  rows,
}: {
  title: string
  factors: Record<string, unknown> | null
  rows: FactorRow[]
}): ReactNode {
  return (
    <div className="ssp-card">
      <div className="ssp-card-title">{title}</div>
      <div className="ssp-card-body">
        {rows.map((row) => {
          const value = factors ? factors[row.key] : null
          const formatted = row.format ? row.format(value) : fmt(value)
          return (
            <div className="ssp-row" key={row.key}>
              <span className="ssp-label">{row.label}</span>
              <span className="ssp-value">{formatted}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ===== 主组件 =====
export function StockStructuralStatePanel({
  instrumentId,
}: StockStructuralStatePanelProps): ReactNode {
  const [activeTab, setActiveTab] = useState<'primary' | 'secondary'>('primary')
  const query = useStructuralFactors(instrumentId)

  // 加载中
  if (query.isLoading || query.isPending) {
    return (
      <div className="ssp-panel">
        <div className="ssp-header">
          <span className="ssp-title">结构状态</span>
        </div>
        <div className="ssp-loading">加载中...</div>
      </div>
    )
  }

  // API 失败
  if (query.isError || !query.data) {
    return (
      <div className="ssp-panel">
        <div className="ssp-header">
          <span className="ssp-title">结构状态</span>
        </div>
        <div className="ssp-empty">暂无数据</div>
      </div>
    )
  }

  const data: StructuralFactorResponse = query.data
  const primaryTimeframe = Object.keys(data.primary)[0] ?? '1d'
  const secondaryTimeframe = Object.keys(data.secondary)[0] ?? '15m'

  const activeTimeframe = activeTab === 'primary' ? primaryTimeframe : secondaryTimeframe
  const activeFactors =
    activeTab === 'primary'
      ? (data.primary[primaryTimeframe] ?? null)
      : (data.secondary[secondaryTimeframe] ?? null)

  return (
    <div className="ssp-panel">
      {/* 头部：标题 + as_of */}
      <div className="ssp-header">
        <span className="ssp-title">结构状态</span>
        <span className="ssp-as-of">{data.meta.as_of ?? '-'}</span>
      </div>

      {/* 降级提示 */}
      {data.meta.degraded_reasons.length > 0 && (
        <div className="ssp-degraded" title={data.meta.degraded_reasons.join('; ')}>
          ⚠ {data.meta.degraded_reasons.length} 项降级
        </div>
      )}

      {/* 双周期 tabs */}
      <div className="ssp-tabs">
        <button
          type="button"
          className={activeTab === 'primary' ? 'ssp-tab active' : 'ssp-tab'}
          onClick={() => setActiveTab('primary')}
        >
          {primaryTimeframe}
        </button>
        <span className="ssp-tab-sep">|</span>
        <button
          type="button"
          className={activeTab === 'secondary' ? 'ssp-tab active' : 'ssp-tab'}
          onClick={() => setActiveTab('secondary')}
        >
          {secondaryTimeframe}
        </button>
      </div>

      {/* 5 张卡片 */}
      <div className="ssp-cards">
        {CARDS.map((card) => (
          <FactorCard
            key={card.group}
            title={card.title}
            factors={activeFactors ? (activeFactors[card.group] as Record<string, unknown> | null) : null}
            rows={card.rows}
          />
        ))}
      </div>

      {/* 对比关系 */}
      {(data.relation.trend_alignment || data.relation.momentum_alignment) && (
        <div className="ssp-relation">
          <div className="ssp-row">
            <span className="ssp-label">趋势一致性</span>
            <span className="ssp-value">{data.relation.trend_alignment ?? '-'}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">动量一致性</span>
            <span className="ssp-value">{data.relation.momentum_alignment ?? '-'}</span>
          </div>
        </div>
      )}

      {/* 明细折叠 */}
      <details className="ssp-detail">
        <summary>结构因子明细 ({activeTimeframe})</summary>
        <pre className="ssp-detail-pre">
          {JSON.stringify(activeFactors, null, 2)}
        </pre>
      </details>
    </div>
  )
}

export default StockStructuralStatePanel
