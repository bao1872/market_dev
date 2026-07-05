// [结构状态因子层] - 描述: 个股结构状态因子面板（双周期 1d/15m，5 组因子）
// 用法：在 StockDetailPage 右侧栏渲染，<StockStructuralStatePanel instrumentId={id} />
// 契约：前端只渲染后端 DTO，严禁重新计算因子。所有因子由后端 structural_factor_service 计算。
// 数据源：useStructuralFactors hook → GET /api/v1/instruments/{id}/structural-factors
// 降级策略：API 失败显示"暂无数据"；null 字段显示"-"；degraded_reasons 显示警告条
// [时序特征 V1] - 描述: 面板末尾折叠卡片，渲染 temporal-features API DTO（受同一个结构状态开关控制）
import { useState, type ReactNode } from 'react'
import { useStructuralFactors, useTemporalFeatures } from '../hooks/useApi'
import type { StructuralFactorResponse, TemporalFeaturesResponse } from '../api/endpoints'

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

// [V1.8] - 描述: bool 字段格式化为 是/否
function fmtBool(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

// ===== 卡片配置（V1.8 扩展字段）=====
const CARDS: Array<{ title: string; group: FactorGroup; rows: FactorRow[] }> = [
  {
    title: 'DSA 段质量',
    group: 'dsa_segment',
    rows: [
      // V1.7 保留字段
      { label: '段 ID', key: 'segment_id', format: fmtInt },
      { label: '方向', key: 'segment_dir', format: fmtDir },
      { label: '起始价', key: 'segment_start_price', format: fmtPrice },
      { label: '持续 bar', key: 'age_bars', format: fmtInt },
      { label: '幅度/ATR', key: 'segment_extents_pct', format: (v) => fmt(v, 2) },
      // V1.8 基础字段
      { label: 'DSA VWAP', key: 'dsa_value', format: fmtPrice },
      { label: 'close vs DSA / ATR', key: 'price_vs_dsa_atr', format: (v) => fmt(v, 3) },
      // V1.8 当前段字段
      { label: '当前段 ID', key: 'current_dsa_segment_id', format: fmtInt },
      { label: '当前段方向', key: 'current_dsa_segment_dir', format: fmtDir },
      { label: '当前段持续 bar', key: 'current_dsa_segment_age_bars', format: fmtInt },
      { label: '当前段收益 %', key: 'current_dsa_segment_return_pct', format: fmtPct },
      { label: '当前段斜率 %/bar', key: 'current_dsa_segment_slope_pct_per_bar', format: (v) => fmt(v, 4) },
      { label: '当前段斜率 ATR/bar', key: 'current_dsa_segment_slope_atr_per_bar', format: (v) => fmt(v, 4) },
      { label: '当前段效率', key: 'current_dsa_segment_efficiency_0_1', format: (v) => fmt(v, 3) },
      { label: '当前段成交量', key: 'current_segment_volume_sum', format: fmtInt },
      // V1.8 前一段字段
      { label: '前段方向', key: 'prev_dsa_segment_dir', format: fmtDir },
      { label: '前段持续 bar', key: 'prev_dsa_segment_age_bars', format: fmtInt },
      { label: '前段收益 %', key: 'prev_dsa_segment_return_pct', format: fmtPct },
      { label: '前段效率', key: 'prev_dsa_segment_efficiency_0_1', format: (v) => fmt(v, 3) },
      // V1.8 段间对比字段
      { label: '收益绝对比', key: 'segment_return_abs_ratio', format: (v) => fmt(v, 3) },
      { label: '斜率绝对比', key: 'segment_slope_abs_ratio', format: (v) => fmt(v, 3) },
      { label: '持续时长比', key: 'segment_duration_ratio', format: (v) => fmt(v, 3) },
      { label: '效率差值', key: 'segment_efficiency_delta', format: (v) => fmt(v, 4) },
      { label: '当前/前段量比', key: 'current_vs_prev_volume_ratio', format: (v) => fmt(v, 3) },
      { label: '当前段收益/量', key: 'current_segment_return_per_volume', format: (v) => fmt(v, 8) },
      { label: '前段收益/量', key: 'prev_segment_return_per_volume', format: (v) => fmt(v, 8) },
      { label: '收益/量比', key: 'return_per_volume_ratio', format: (v) => fmt(v, 3) },
      { label: '每1%收益量', key: 'volume_per_1pct_return', format: fmtInt },
    ],
  },
  {
    title: 'Swing 结构位置',
    group: 'swing_position',
    rows: [
      // V1.7 保留字段
      { label: '最近 swing high', key: 'confirmed_swing_high', format: fmtPrice },
      { label: '最近 swing low', key: 'confirmed_swing_low', format: fmtPrice },
      { label: '距 high bar 数', key: 'bars_since_swing_high', format: fmtInt },
      { label: '距 low bar 数', key: 'bars_since_swing_low', format: fmtInt },
      { label: 'close vs high', key: 'swing_high_to_close_pct', format: fmtPct },
      { label: 'close vs low', key: 'swing_low_to_close_pct', format: fmtPct },
      // V1.8 新增字段
      { label: 'Swing 范围', key: 'swing_range', format: fmtPrice },
      { label: '位置 [0,1]', key: 'price_position_in_swing_0_1', format: (v) => fmt(v, 3) },
      { label: '距 high / ATR', key: 'distance_to_swing_high_atr', format: (v) => fmt(v, 3) },
      { label: '距 low / ATR', key: 'distance_to_swing_low_atr', format: (v) => fmt(v, 3) },
      { label: '高点回撤', key: 'retracement_from_high_0_1', format: (v) => fmt(v, 3) },
      { label: '低点反弹', key: 'rebound_from_low_0_1', format: (v) => fmt(v, 3) },
    ],
  },
  {
    title: '成本/节点',
    group: 'cost_position',
    rows: [
      // V1.7 保留字段
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
      // V1.8 新增字段
      { label: 'close vs POC / ATR', key: 'price_vs_poc_atr', format: (v) => fmt(v, 3) },
      { label: 'VA 位置 [0,1]', key: 'value_area_position_0_1', format: (v) => fmt(v, 3) },
      { label: '上方节点价', key: 'nearest_node_above_price', format: fmtPrice },
      { label: '下方节点价', key: 'nearest_node_below_price', format: fmtPrice },
      { label: '距上方节点 / ATR', key: 'distance_to_node_above_atr', format: (v) => fmt(v, 3) },
      { label: '距下方节点 / ATR', key: 'distance_to_node_below_atr', format: (v) => fmt(v, 3) },
      { label: '上方节点强度', key: 'node_above_strength', format: fmtInt },
      { label: '下方节点强度', key: 'node_below_strength', format: fmtInt },
    ],
  },
  {
    title: '动量/波动',
    group: 'volatility_momentum',
    rows: [
      // V1.7 保留字段
      { label: 'BB %B', key: 'bb_percent_b', format: (v) => fmt(v, 3) },
      { label: 'BB 带宽', key: 'bb_bandwidth_pct', format: (v) => fmt(v, 3) },
      { label: 'BB 带宽分位', key: 'bb_bandwidth_percentile', format: fmtPct },
      { label: 'SQZMOM val', key: 'sqzmom_val', format: (v) => fmt(v, 4) },
      { label: 'SQZMOM Δ1', key: 'sqzmom_delta_1', format: (v) => fmt(v, 4) },
      { label: 'SQZMOM 分位', key: 'sqzmom_percentile', format: fmtPct },
      // V1.8 新增字段
      { label: '距 BB 上轨 / ATR', key: 'distance_to_bb_upper_atr', format: (v) => fmt(v, 3) },
      { label: '距 BB 下轨 / ATR', key: 'distance_to_bb_lower_atr', format: (v) => fmt(v, 3) },
      { label: 'SQZMOM 绝对分位', key: 'sqzmom_abs_percentile', format: fmtPct },
      { label: 'Squeeze On', key: 'sqz_on', format: fmtBool },
      { label: 'Squeeze Off', key: 'sqz_off', format: fmtBool },
    ],
  },
  {
    title: '成交参与',
    group: 'participation',
    rows: [
      // V1.7 保留字段
      { label: '量比 20', key: 'volume_ratio_20', format: (v) => fmt(v, 3) },
      { label: '量能 120 分位', key: 'volume_percentile_120', format: fmtPct },
      // V1.8 段级成交量字段（从 dsa_segment 共享）
      { label: '当前段成交量', key: 'current_segment_volume_sum', format: fmtInt },
      { label: '前段成交量', key: 'prev_segment_volume_sum', format: fmtInt },
      { label: '当前/前段量比', key: 'current_vs_prev_volume_ratio', format: (v) => fmt(v, 3) },
      { label: '当前段收益/量', key: 'current_segment_return_per_volume', format: (v) => fmt(v, 8) },
      { label: '前段收益/量', key: 'prev_segment_return_per_volume', format: (v) => fmt(v, 8) },
      { label: '收益/量比', key: 'return_per_volume_ratio', format: (v) => fmt(v, 3) },
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

// ===== Temporal Features V1 折叠卡片 =====
// 渲染 temporal-features API DTO（daily_context + m15_response + derived_relation + meta）
// 前端只渲染 DTO，严禁重新计算。null 字段显示 "-"。
const TEMPORAL_DAILY_ROWS: FactorRow[] = [
  { label: 'DSA 方向', key: 'daily_dsa_dir', format: fmtDir },
  { label: '段持续分位', key: 'daily_dsa_segment_duration_percentile', format: fmtPct },
  { label: '段斜率 ATR/bar', key: 'daily_dsa_slope_atr_per_bar', format: (v) => fmt(v, 4) },
  { label: '段效率', key: 'daily_dsa_efficiency_0_1', format: (v) => fmt(v, 3) },
  { label: 'Swing 位置 [0,1]', key: 'daily_price_position_in_swing_0_1', format: (v) => fmt(v, 3) },
  { label: '距 high / ATR', key: 'daily_distance_to_swing_high_atr', format: (v) => fmt(v, 3) },
  { label: '距上方节点 / ATR', key: 'daily_distance_to_node_above_atr', format: (v) => fmt(v, 3) },
  { label: 'SQZMOM 段内变化', key: 'daily_sqzmom_change_since_segment_start', format: (v) => fmt(v, 4) },
  { label: '量能分位段内变化', key: 'daily_volume_percentile_change_since_segment_start', format: fmtPct },
]

const TEMPORAL_M15_ROWS: FactorRow[] = [
  { label: 'Swing 位置 [0,1]', key: 'm15_price_position_in_swing_0_1', format: (v) => fmt(v, 3) },
  { label: '位置 anchor 后变化', key: 'm15_position_change_since_swing_anchor', format: (v) => fmt(v, 3) },
  { label: '距 high / ATR', key: 'm15_distance_to_swing_high_atr', format: (v) => fmt(v, 3) },
  { label: '距 low / ATR', key: 'm15_distance_to_swing_low_atr', format: (v) => fmt(v, 3) },
  { label: 'SQZMOM anchor 后变化', key: 'm15_sqzmom_change_since_swing_anchor', format: (v) => fmt(v, 4) },
  { label: 'SQZMOM 绝对分位', key: 'm15_sqzmom_abs_percentile', format: fmtPct },
  { label: 'Squeeze Off', key: 'm15_sqz_off', format: fmtBool },
  { label: 'BB 带宽 anchor 后变化', key: 'm15_bb_bandwidth_change_since_swing_anchor', format: (v) => fmt(v, 3) },
  { label: '量能分位 anchor 后变化', key: 'm15_volume_percentile_change_since_swing_anchor', format: fmtPct },
]

const TEMPORAL_DERIVED_ROWS: FactorRow[] = [
  { label: 'm15 vs daily 位置', key: 'm15_position_relative_to_daily', format: (v) => fmt(v, 3) },
  { label: '响应方向', key: 'm15_response_direction_relative_to_daily', format: (v) => fmt(v, 0) },
  { label: '响应强度', key: 'm15_response_intensity', format: (v) => fmt(v, 3) },
]

function TemporalFeaturesCard({
  instrumentId,
}: {
  instrumentId: string
}): ReactNode {
  const query = useTemporalFeatures(instrumentId)

  // 加载中
  if (query.isLoading || query.isPending) {
    return (
      <details className="ssp-detail ssp-temporal" open>
        <summary>时序特征 V1</summary>
        <div className="ssp-loading">加载中...</div>
      </details>
    )
  }

  // API 失败或无数据
  if (query.isError || !query.data) {
    return (
      <details className="ssp-detail ssp-temporal" open>
        <summary>时序特征 V1</summary>
        <div className="ssp-empty">暂无数据</div>
      </details>
    )
  }

  const data: TemporalFeaturesResponse = query.data
  const hasNotes = data.meta.warmup_notes.length > 0 || data.meta.degraded_reasons.length > 0

  return (
    <details className="ssp-detail ssp-temporal" open>
      <summary>时序特征 V1（{data.meta.primary_timeframe} + {data.meta.secondary_timeframe}）</summary>
      {hasNotes && (
        <div className="ssp-degraded" title={[...data.meta.degraded_reasons, ...data.meta.warmup_notes].join('; ')}>
          ⚠ {data.meta.degraded_reasons.length + data.meta.warmup_notes.length} 项提示
        </div>
      )}
      <div className="ssp-cards">
        <FactorCard title="日线上下文 (daily_context)" factors={data.daily_context as unknown as Record<string, unknown>} rows={TEMPORAL_DAILY_ROWS} />
        <FactorCard title="15 分钟响应 (m15_response)" factors={data.m15_response as unknown as Record<string, unknown>} rows={TEMPORAL_M15_ROWS} />
        <FactorCard title="派生关系 (derived_relation)" factors={data.derived_relation as unknown as Record<string, unknown>} rows={TEMPORAL_DERIVED_ROWS} />
      </div>
    </details>
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

      {/* 对比关系（V1.8 客观关系字段，移除动量一致性事件字段）*/}
      {(data.relation.primary_dir !== null && data.relation.primary_dir !== undefined) ||
      (data.relation.secondary_dir !== null && data.relation.secondary_dir !== undefined) ? (
        <div className="ssp-relation">
          <div className="ssp-row">
            <span className="ssp-label">主周期方向</span>
            <span className="ssp-value">{fmtDir(data.relation.primary_dir)}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">副周期方向</span>
            <span className="ssp-value">{fmtDir(data.relation.secondary_dir)}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">趋势一致性</span>
            <span className="ssp-value">{data.relation.trend_alignment ?? '-'}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">主周期 Swing 位置</span>
            <span className="ssp-value">{fmt(data.relation.primary_swing_position, 3)}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">副周期 Swing 位置</span>
            <span className="ssp-value">{fmt(data.relation.secondary_swing_position, 3)}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">主周期斜率 ATR</span>
            <span className="ssp-value">{fmt(data.relation.primary_slope_atr, 4)}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">副周期斜率 ATR</span>
            <span className="ssp-value">{fmt(data.relation.secondary_slope_atr, 4)}</span>
          </div>
          <div className="ssp-row">
            <span className="ssp-label">副 vs 主 位置差</span>
            <span className="ssp-value">{fmt(data.relation.secondary_vs_primary_position_delta, 3)}</span>
          </div>
        </div>
      ) : null}

      {/* 明细折叠 */}
      <details className="ssp-detail">
        <summary>结构因子明细 ({activeTimeframe})</summary>
        <pre className="ssp-detail-pre">
          {JSON.stringify(activeFactors, null, 2)}
        </pre>
      </details>

      {/* 时序特征 V1 折叠卡片：渲染 temporal-features API DTO，受同一个结构状态开关控制 */}
      <TemporalFeaturesCard instrumentId={instrumentId} />
    </div>
  )
}

export default StockStructuralStatePanel
