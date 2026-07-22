// StrategyChart：纯 Canvas 2D 策略图表组件（V1.6.4）
// 对应原型 assets/charts.js 的 drawTrading 渲染管线
// 支持 K 线 + 成交量 + VWAP + Volume Profile + Node Cluster + Volume Delta + 事件标记
// + DSA Pine 标签（HH/HL/LH/LL）与 regime 分段着色 + MACD 副图
// 图层可见性持久化到 localStorage，十字线联动 OHLC 图例
// 用法：<StrategyChart symbol="688112" bars={bars} events={events} strategyId="node" source="watchlist" height={660} />

import { useEffect, useRef, useState, useMemo, useCallback } from 'react'
import clsx from 'clsx'
import {
  CALCULATION_WINDOWS,
  STRATEGIES,
  FEISHU_CAPTURE_LAYERS,
} from '../lib/strategy-manifest'
import type { ChartLayer, DsaSelectorData, IndicatorResponse } from '../api/endpoints'
import {
  MAX_VISIBLE_BARS,
  MIN_VISIBLE_BARS,
  type ChartViewport,
  clampViewport,
  createDefaultViewport,
  panViewport,
  zoomAtAnchor,
} from './chartViewport'
import type { ChartLayerVisibility } from '../features/stock-research/stockResearchTypes'
import {
  getIndicatorViewLayerPreset,
} from '../features/stock-research/stockResearchTypes'
import type { IndicatorView } from '../api/endpoints'
// [PROMPT.md §5.3.4 V2] Canvas 字体/线宽/几何集中缩放
import {
  type ChartRenderScale,
  type RenderDensity,
  getRenderScale,
} from './chartRenderScale'
// [2026-07-21 反馈] SMC 中文显示文案唯一映射（前后端/详情/飞书共用）
import {
  getSmcEventLabel,
  getSmcEqLabel,
  getSmcObLabel,
} from './smcLabels'

// [ChartRightPadding] - 描述: K 线绘图区右侧留白比例（CHANGE-20260713-008）
// 最新 K 线位于绘图区约 80% 位置（留白 20%，落在 18%-22% 要求区间内）。
// 通过压缩 step（bar 分布宽度）实现：所有坐标映射统一使用 step，
// 十字线/滚轮锚点/Pointer 拖拽/双击复位/节点/事件命中自动同步。
// 网格线和十字线水平线仍延伸到 g.plotRight（保持全宽），只压缩 bar 分布区域。
// 不修改 Node/Profile/POC 算法、indicator_contract、盘中监控或 Capture 口径。
const RIGHT_PADDING_RATIO = 0.20

// ===== 颜色常量（A 股红涨绿跌，对齐原型 charts.js 的 C 对象）=====
const C = {
  bg: '#0d1118',
  panel: '#0a0e15',
  grid: '#252c39',
  grid2: '#1b2230',
  text: '#778297',
  text2: '#aab4c8',
  up: '#ef5350',      // A 股红涨
  down: '#26a69a',    // A 股绿跌
  blue: '#4f7cff',
  blue2: '#82a0ff',
  orange: '#ff9800',
  purple: '#8b5cf6',
  yellow: '#ffd166',
  cyan: '#2fd0c2',
  profileBuy: '#ef5350',   // A 股多头红色
  profileSell: '#26a69a',  // A 股空头绿色
  valueArea: '#5f7fd8',
  // BB 轨配色（A 股习惯：上轨/下轨浅蓝、中轨橙黄）
  bbUpperLower: '#2196f3',
  bbMiddle: '#ff9800',
  bbFill: 'rgba(33,150,243,0.08)',
} as const

// ===== 类型定义 =====
export interface BarData {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface ChartEvent {
  time: string
  type: string
  title: string
  description?: string
}

// 图层可见性
export interface LayerVisibility {
  volume: boolean
  dsa: boolean
  macd: boolean
  breakout: boolean
  selection: boolean
  node: boolean
  poc: boolean
  profile: boolean
  bb: boolean
  delta: boolean
  events: boolean
  sqzmom: boolean
  // [CHANGE-011 SMC] - 智能资金图层（BOS/CHoCH/OB/EQH/EQL/trailing），按需计算
  smc: boolean
}

export interface StrategyChartProps {
  symbol: string
  bars: BarData[]
  events?: ChartEvent[]
  indicators?: IndicatorResponse | undefined
  strategyId?: string
  source?: string
  displayName?: string
  height?: number
  timeframe?: string
  onTimeframeChange?: (tf: string) => void
  // [chartViewport] - 受控 viewport：父组件按周期独立保存，切换周期时重置
  // 未传入或失效时由组件内部计算默认值（取末尾 MAX_VISIBLE_BARS 根）
  viewport?: ChartViewport
  onViewportChange?: (vp: ChartViewport) => void
  // [2026-07-21 反馈] 飞书移动舞台默认显示窗口（仅 isCaptureMode 下生效）
  //   未传入时回退到 MAX_VISIBLE_BARS（250）；传入 90 时飞书舞台只显示最近 90 根
  //   不影响底层数据拉取总长度，也不影响详情页用户缩放逻辑
  defaultVisibleBars?: number
  // [feishu-capture] - 描述: 飞书截图模式，强制开启 FEISHU_CAPTURE_LAYERS 且不可关闭，不读写 localStorage
  isCaptureMode?: boolean
  // [CHANGE-20260720-Phase4 §四] indicator_view 选择（仅 capture 模式生效）
  //   携带时使用 INDICATOR_VIEW_LAYER_PRESETS 替代 FEISHU_CAPTURE_LAYERS，
  //   保证"每张截图只渲染一个指标视图"（advice.md v6 + 后端 INDICATOR_VIEW_VALUES）。
  //   - node_cluster: Node + Profile + POC（筹码共识价）
  //   - bollinger: BB（布林带）
  //   - smc: SMC 结构（BOS/CHoCH/OB/EQH/EQL/trailing）
  //   未传入时回退到 FEISHU_CAPTURE_LAYERS（向后兼容旧 capture URL）。
  indicatorView?: IndicatorView | null
  // [PROMPT.md §5.3.4 V2] Canvas 字体/线宽/几何集中缩放密度。
  //   - 'desktop'（默认）：保持现有 8-11px 字号 / 1-1.5px 线宽，PC 端浏览体验不变
  //   - 'mobile_capture'：按 §5.3.4 规范表放大字号（价格轴 32px / Node 36px / SMC swing 36px 等）
  //     与线宽（BB 3px / POC 3.5px / K线最小实体 4px），用于 1440×2560 移动舞台截图。
  //   截图页面（CaptureStockPage）在 isCaptureMode=true 时显式传入 'mobile_capture'，
  //   普通详情页保持默认 'desktop'。
  renderDensity?: RenderDensity
  // [chartLayerVisibility] - 图表图层显隐偏好（PRD §6.2 单一真源 v2）
  // 由父组件 StockResearchWorkspace 持有并传入；StrategyChart 作为受控组件，不再内部管理 layers state。
  // 截图模式时不传（undefined），由 StrategyChart 内部派生 forced layers。
  layerVisibility?: ChartLayerVisibility
  // [ChartRenderFrame] - bars 端渲染帧（PROMPT.md §二.1 + §五.296-307）
  //   父组件从 barsQuery.data 提取 display_frame / source_bar_hash / market_data_contract_version /
  //   bar times 构造，StrategyChart 与 indicators 帧比对；mismatch 时跳过指标图层渲染（保留 K线/网格/profile）。
  //   未传入时降级到"不检查"（保持向后兼容，不阻塞现有调用方）。
  barsFrame?: ChartRenderFrame | null
  // [ChartRenderFrame 3 态] - PROMPT.md §二.1：loading 只在请求 pending 时显示；
  //   请求结束后不匹配必须显示明确错误码 + 重试按钮，禁止无限 loading。
  //   indicatorsFetching=true 时显示"指标加载中"；indicatorsFetching=false 且 mismatch 时显示错误 + 重试。
  indicatorsFetching?: boolean
  // 点击"重试"按钮回调（父组件调用 indicatorsQuery.refetch）
  onIndicatorsRetry?: () => void
}

// 计算后的 Bar（含指标字段）
interface CalculatedBar extends BarData {
  delta: number
  cvd: number
}

// [Volume Profile] - 后端 profile_rows 单行数据结构（SSOT: volume_node_monitor.compute_indicators）
interface ProfileRow {
  price_low: number
  price_high: number
  price_mid: number
  bullish_volume: number
  bearish_volume: number
  total_volume: number
  is_peak: boolean
  is_poc: boolean
  is_value_area: boolean
}

// [Volume Profile] - 后端 profile_meta 元信息（SSOT: volume_node_monitor.compute_indicators）
interface ProfileMeta {
  row_count: number
  price_step: number | null
  poc_price: number | null
  vah_price: number | null
  val_price: number | null
}

// [Volume Profile] - 后端 peak_rows 单行数据（SSOT: volume_node_monitor.compute_indicators）
interface PeakRow {
  price_mid: number
  bullish_volume: number
  bearish_volume: number
  total_volume: number
  is_peak: boolean
}

// [Volume Profile] - 从后端 upper_node/lower_node/peak_rows 提取的节点信息
interface BackendNode {
  id: string
  mid: number
  lo: number
  hi: number
  poc: boolean
  bullish_volume: number
  bearish_volume: number
}

// [Volume Profile] - 后端 VP 数据聚合（profile_rows + profile_meta + peak_rows）
interface BackendProfile {
  rows: ProfileRow[]
  meta: ProfileMeta
  peaks: PeakRow[]
  nodes: BackendNode[]
  pocPrice: number | null
}

// 窗格矩形
interface PaneRect {
  top: number
  bottom: number
}

// 布局几何
interface Geometry {
  l: number
  axis: number
  profileW: number
  gap: number
  plotRight: number
  profileStart: number
  profileEnd: number
  panes: Record<string, PaneRect>
  bottom: number
}

// 映射后的事件（含 bar 索引和渲染坐标）
interface MappedEvent extends ChartEvent {
  id: string
  index: number
  price: number
  color: string
  x?: number
  y?: number
}

// Profile 行命中区域
interface ProfileHit {
  i: number
  y1: number
  y2: number
  x: number
  totalW: number
  row: ProfileRow
}

// 图表运行时状态（供交互命中检测使用）
interface ChartState {
  ctx: CanvasRenderingContext2D | null
  w: number
  h: number
  g: Geometry | null
  data: CalculatedBar[]
  calc: CalculatedBar[]
  min: number
  max: number
  py: ((v: number) => number) | null
  step: number
  profile: BackendProfile | null
  events: MappedEvent[]
  hoverProfileIndex: number | null
  selectedNodeId: string | null
  focusEventId: string | null
  profileHit: ProfileHit[]
  eventHit: MappedEvent[]
  // [DSA 数据源校验] - K 线时间与 indicators.source_bar_times 不一致标记，供 JSX 渲染页面提示横幅
  dsaSourceMismatch: boolean
  // [ChartRenderFrame] - Bars 与 Indicators 帧不匹配标记（PROMPT.md §五.296-307）
  //   周期切换过程中短暂出现"新K线+旧指标"时为 true，drawTrading 跳过指标图层渲染，
  //   仅绘制 K线/网格/profile 基础图层；JSX 显示"指标加载中"提示
  frameMismatch: boolean
  // [PROMPT.md §5.3.4 V2] Canvas 字体/线宽/几何缩放（desktop | mobile_capture）
  //   由组件顶层根据 renderDensity prop 写入，drawTrading 及所有子 draw 函数读取此字段，
  //   禁止 renderer 直接写 '8px monospace' / lineWidth=1 等魔法数字。
  scale: ChartRenderScale
}

// ===== 通用工具函数 =====
const clamp = (v: number, a: number, b: number): number => Math.max(a, Math.min(b, v))
const fmt = (v: number | string, d = 2): string => Number(v).toFixed(d)

function formatVolume(v: number): string {
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toFixed(0)
}

function formatTime(timeStr: string): string {
  const d = new Date(timeStr)
  if (isNaN(d.getTime())) return timeStr

  const dateFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
  const datePart = dateFmt.format(d).replace(/\//g, '-')

  if (/^\d{4}-\d{2}-\d{2}$/.test(timeStr)) {
    return datePart
  }

  const timeFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const timePart = timeFmt.format(d)
  return `${datePart} ${timePart}`
}

// [chartViewport] - 时间键规范化和时间轴刻度函数已迁移至 src/utils/chartTime.ts
//   纯 .ts 文件便于 Node --experimental-strip-types 单元测试（DSA source alignment contract test）
import { normalizeChartTime, timeTicks } from '@/utils/chartTime'
// [DSA Overlay Policy] - DSA 全周期支持（PR #32）
//   DSA VWAP 支持 1d/15m/1h/1w/1mo；1d 是主结构锚，非 1d 是验证图层
import {
  shouldCheckDsaMismatch,
  shouldIncludeDsaInPriceRange,
  shouldRenderBbLayer,
  shouldRenderDsaLayer,
} from '@/utils/dsaOverlayPolicy'
// [DSA Segment Match] - debugIndicatorAlignment 诊断已移除（P1 清理）
//   computeDsaSegmentMatchStats 工具函数保留在 utils/dsaSegmentMatch.ts
// [ChartRenderFrame] - 周期切换原子渲染门禁 + 纵轴 domain policy
//   1. isFrameMatched: Bars 与 Indicators 帧一致才提交指标绘制（PROMPT.md §五.296-307）
//   2. computeVisiblePriceBounds + shouldIncludeNodeInPriceRange: 过滤远端 Node
//      避免纵轴被非可见指标扩张（PROMPT.md §五.255-282）
import {
  buildIndicatorsFrame,
  computeVisiblePriceBounds,
  isFrameMatched,
  shouldIncludeNodeInPriceRange,
  shouldIncludeSmcTrailingInPriceRange,
  type ChartRenderFrame,
} from '@/utils/chartRenderFrame'

// ===== 指标计算模块（从 charts.js 迁移）=====

// 计算 Delta / CVD（typical 已随 buildProfile 删除，不再计算）
function addIndicators(bars: BarData[]): CalculatedBar[] {
  const out: CalculatedBar[] = bars.map(x => ({ ...x } as CalculatedBar))
  let cvd = 0
  out.forEach((d) => {
    const clv = (2 * d.close - d.high - d.low) / Math.max(0.001, d.high - d.low)
    d.delta = d.volume * clamp(clv * 0.82 + (d.close >= d.open ? 0.16 : -0.16), -0.95, 0.95)
    cvd += d.delta
    d.cvd = cvd
  })
  return out
}

// [Volume Profile] - 从后端 indicators 提取 VP 数据
// [CHANGE-20260720-001] 优先读取独立 data["node_cluster"]（固定 1d×250+15m×4000，五周期一致）；
//   旧 watchlist_monitor/volume_node_monitor 仅临时兼容回退，迁移完成后删除。
// profile_rows/profile_meta/peak_rows 为价格档位快照，非 bar 对齐时间序列，禁止前端重算
//
// [PROMPT.md §三.3 V2] Canonical Node DTO V2：
//   优先读取 vn.node_regions（稳定 entity_id/kind/low/mid/high/多空量/is_poc），
//   禁止从 state/peak_rows 重建 Node 列表。V1 缓存缺失 node_regions 时降级到旧路径
//   （从 upper_node/lower_node/peak_rows 收集），保持向后兼容。
function extractBackendProfile(indicators: IndicatorResponse | undefined): BackendProfile | null {
  if (!indicators?.data) return null
  const vn = (indicators.data['node_cluster'] ?? indicators.data['watchlist_monitor'] ?? indicators.data['volume_node_monitor']) as unknown as
    | Record<string, unknown>
    | undefined
  if (!vn) return null

  // profile_rows：完整 100 行 VP 价格档位快照
  const rawRows = vn.profile_rows
  const rows: ProfileRow[] = []
  if (Array.isArray(rawRows)) {
    rawRows.forEach(v => {
      if (v != null && typeof v === 'object') {
        const o = v as Record<string, unknown>
        rows.push({
          price_low: Number(o.price_low) || 0,
          price_high: Number(o.price_high) || 0,
          price_mid: Number(o.price_mid) || 0,
          bullish_volume: Number(o.bullish_volume) || 0,
          bearish_volume: Number(o.bearish_volume) || 0,
          total_volume: Number(o.total_volume) || 0,
          is_peak: Boolean(o.is_peak),
          is_poc: Boolean(o.is_poc),
          is_value_area: Boolean(o.is_value_area),
        })
      }
    })
  }

  // profile_meta：VP 元信息（row_count/price_step/poc_price/vah_price/val_price）
  const rawMeta = vn.profile_meta
  let meta: ProfileMeta = {
    row_count: rows.length,
    price_step: null,
    poc_price: null,
    vah_price: null,
    val_price: null,
  }
  if (rawMeta != null && typeof rawMeta === 'object') {
    const m = rawMeta as Record<string, unknown>
    meta = {
      row_count: Number(m.row_count) || rows.length,
      price_step: m.price_step != null ? Number(m.price_step) : null,
      poc_price: m.poc_price != null ? Number(m.poc_price) : null,
      vah_price: m.vah_price != null ? Number(m.vah_price) : null,
      val_price: m.val_price != null ? Number(m.val_price) : null,
    }
  }

  // peak_rows：peak 节点多空量快照
  const rawPeaks = vn.peak_rows
  const peaks: PeakRow[] = []
  if (Array.isArray(rawPeaks)) {
    rawPeaks.forEach(v => {
      if (v != null && typeof v === 'object') {
        const o = v as Record<string, unknown>
        peaks.push({
          price_mid: Number(o.price_mid) || 0,
          bullish_volume: Number(o.bullish_volume) || 0,
          bearish_volume: Number(o.bearish_volume) || 0,
          total_volume: Number(o.total_volume) || 0,
          is_peak: Boolean(o.is_peak),
        })
      }
    })
  }

  let pocPrice: number | null = null
  if (meta.poc_price != null) {
    pocPrice = meta.poc_price
  } else if (Array.isArray(vn.poc_price)) {
    for (const p of vn.poc_price) {
      if (p != null) { pocPrice = Number(p); break }
    }
  }

  // [PROMPT.md §三.3 V2] 优先读取 Canonical Node DTO V2（vn.node_regions）
  //   后端 _compute_independent_node_cluster / profile_to_dict 都已输出 node_regions，
  //   四链（详情/Capture/Snapshot/Monitor）统一读取该字段，前端禁止从 state/peak_rows 重建。
  //   V1 缓存可能缺失 node_regions（旧 schema），降级到旧路径保持向后兼容。
  const rawNodeRegions = vn.node_regions
  let nodes: BackendNode[] = []
  if (Array.isArray(rawNodeRegions) && rawNodeRegions.length > 0) {
    // [V2] 直接读 Canonical Node DTO（不重建）
    nodes = rawNodeRegions
      .map((v): BackendNode | null => {
        if (v == null || typeof v !== 'object') return null
        const o = v as Record<string, unknown>
        const entityId = typeof o.entity_id === 'string' ? o.entity_id : ''
        const mid = Number(o.mid) || Number(o.price_mid) || 0
        const lo = Number(o.low) || Number(o.price_low) || mid
        const hi = Number(o.high) || Number(o.price_high) || mid
        return {
          id: entityId || `peak_${mid}`,
          mid, lo, hi,
          poc: Boolean(o.is_poc) || (pocPrice != null && Math.abs(mid - pocPrice) < 0.01),
          bullish_volume: Number(o.bullish_volume) || 0,
          bearish_volume: Number(o.bearish_volume) || 0,
        }
      })
      .filter((n): n is BackendNode => n != null)
      .sort((a, b) => a.lo - b.lo)
  } else {
    // [V1 fallback] 旧缓存无 node_regions，从 upper_node/lower_node/peak_rows 重建
    //   保留旧逻辑以兼容 V1 缓存；缓存失效后自动走 V2 路径
    const peakVolMap = new Map<number, { bullish: number; bearish: number }>()
    peaks.forEach(p => {
      peakVolMap.set(p.price_mid, { bullish: p.bullish_volume, bearish: p.bearish_volume })
    })
    const peakMap = new Map<number, { lo: number; hi: number }>()
    const collect = (arr: unknown) => {
      if (!Array.isArray(arr)) return
      arr.forEach(v => {
        if (v != null && typeof v === 'object' && (v as Record<string, unknown>).price_mid != null) {
          const o = v as Record<string, number>
          const mid = Number(o.price_mid)
          if (!peakMap.has(mid)) peakMap.set(mid, { lo: Number(o.price_low), hi: Number(o.price_high) })
        }
      })
    }
    collect(vn.upper_node)
    collect(vn.lower_node)
    nodes = Array.from(peakMap.entries())
      .map(([mid, { lo, hi }], i) => {
        const vol = peakVolMap.get(mid)
        return {
          id: `backend_node_${i + 1}`,
          mid, lo, hi,
          poc: pocPrice != null && Math.abs(mid - pocPrice) < 0.01,
          bullish_volume: vol?.bullish ?? 0,
          bearish_volume: vol?.bearish ?? 0,
        }
      })
      .sort((a, b) => a.lo - b.lo)
  }

  return { rows, meta, peaks, nodes, pocPrice }
}

// ===== 布局几何模块 =====
// 根据启用的图层动态分配窗格高度（价格/成交量/MACD 共享同一 X 轴与十字线索引）
// [2026-07-21 反馈] mobile_capture 模式下成交量区按舞台高度 18% 分配（主图:成交量 ≈ 82:18），
//   desktop 模式保持 76px 固定高度（向后兼容）
function geometry(
  layers: Set<string>,
  w: number,
  h: number,
  scale?: ChartRenderScale,
): Geometry {
  const profileOn = layers.has('profile')
  const volumeOn = layers.has('volume')
  const macdOn = layers.has('macd')
  const sqzmomOn = layers.has('sqzmom')
  const l = 58
  const axis = 57
  const profileW = profileOn ? 148 : 0
  const gap = profileOn ? 8 : 0
  const plotRight = w - axis - profileW - gap
  const bottom = 25
  const paneGap = 7
  let cursor = h - bottom
  const panes: Record<string, PaneRect> = {}
  // [MACD 副图] - 放在成交量上方，与价格/成交量共享 X 轴
  if (macdOn) {
    panes.macd = { bottom: cursor, top: cursor - 82 }
    cursor = panes.macd.top - paneGap
  }
  // [SQZMOM_LB 副图] - 放在 MACD 上方（两个动量副图相邻），后端返回 val/bcolor/scolor 序列
  if (sqzmomOn) {
    panes.sqzmom = { bottom: cursor, top: cursor - 82 }
    cursor = panes.sqzmom.top - paneGap
  }
  if (volumeOn) {
    // [2026-07-21 反馈] mobile_capture 下成交量区按剩余高度 18% 分配
    //   飞书舞台 INDICATOR_VIEW_LAYER_PRESETS 只开 volume（macd/sqzmom 关闭），
    //   所以 (h - 24 - bottom - paneGap) 是 price + volume 总高，18% 给 volume ≈ 82:18
    //   desktop 保持 76px 固定高度（向后兼容）
    const isMobile = scale?.density === 'mobile_capture'
    const volumeH = isMobile
      ? Math.max(150, Math.round((h - 24 - bottom - paneGap) * 0.18))
      : 76
    panes.volume = { bottom: cursor, top: cursor - volumeH }
    cursor = panes.volume.top - paneGap
  }
  panes.price = { top: 24, bottom: Math.max(220, cursor) }
  return {
    l,
    axis,
    profileW,
    gap,
    plotRight,
    profileStart: plotRight + gap,
    profileEnd: w - axis - 4,
    panes,
    bottom,
  }
}

// ===== 绘图工具函数 =====
function fit(canvas: HTMLCanvasElement): { ctx: CanvasRenderingContext2D; w: number; h: number } {
  const dpr = Math.max(1, window.devicePixelRatio || 1)
  const r = canvas.getBoundingClientRect()
  canvas.width = Math.max(1, r.width * dpr)
  canvas.height = Math.max(1, r.height * dpr)
  const ctx = canvas.getContext('2d')!
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  return { ctx, w: r.width, h: r.height }
}

function drawLine(
  ctx: CanvasRenderingContext2D,
  x1: number, y1: number, x2: number, y2: number,
  color: string, width = 1, dash: number[] = [],
): void {
  ctx.beginPath()
  ctx.moveTo(x1, y1)
  ctx.lineTo(x2, y2)
  ctx.strokeStyle = color
  ctx.lineWidth = width
  ctx.setLineDash(dash)
  ctx.stroke()
  ctx.setLineDash([])
}

function drawText(
  ctx: CanvasRenderingContext2D,
  t: string, x: number, y: number,
  color: string = C.text,
  // [PROMPT.md §5.3.4 V2] font 由调用方从 scale.fonts.* 显式传入（默认空串仅作类型 fallback，
  //   实际所有 27 处 drawText 调用都已传 scale.fonts.axisLabel / paneLabel / nodeLabel 等）
  font = '',
  align: CanvasTextAlign = 'left',
): void {
  ctx.fillStyle = color
  ctx.font = font
  ctx.textAlign = align
  ctx.fillText(t, x, y)
}

// 副图刻度与当前值标签
function drawPaneTicks(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  pane: string,
  min: number,
  max: number,
  label: string,
  current: number | undefined,
  color: string,
  scale: ChartRenderScale,
): void {
  const p = g.panes[pane]
  if (!p) return
  drawLine(ctx, g.l, (p.top + p.bottom) / 2, g.plotRight, (p.top + p.bottom) / 2, C.grid2, scale.strokes.grid2)
  // [PROMPT.md §5.3.4 V2] 副图标题/刻度/当前值字号按 scale.fonts.paneLabel / paneTick / paneCurrent 缩放
  //   mobile_capture 下 paneLabel=30px / paneTick=30px / paneCurrent=30px（≥30px）
  //   垂直偏移按字号比例放大（top+12 → top + scale 字号的 1.2 倍）
  const labelOffset = Math.round(parseFloat(scale.fonts.paneLabel) * 1.2)
  const tickOffsetTop = Math.round(parseFloat(scale.fonts.paneTick) * 1.0)
  const tickOffsetBottom = Math.round(parseFloat(scale.fonts.paneTick) * 0.2)
  drawText(ctx, label, g.l + 5, p.top + labelOffset, color, scale.fonts.paneLabel)
  drawText(ctx, fmt(max, 2), g.plotRight + 5, p.top + tickOffsetTop, C.text, scale.fonts.paneTick)
  drawText(ctx, fmt(min, 2), g.plotRight + 5, p.bottom - tickOffsetBottom, C.text, scale.fonts.paneTick)
  if (current !== undefined) {
    const y = p.top + (max - current) / Math.max(0.0001, max - min) * (p.bottom - p.top)
    ctx.fillStyle = color
    // [PROMPT.md §5.3.4 V2] 当前值标签背景尺寸按 scale.geometry.paneCurrentBox* 缩放
    const boxW = scale.geometry.paneCurrentBoxWidth
    const boxH = scale.geometry.paneCurrentBoxHeight
    ctx.fillRect(g.plotRight + 1, y - boxH / 2, boxW, boxH)
    drawText(ctx, fmt(current, 2), g.plotRight + boxW / 2, y + boxH / 4, '#fff', scale.fonts.paneCurrent, 'center')
  }
}

// ===== 渲染函数 =====

// 背景 + 副图底色 + 价格网格 + 垂直网格 + 右侧价格刻度
function drawGrid(
  ctx: CanvasRenderingContext2D,
  w: number, h: number,
  g: Geometry,
  min: number, max: number,
  scale: ChartRenderScale,
): void {
  ctx.fillStyle = C.bg
  ctx.fillRect(0, 0, w, h)
  Object.entries(g.panes).forEach(([name, p]) => {
    if (name !== 'price') {
      ctx.fillStyle = C.panel
      ctx.fillRect(g.l, p.top, g.plotRight - g.l, p.bottom - p.top)
      drawLine(ctx, g.l, p.top, g.plotRight, p.top, C.grid, scale.strokes.paneSep)
    }
  })
  if (g.profileW) {
    ctx.fillStyle = '#0b0f16'
    ctx.fillRect(g.profileStart, g.panes.price.top, g.profileEnd - g.profileStart, g.panes.price.bottom - g.panes.price.top)
    drawLine(ctx, g.profileStart, g.panes.price.top, g.profileStart, g.panes.price.bottom, C.grid, scale.strokes.grid)
  }
  // [PROMPT.md §5.3.4 V2] 价格轴标签字号按 scale.fonts.axisLabel 缩放（mobile_capture ≥32px）
  //   垂直偏移按字号 0.3 倍（保持视觉居中）
  const axisFont = scale.fonts.axisLabel
  const axisOffset = Math.round(parseFloat(axisFont) * 0.3)
  for (let i = 0; i < 7; i++) {
    const y = g.panes.price.top + (g.panes.price.bottom - g.panes.price.top) * i / 6
    drawLine(ctx, g.l, y, g.plotRight, y, C.grid, scale.strokes.grid)
    drawText(ctx, fmt(max - (max - min) * i / 6), w - g.axis + 7, y + axisOffset, C.text, axisFont)
  }
  for (let i = 0; i < 9; i++) {
    const x = g.l + (g.plotRight - g.l) * i / 8
    drawLine(ctx, x, g.panes.price.top, x, h - g.bottom, C.grid2, scale.strokes.grid2)
  }
}

// [Volume Profile] - 右侧 VP 渲染（从后端 profile_rows 直接绘制，禁止前端重算）
function renderProfile(
  ctx: CanvasRenderingContext2D,
  profile: BackendProfile,
  g: Geometry,
  py: (v: number) => number,
  state: ChartState,
  layers: Set<string>,
): void {
  const width = g.profileEnd - g.profileStart
  const rows = profile.rows
  // 后端返回的 total_volume 最大值作为归一化基准
  const maxTotal = Math.max(...rows.map(r => r.total_volume), 1)
  state.profileHit = []
  // [PROMPT.md §5.3.4 V2] 从 state.scale 读取字体/线宽（mobile_capture 放大）
  const { scale } = state

  // 价值区填充 + VAH/VAL 虚线（从后端 profile_meta 读取）
  if (profile.meta.vah_price != null && profile.meta.val_price != null) {
    const valueTop = py(profile.meta.vah_price)
    const valueBottom = py(profile.meta.val_price)
    ctx.fillStyle = 'rgba(95,127,216,.055)'
    ctx.fillRect(g.l, valueTop, g.profileEnd - g.l, Math.max(1, valueBottom - valueTop))
    drawLine(ctx, g.l, valueTop, g.profileEnd, valueTop, 'rgba(130,160,255,.58)', scale.strokes.vaLine, [3, 3])
    drawLine(ctx, g.l, valueBottom, g.profileEnd, valueBottom, 'rgba(130,160,255,.58)', scale.strokes.vaLine, [3, 3])
    drawText(ctx, 'VAH', g.plotRight - 4, valueTop - 4, C.blue2, scale.fonts.vaLabel, 'right')
    drawText(ctx, 'VAL', g.plotRight - 4, valueBottom - 4, C.blue2, scale.fonts.vaLabel, 'right')
  }

  // 买卖量双色条（从后端 profile_rows 直接渲染：price_high/price_low → y 坐标，total_volume → 宽度）
  rows.forEach((row, i) => {
    const y1 = py(row.price_high)
    const y2 = py(row.price_low)
    const bh = Math.max(1, y2 - y1 - 0.4)
    const totalW = row.total_volume / maxTotal * width * 0.94
    const buyW = row.bullish_volume / Math.max(1, row.total_volume) * totalW
    const sellW = totalW - buyW
    const x = g.profileEnd - totalW
    // 价值区内 alpha=1，区外 alpha=0.4（从后端 is_value_area 标记）
    ctx.globalAlpha = row.is_value_area ? 1 : 0.4
    ctx.fillStyle = C.profileSell
    ctx.fillRect(x, y1, sellW, bh)
    ctx.fillStyle = C.profileBuy
    ctx.fillRect(x + sellW, y1, buyW, bh)
    ctx.globalAlpha = 1
    // POC 行高亮（从后端 is_poc 标记）
    if (row.is_poc && layers.has('poc')) {
      ctx.strokeStyle = C.orange
      ctx.lineWidth = scale.strokes.pocLine
      ctx.strokeRect(x - 0.5, y1 - 0.5, totalW + 1, bh + 1)
    }
    // Peak 节点标记（从后端 is_peak 标记，左侧黄色短条）
    if (row.is_peak) {
      ctx.fillStyle = C.yellow
      ctx.fillRect(g.profileStart - 3, y1 + bh * 0.25, 3, Math.max(2, bh * 0.5))
    }
    // hover 高亮
    if (state.hoverProfileIndex === i) {
      ctx.fillStyle = 'rgba(255,255,255,.12)'
      ctx.fillRect(g.profileStart, y1, width, bh)
    }
    state.profileHit.push({ i, y1, y2, x, totalW, row })
  })

  // [PROMPT.md §5.3.4 V2] 买卖量头部标签按 scale.fonts.profileLabel 缩放（mobile_capture 34px）
  //   垂直偏移按字号 1.2 倍（与 paneLabel 一致）
  const profileLabelOffset = Math.round(parseFloat(scale.fonts.profileLabel) * 1.2)
  const profileLabelGap = Math.round(parseFloat(scale.fonts.profileLabel) * 3.5)
  drawText(ctx, '卖量', g.profileStart + 3, g.panes.price.top + profileLabelOffset, C.profileSell, scale.fonts.profileLabel)
  drawText(ctx, '买量', g.profileStart + profileLabelGap, g.panes.price.top + profileLabelOffset, C.profileBuy, scale.fonts.profileLabel)

  // Node Cluster 节点矩形框（从后端 upper_node/lower_node 提取的节点区间）
  if (layers.has('node') && profile.nodes.length > 0) {
    profile.nodes.forEach(n => {
      const y1 = py(n.hi)
      const y2 = py(n.lo)
      const selected = state.selectedNodeId === n.id
      ctx.strokeStyle = n.poc ? C.orange : selected ? '#dce6ff' : 'rgba(79,124,255,.72)'
      // [PROMPT.md §5.3.4 V2] Node 边线宽度按 scale.strokes.nodeLine 缩放
      ctx.lineWidth = selected ? scale.strokes.nodeLine * 1.5 : n.poc ? scale.strokes.pocLine : scale.strokes.nodeLine
      ctx.strokeRect(g.profileStart + 0.5, y1 + 0.5, width - 1, Math.max(2, y2 - y1 - 1))
    })
  }
}

// 成交量副图
function renderVolume(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  data: CalculatedBar[],
  step: number,
  barW: number,
  scale: ChartRenderScale,
): void {
  const p = g.panes.volume
  if (!p) return
  const vmax = Math.max(...data.map(d => d.volume))
  data.forEach((d, i) => {
    const x = g.l + (i + 0.5) * step
    const bh = d.volume / vmax * (p.bottom - p.top) * 0.83
    ctx.fillStyle = d.close >= d.open ? 'rgba(239,83,80,.58)' : 'rgba(38,166,154,.58)'
    ctx.fillRect(x - barW / 2, p.bottom - bh, barW, bh)
  })
  drawPaneTicks(ctx, g, 'volume', 0, vmax, 'VOL', data[data.length - 1].volume, C.text, scale)
}

// 突破压力区
function renderBreakout(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  data: CalculatedBar[],
  py: (v: number) => number,
  scale: ChartRenderScale,
): void {
  const pressure = Math.max(...data.slice(-55, -18).map(d => d.high))
  // [PROMPT.md §5.3.4 V2] 结构压力位线宽/字号按 scale.strokes.grid / fonts.structureLabel 缩放
  drawLine(ctx, g.l, py(pressure), g.plotRight, py(pressure), C.down, scale.strokes.grid, [7, 4])
  drawText(ctx, '结构压力', g.plotRight - 54, py(pressure) - 5, C.down, scale.fonts.structureLabel)
}

// ===== 通用渲染器（根据后端返回的 ChartLayer.renderer 分发）=====

// 通用渲染器：根据 layer.renderer 分发
// [chartViewport] - 描述: 所有 renderer 统一按 normalizeChartTime 时间键逐根匹配，
//   不再用 values.length - barsCount 尾部截取，避免 K 线与指标错位
// [CHANGE-20260719-003 §四] indexMap 参数：由 drawTrading 顶部构建并传入（time-index map 共享），
//   避免每个 render 函数重复构建 O(n) Map；未传入时各 render 内部回退到 buildDisplayIndexMap。
function renderIndicatorLayer(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  barsCount: number,
  step: number,
  py: (v: number) => number,
  displayTimes: string[],
  timeframe: string,
  indexMap: (number | undefined)[],
  scale: ChartRenderScale,
): void {
  switch (layer.renderer) {
    case 'line':
      renderIndicatorLine(ctx, g, layer, data, displayTimes, step, py, timeframe, indexMap, scale)
      break
    case 'dsa_polyline':
      // renderDsaPolyline 用 visual_segments 自有时间匹配，不消费 indexMap
      renderDsaPolyline(ctx, g, layer, data, displayTimes, step, py, timeframe, scale)
      break
    case 'price_zone':
      renderIndicatorPriceZone(ctx, g, layer, data, displayTimes, step, py, timeframe, indexMap, scale)
      break
    case 'band':
      renderIndicatorBand(ctx, g, layer, data, displayTimes, step, py, timeframe, indexMap, scale)
      break
    case 'macd':
      renderIndicatorMacd(ctx, g, layer, data, barsCount, step, displayTimes, timeframe, indexMap, scale)
      break
    case 'sqzmom':
      renderIndicatorSqzmom(ctx, g, layer, data, barsCount, step, displayTimes, timeframe, indexMap, scale)
      break
    // [CHANGE-011 SMC] - 智能资金概念图层渲染（BOS/CHoCH/OB/EQH/EQL/trailing）
    case 'smc':
      // renderIndicatorSmc 用 klineTimeIndex（displayTimes → display index）反向映射，不消费 indexMap
      renderIndicatorSmc(ctx, g, layer, data, displayTimes, step, py, timeframe, scale)
      break
  }
}

// [chartViewport] - 描述: 把 display bar 时间映射到指标数组索引，按 normalizeChartTime 逐根匹配
function buildDisplayIndexMap(
  displayTimes: string[],
  indicatorTimes: (number | string | null)[] | undefined,
  tf: string,
): (number | undefined)[] {
  const timeIndex = buildTimeIndex(indicatorTimes, tf)
  return displayTimes.map(t => {
    if (t == null) return undefined
    const key = normalizeChartTime(t, tf)
    return key != null ? timeIndex.get(key) : undefined
  })
}

// [DSA 分段] - 线图渲染（支持 regime_field 分段 + direction_colored + 锚点小圆点）
function renderIndicatorLine(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  displayTimes: string[],
  step: number,
  py: (v: number) => number,
  timeframe: string,
  sharedIndexMap: (number | undefined)[],
  scale: ChartRenderScale,
): void {
  // layer.fields[0] 是主值字段（如 dsa_vwap）
  // layer.fields[1] 是方向字段（如 dsa_dir，1=上涨，0=下跌）
  const valueField = layer.fields[0]
  const dirField = layer.fields[1]
  const values = data[valueField]
  if (!values || !values.length) return

  // [chartViewport] - 按 normalizeChartTime 时间键逐根匹配，不再 tail 截取
  // [CHANGE-20260719-003 §四] 复用 drawTrading 传入的 indexMap（time-index map 共享）
  const indexMap = sharedIndexMap ?? buildDisplayIndexMap(displayTimes, data.time, timeframe)
  const len = indexMap.length

  // [DSA 分段] - 分组键：优先 regime_field（regime_id），回退 dirField
  // 每次 regime_id 改变 → 结束旧段 → 新段从当前点开始（切换点不连接）
  const segField = layer.regime_field && data[layer.regime_field] ? layer.regime_field : dirField
  const segKeys: (number | string | null)[] | undefined = segField ? data[segField] : undefined

  if (layer.direction_colored && segKeys && dirField && data[dirField]) {
    const dirs = data[dirField]
    // 分段绘制：相邻分组键相同的点连成一段；切换点不连接（新段从 i 开始，不含 i-1）
    let segStart = 0
    for (let i = 1; i <= len; i++) {
      const curIdx = i < len ? indexMap[i] : undefined
      const prevIdx = indexMap[i - 1]
      const curKey = curIdx != null ? segKeys[curIdx] : null
      const prevKey = prevIdx != null ? segKeys[prevIdx] : null
      const segChanged = i === len || curKey !== prevKey
      if (segChanged && i > segStart + 1) {
        // 绘制 segStart 到 i-1 的线段（旧段，方向由 i-1 处的 dir 决定）
        const prevSegIdx = indexMap[i - 1]
        const dir = prevSegIdx != null ? dirs[prevSegIdx] : null
        const color = dir === 1 ? (layer.direction_up_color || '#ff1744') : (layer.direction_down_color || '#00e676')
        ctx.beginPath()
        let started = false
        for (let j = segStart; j < i; j++) {
          const idx = indexMap[j]
          if (idx == null) { started = false; continue }
          const v = values[idx]
          if (v == null || typeof v === 'string') { started = false; continue }
          const x = g.l + (j + 0.5) * step
          const y = py(v)
          if (!started) { ctx.moveTo(x, y); started = true }
          else ctx.lineTo(x, y)
        }
        ctx.strokeStyle = color
        // [PROMPT.md §5.3.4 V2] DSA VWAP 线宽按 scale.strokes.dsaVwap（mobile_capture 3px）
        ctx.lineWidth = scale.strokes.dsaVwap
        ctx.stroke()
        // [DSA 分段] - 切换点不连接：新段从 i 开始（不是 i-1），避免跨段连线
        segStart = i
      } else if (segChanged && i <= segStart + 1) {
        // 段内不足 2 个点，直接推进 segStart 到 i（不绘制）
        segStart = i
      }
    }
  } else {
    // 单色线
    ctx.beginPath()
    let started = false
    for (let i = 0; i < len; i++) {
      const idx = indexMap[i]
      if (idx == null) { started = false; continue }
      const v = values[idx]
      if (v == null || typeof v === 'string') { started = false; continue }
      const x = g.l + (i + 0.5) * step
      const y = py(v)
      if (!started) { ctx.moveTo(x, y); started = true }
      else ctx.lineTo(x, y)
    }
    ctx.strokeStyle = layer.color || C.yellow
    // [PROMPT.md §5.3.4 V2] 单色线宽按 scale.strokes.dsaVwap（mobile_capture 3px）
    ctx.lineWidth = scale.strokes.dsaVwap
    ctx.stroke()
  }

  // [DSA 分段] - 锚点小圆点：在 anchor_field 标记的 bar 位置（vwap 值处）绘制小圆点
  if (layer.anchor_field && data[layer.anchor_field]) {
    const anchors = data[layer.anchor_field]
    for (let i = 0; i < len; i++) {
      const idx = indexMap[i]
      if (idx == null) continue
      const a = anchors[idx]
      // anchor_time != null 表示该 bar 是锚点（dir 翻转点）
      if (a == null) continue
      const v = values[idx]
      if (v == null || typeof v === 'string') continue
      const x = g.l + (i + 0.5) * step
      const y = py(v)
      // 外圈白色 + 内圈方向色（与当前段方向一致）
      const dir = dirField && data[dirField] ? data[dirField][idx] : null
      const innerColor = dir === 1 ? (layer.direction_up_color || '#ff1744') : (layer.direction_down_color || '#00e676')
      ctx.beginPath()
      ctx.arc(x, y, 3.5, 0, Math.PI * 2)
      ctx.fillStyle = '#ffffff'
      ctx.fill()
      ctx.beginPath()
      ctx.arc(x, y, 2.2, 0, Math.PI * 2)
      ctx.fillStyle = innerColor
      ctx.fill()
    }
  }

  // [DSA Pine 标签] - 在 pivot_type 标记的 bar 位置绘制 HH/HL/LH/LL 文本
  // pivot_type/pivot_price 位于 layer.fields[4]/[5]，与 Pine Script 标签对齐
  const pivotTypeField = layer.fields[4]
  const pivotPriceField = layer.fields[5]
  if (pivotTypeField && data[pivotTypeField] && pivotPriceField && data[pivotPriceField]) {
    const pivotTypes = data[pivotTypeField]
    const pivotPrices = data[pivotPriceField]
    for (let i = 0; i < len; i++) {
      const idx = indexMap[i]
      if (idx == null) continue
      const label = pivotTypes[idx]
      if (typeof label !== 'string') continue
      const price = pivotPrices[idx]
      if (price == null || typeof price === 'string') continue
      const x = g.l + (i + 0.5) * step
      const y = py(Number(price))
      // HH/LH 为波段高点，标签画在 K 线上方；HL/LL 为波段低点，画在下方
      const isHigh = label === 'HH' || label === 'LH'
      const labelColor = isHigh ? C.up : C.down
      // [PROMPT.md §5.3.4 V2] DSA Pine 标签字号按 scale.fonts.legendBold（mobile_capture 30px）
      //   垂直偏移按字号比例放大（保持视觉间距）
      const _legendSize = parseFloat(scale.fonts.legendBold)
      const textY = isHigh ? y - Math.round(_legendSize * 0.9) : y + Math.round(_legendSize * 1.4)
      ctx.fillStyle = labelColor
      ctx.font = scale.fonts.legendBold
      ctx.textAlign = 'center'
      ctx.fillText(label, x, textY)
    }
  }
}

// [DSA 分段] - dsa_polyline 渲染器：基于后端预计算的 visual_segments 逐段独立绘制
//   段间不连线（每段独立 beginPath/moveTo/lineTo/stroke），上涨 #ff1744 / 下降 #00e676
//   锚点与 HH/HL/LH/LL 标签通过 segment point 的实际 time 经 normalizeChartTime 匹配 K 线索引
// [DSA 数据契约] - visual_segments 从 data.dsa_selector.visual_segments 读取（非 layer.visual_segments）
function renderDsaPolyline(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: DsaSelectorData | Record<string, (number | string | null)[]>,
  displayTimes: string[],
  step: number,
  py: (v: number) => number,
  timeframe: string,
  scale: ChartRenderScale,
): void {
  // [DSA 数据契约] - visual_segments 属于 data（DsaSelectorData），不属于 ChartLayer
  const segments = (data as DsaSelectorData).visual_segments
  if (!segments || segments.length === 0) return
  // 后续按 Record 索引访问 anchor_time / pivot_type / pivot_price / time 等数组字段
  const recordData = data as Record<string, (number | string | null)[]>

  // [PR #34] - DSA visual_segments matched 诊断已移除（debugIndicatorAlignment 清理）
  //   后端 format_dsa_time 修复后，15m/1h segment.points.time 含 THH:MM:SS，
  //   normalizeChartTime 可与 K线 displayTimes canonical 匹配。

  // K 线时间 → display index 映射（segment point time 经 normalizeChartTime 匹配）
  const klineTimeIndex = new Map<string, number>()
  displayTimes.forEach((t, i) => {
    const key = normalizeChartTime(t, timeframe)
    if (key != null) klineTimeIndex.set(key, i)
  })

  // segment point time → value / direction 查找表（供锚点着色与定位使用）
  const segValueByTime = new Map<string, number>()
  const segDirByTime = new Map<string, 1 | -1>()
  for (const seg of segments) {
    if (!seg.points) continue
    for (const pt of seg.points) {
      const key = normalizeChartTime(pt.time, timeframe)
      if (key == null) continue
      segValueByTime.set(key, pt.value)
      segDirByTime.set(key, seg.direction)
    }
  }

  // 逐段绘制：每段独立 beginPath/stroke，段间不连线
  for (const seg of segments) {
    if (!seg.points || seg.points.length === 0) continue
    const color = seg.direction === 1 ? '#ff1744' : '#00e676'
    ctx.beginPath()
    let started = false
    for (const pt of seg.points) {
      const key = normalizeChartTime(pt.time, timeframe)
      if (key == null) continue
      const i = klineTimeIndex.get(key)
      if (i == null) continue
      const x = g.l + (i + 0.5) * step
      const y = py(pt.value)
      if (!started) { ctx.moveTo(x, y); started = true }
      else ctx.lineTo(x, y)
    }
    if (started) {
      ctx.strokeStyle = color
      // [PROMPT.md §5.3.4 V2] DSA polyline 线宽按 scale.strokes.dsaPolyline（mobile_capture 2.5px）
      ctx.lineWidth = scale.strokes.dsaPolyline
      ctx.stroke()
    }
  }

  // 锚点小圆点：anchor_field 提供 anchor_time 数组，非 null 即方向翻转锚点
  //   位置与值通过 anchor_time 经 normalizeChartTime 匹配 K 线索引与 segment point value
  if (layer.anchor_field && recordData[layer.anchor_field]) {
    const anchors = recordData[layer.anchor_field]
    for (let k = 0; k < anchors.length; k++) {
      const a = anchors[k]
      if (a == null) continue  // null 表示该 bar 非锚点
      const key = normalizeChartTime(a, timeframe)
      if (key == null) continue
      const i = klineTimeIndex.get(key)
      if (i == null) continue
      const v = segValueByTime.get(key)
      if (v == null) continue
      const x = g.l + (i + 0.5) * step
      const y = py(v)
      // 锚点方向色：取该 time 所属 segment 的 direction（翻转后方向）
      const dir = segDirByTime.get(key)
      const innerColor = dir === 1 ? (layer.direction_up_color || '#ff1744') : (layer.direction_down_color || '#00e676')
      ctx.beginPath()
      ctx.arc(x, y, 3.5, 0, Math.PI * 2)
      ctx.fillStyle = '#ffffff'
      ctx.fill()
      ctx.beginPath()
      ctx.arc(x, y, 2.2, 0, Math.PI * 2)
      ctx.fillStyle = innerColor
      ctx.fill()
    }
  }

  // [DSA Pine 标签] - HH/HL/LH/LL：pivot_type/pivot_price 按 indicator index 存储，
  //   time 取 data.time[indicator_index]，经 normalizeChartTime 匹配 K 线索引
  const pivotTypeField = layer.fields[4]
  const pivotPriceField = layer.fields[5]
  if (pivotTypeField && recordData[pivotTypeField] && pivotPriceField && recordData[pivotPriceField] && recordData.time) {
    const pivotTypes = recordData[pivotTypeField]
    const pivotPrices = recordData[pivotPriceField]
    const times = recordData.time
    for (let k = 0; k < pivotTypes.length; k++) {
      const label = pivotTypes[k]
      if (typeof label !== 'string') continue
      const price = pivotPrices[k]
      if (price == null || typeof price === 'string') continue
      const t = times[k]
      const key = normalizeChartTime(t, timeframe)
      if (key == null) continue
      const i = klineTimeIndex.get(key)
      if (i == null) continue
      const x = g.l + (i + 0.5) * step
      const y = py(Number(price))
      // HH/LH 为波段高点，标签画在 K 线上方；HL/LL 为波段低点，画在下方
      const isHigh = label === 'HH' || label === 'LH'
      const labelColor = isHigh ? C.up : C.down
      // [PROMPT.md §5.3.4 V2] DSA Pine 标签字号按 scale.fonts.legendBold（mobile_capture 30px）
      //   垂直偏移按字号比例放大（保持视觉间距）
      const _legendSize = parseFloat(scale.fonts.legendBold)
      const textY = isHigh ? y - Math.round(_legendSize * 0.9) : y + Math.round(_legendSize * 1.4)
      ctx.fillStyle = labelColor
      ctx.font = scale.fonts.legendBold
      ctx.textAlign = 'center'
      ctx.fillText(label, x, textY)
    }
  }
}

// 价格区间渲染（半透明矩形）
function renderIndicatorPriceZone(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  displayTimes: string[],
  step: number,
  py: (v: number) => number,
  timeframe: string,
  sharedIndexMap: (number | undefined)[],
  _scale: ChartRenderScale,
): void {
  // layer.fields: [upper_node, lower_node, poc_price]
  const upperField = layer.fields[0]
  const lowerField = layer.fields[1]
  const upperVals = data[upperField]
  const lowerVals = data[lowerField]
  if (!upperVals || !lowerVals) return

  // [chartViewport] - 按 normalizeChartTime 时间键逐根匹配，不再 tail 截取
  // [CHANGE-20260719-003 §四] 复用 drawTrading 传入的 indexMap（time-index map 共享）
  const indexMap = sharedIndexMap ?? buildDisplayIndexMap(displayTimes, data.time, timeframe)
  const len = indexMap.length

  ctx.fillStyle = layer.color || 'rgba(33,150,243,0.50)'
  for (let i = 0; i < len; i++) {
    const idx = indexMap[i]
    if (idx == null) continue
    const upper = upperVals[idx]
    const lower = lowerVals[idx]
    // [类型守卫] - upper/lower 必须为 number（data 放宽为 number|string|null 后需显式收窄）
    if (typeof upper !== 'number' || typeof lower !== 'number') continue
    const x = g.l + i * step
    const y1 = py(upper)
    const y2 = py(lower)
    ctx.fillRect(x, y1, step, y2 - y1)
  }
}

// Band 带状渲染（布林带等，A 股配色：上轨/下轨浅蓝、中轨橙黄）
function renderIndicatorBand(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  displayTimes: string[],
  step: number,
  py: (v: number) => number,
  timeframe: string,
  sharedIndexMap: (number | undefined)[],
  scale: ChartRenderScale,
): void {
  const upperField = layer.fields[0]
  const lowerField = layer.fields[1]
  const middleField = layer.fields[2]
  const upperVals = data[upperField]
  const lowerVals = data[lowerField]
  const middleVals = middleField ? data[middleField] : null
  if (!upperVals || !lowerVals) return

  // [chartViewport] - 按 normalizeChartTime 时间键逐根匹配，不再 tail 截取
  // [CHANGE-20260719-003 §四] 复用 drawTrading 传入的 indexMap（time-index map 共享）
  const indexMap = sharedIndexMap ?? buildDisplayIndexMap(displayTimes, data.time, timeframe)
  const len = indexMap.length

  // A 股 BB 配色：填充浅蓝半透明、上轨/下轨蓝色、中轨橙黄
  const bandColor = C.bbFill
  const upperLowerColor = C.bbUpperLower
  const middleColor = C.bbMiddle

  // 1. 半透明填充带
  ctx.beginPath()
  let started = false
  for (let i = 0; i < len; i++) {
    const idx = indexMap[i]
    if (idx == null) { started = false; continue }
    const u = upperVals[idx]
    const l = lowerVals[idx]
    if (u == null || l == null) { started = false; continue }
    const x = g.l + (i + 0.5) * step
    const un = Number(u)
    if (!started) { ctx.moveTo(x, py(un)); started = true }
    else ctx.lineTo(x, py(un))
  }
  for (let i = len - 1; i >= 0; i--) {
    const idx = indexMap[i]
    if (idx == null) continue
    const l = lowerVals[idx]
    if (l == null) continue
    const x = g.l + (i + 0.5) * step
    ctx.lineTo(x, py(Number(l)))
  }
  ctx.closePath()
  ctx.fillStyle = bandColor
  ctx.fill()

  // 2. 上轨线（浅蓝）
  ctx.beginPath()
  started = false
  for (let i = 0; i < len; i++) {
    const idx = indexMap[i]
    if (idx == null) { started = false; continue }
    const v = upperVals[idx]
    if (v == null) { started = false; continue }
    const x = g.l + (i + 0.5) * step
    const vn = Number(v)
    if (!started) { ctx.moveTo(x, py(vn)); started = true }
    else ctx.lineTo(x, py(vn))
  }
  ctx.strokeStyle = upperLowerColor
  // [PROMPT.md §5.3.4 V2] BB 上轨线宽按 scale.strokes.bbLine（mobile_capture 3px）
  ctx.lineWidth = scale.strokes.bbLine
  ctx.setLineDash([5, 3])
  ctx.stroke()
  ctx.setLineDash([])

  // 3. 下轨线（浅蓝）
  ctx.beginPath()
  started = false
  for (let i = 0; i < len; i++) {
    const idx = indexMap[i]
    if (idx == null) { started = false; continue }
    const v = lowerVals[idx]
    if (v == null) { started = false; continue }
    const x = g.l + (i + 0.5) * step
    const vn = Number(v)
    if (!started) { ctx.moveTo(x, py(vn)); started = true }
    else ctx.lineTo(x, py(vn))
  }
  ctx.strokeStyle = upperLowerColor
  // [PROMPT.md §5.3.4 V2] BB 下轨线宽按 scale.strokes.bbLine（mobile_capture 3px）
  ctx.lineWidth = scale.strokes.bbLine
  ctx.setLineDash([5, 3])
  ctx.stroke()
  ctx.setLineDash([])

  // 4. 中轨线（橙黄实线）
  if (middleVals) {
    ctx.beginPath()
    started = false
    for (let i = 0; i < len; i++) {
      const idx = indexMap[i]
      if (idx == null) { started = false; continue }
      const v = middleVals[idx]
      if (v == null) { started = false; continue }
      const x = g.l + (i + 0.5) * step
      const vn = Number(v)
      if (!started) { ctx.moveTo(x, py(vn)); started = true }
      else ctx.lineTo(x, py(vn))
    }
    ctx.strokeStyle = middleColor
    // [PROMPT.md §5.3.4 V2] BB 中轨线宽按 scale.strokes.bbLine（mobile_capture 3px）
    ctx.lineWidth = scale.strokes.bbLine
    ctx.stroke()
  }
}

// [chartViewport] - 描述: 根据后端返回 time 数组建立 time->index 映射，供 MACD/BB/DSA 对齐使用
function buildTimeIndex(timeArr: (number | string | null)[] | undefined, tf: string): Map<string, number> {
  const map = new Map<string, number>()
  if (!timeArr) return map
  timeArr.forEach((t, idx) => {
    if (t == null) return
    const key = normalizeChartTime(t, tf)
    if (key == null) return
    if (!map.has(key)) map.set(key, idx)
  })
  return map
}

// [MACD 副图] - 描述: 仅绘制后端返回的 macd_dif/macd_dea/macd_hist，禁止前端重算
function renderIndicatorMacd(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  _layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  _barsCount: number,
  step: number,
  displayTimes: string[],
  timeframe: string,
  sharedIndexMap: (number | undefined)[],
  scale: ChartRenderScale,
): void {
  const p = g.panes.macd
  if (!p || !displayTimes.length) return

  const difVals = data.macd_dif
  const deaVals = data.macd_dea
  const histVals = data.macd_hist
  if (!difVals?.length || !deaVals?.length || !histVals?.length) return

  // [MACD 副图] - 描述: 用后端 time 数组与 K 线 time 数组按 normalizeChartTime 对齐，禁止数组尾部长度猜测
  // [CHANGE-20260719-003 §四] 复用 drawTrading 传入的 indexMap（time-index map 共享）
  //   buildDisplayIndexMap 与原 buildTimeIndex + displayTimes.map 输出等价（均为 display→indicator 索引数组）
  const indexes = sharedIndexMap ?? buildDisplayIndexMap(displayTimes, data.time, timeframe)

  // [MACD 副图] - 描述: 硬性检查对齐命中率，正常情况应与可见 K 线数量接近
  const matchedCount = indexes.filter((v) => v != null).length
  if (process.env.NODE_ENV === 'development' && matchedCount < displayTimes.length * 0.5) {
    // eslint-disable-next-line no-console
    console.warn(
      `[StrategyChart] MACD 时间对齐命中率过低: ${matchedCount}/${displayTimes.length}, timeframe=${timeframe}`,
    )
  }

  // 计算 MACD 范围（固定包含 0 轴）
  const visible: number[] = []
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const dif = difVals[idx]
    const dea = deaVals[idx]
    const hist = histVals[idx]
    if (typeof dif === 'number') visible.push(Math.abs(dif))
    if (typeof dea === 'number') visible.push(Math.abs(dea))
    if (typeof hist === 'number') visible.push(Math.abs(hist))
  }
  const rawMax = visible.length ? Math.max(...visible) : 0
  const bound = Math.max(0.0001, rawMax * 1.08)
  const range = bound * 2

  const my = (v: number) => p.bottom - (v + bound) / range * (p.bottom - p.top)

  // 1. 零轴
  drawLine(ctx, g.l, my(0), g.plotRight, my(0), C.grid2, 1, [4, 3])

  // 2. 柱状图（hist > 0 红色，< 0 绿色；A 股红涨绿跌）
  const barW = Math.max(1.5, step * 0.62)
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const h = histVals[idx]
    if (h == null || typeof h === 'string') continue
    const hn = Number(h)
    const x = g.l + (i + 0.5) * step
    const y0 = my(0)
    const y1 = my(hn)
    ctx.fillStyle = hn >= 0 ? 'rgba(239,83,80,.55)' : 'rgba(38,166,154,.55)'
    ctx.fillRect(x - barW / 2, Math.min(y0, y1), barW, Math.max(1, Math.abs(y1 - y0)))
  }

  // 3. DIF 快线
  ctx.beginPath()
  let started = false
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const v = difVals[idx]
    if (v == null || typeof v === 'string') { started = false; continue }
    const x = g.l + (i + 0.5) * step
    const y = my(Number(v))
    if (!started) { ctx.moveTo(x, y); started = true }
    else ctx.lineTo(x, y)
  }
  ctx.strokeStyle = '#f4c430'
  // [PROMPT.md §5.3.4 V2] MACD DIF 线宽按 scale.strokes.macdDif（mobile_capture 2px）
  ctx.lineWidth = scale.strokes.macdDif
  ctx.stroke()

  // 4. DEA 慢线
  ctx.beginPath()
  started = false
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const v = deaVals[idx]
    if (v == null || typeof v === 'string') { started = false; continue }
    const x = g.l + (i + 0.5) * step
    const y = my(Number(v))
    if (!started) { ctx.moveTo(x, y); started = true }
    else ctx.lineTo(x, y)
  }
  ctx.strokeStyle = '#2196f3'
  // [PROMPT.md §5.3.4 V2] MACD DEA 线宽按 scale.strokes.macdDea（mobile_capture 2px）
  ctx.lineWidth = scale.strokes.macdDea
  ctx.stroke()

  // 5. 右侧刻度与当前值标签
  // [PROMPT.md §5.3.4 V2] 副图刻度字号按 scale.fonts.paneTick（mobile_capture 30px）
  const _macdTickOffsetTop = Math.round(parseFloat(scale.fonts.paneTick) * 1.0)
  const _macdTickOffsetBottom = Math.round(parseFloat(scale.fonts.paneTick) * 0.2)
  drawText(ctx, fmt(bound, 3), g.plotRight + 5, p.top + _macdTickOffsetTop, C.text, scale.fonts.paneTick)
  drawText(ctx, fmt(-bound, 3), g.plotRight + 5, p.bottom - _macdTickOffsetBottom, C.text, scale.fonts.paneTick)
  let lastDif: number | undefined
  for (let i = indexes.length - 1; i >= 0; i--) {
    const idx = indexes[i]
    if (idx == null) continue
    const v = difVals[idx]
    if (typeof v === 'number') { lastDif = v; break }
  }
  if (typeof lastDif === 'number') {
    const yDif = my(lastDif)
    ctx.fillStyle = '#f4c430'
    // [PROMPT.md §5.3.4 V2] MACD 当前值标签背景按 scale.geometry.paneCurrentBox* 缩放
    ctx.fillRect(g.plotRight + 1, yDif - scale.geometry.paneCurrentBoxHeight / 2, scale.geometry.paneCurrentBoxWidth, scale.geometry.paneCurrentBoxHeight)
    drawText(ctx, fmt(lastDif, 3), g.plotRight + scale.geometry.paneCurrentBoxWidth / 2, yDif + scale.geometry.paneCurrentBoxHeight / 4, '#fff', scale.fonts.paneCurrent, 'center')
  }
}

// [SQZMOM_LB 副图] - 描述: 仅绘制后端返回的 sqzmom_val/sqzmom_bcolor/sqzmom_scolor，禁止前端重算
// Pine 颜色 → Canvas hex 映射（与 TradingView Pine 内置颜色一致）：
//   bcolor: lime(#00FF00) green(#008000) red(#FF0000) maroon(#800000)
//   scolor: blue(#0000FF) black(#000000) gray(#808080)
const SQZMOM_BCOLOR: Record<string, string> = {
  lime: '#00FF00',
  green: '#008000',
  red: '#FF0000',
  maroon: '#800000',
}
const SQZMOM_SCOLOR: Record<string, string> = {
  blue: '#0000FF',
  black: '#000000',
  gray: '#808080',
}

function renderIndicatorSqzmom(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  _layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  _barsCount: number,
  step: number,
  displayTimes: string[],
  timeframe: string,
  sharedIndexMap: (number | undefined)[],
  scale: ChartRenderScale,
): void {
  const p = g.panes.sqzmom
  if (!p || !displayTimes.length) return

  const vals = data.sqzmom_val
  const bcolors = data.sqzmom_bcolor
  const scolors = data.sqzmom_scolor
  // [SQZMOM_LB 副图] - 描述: 早返回 guard，API 缺失 sqzmom_lb 时不崩溃
  if (!vals?.length) return

  // [chartViewport] - 描述: 用后端 time 数组与 K 线 time 数组按 normalizeChartTime 对齐
  // [CHANGE-20260719-003 §四] 复用 drawTrading 传入的 indexMap（time-index map 共享）
  const indexes = sharedIndexMap ?? buildDisplayIndexMap(displayTimes, data.time, timeframe)

  // 计算 val 范围（含 0 轴对称）
  const visible: number[] = []
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const v = vals[idx]
    if (typeof v === 'number') visible.push(Math.abs(v))
  }
  const rawMax = visible.length ? Math.max(...visible) : 0
  const bound = Math.max(0.0001, rawMax * 1.08)
  const range = bound * 2

  const my = (v: number) => p.bottom - (v + bound) / range * (p.bottom - p.top)

  // 1. 零轴（虚线）
  drawLine(ctx, g.l, my(0), g.plotRight, my(0), C.grid2, 1, [4, 3])

  // 2. histogram 柱状图（颜色取自后端 sqzmom_bcolor）
  const barW = Math.max(1.5, step * 0.62)
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const v = vals[idx]
    if (v == null || typeof v === 'string') continue
    const vn = Number(v)
    const x = g.l + (i + 0.5) * step
    const y0 = my(0)
    const y1 = my(vn)
    // 颜色按 bar 取 sqzmom_bcolor，未识别时回退到红涨绿跌
    const bc = bcolors?.[idx]
    const colorHex = typeof bc === 'string' ? (SQZMOM_BCOLOR[bc] ?? (vn >= 0 ? '#ef5350' : '#26a69a')) : (vn >= 0 ? '#ef5350' : '#26a69a')
    ctx.fillStyle = colorHex
    ctx.fillRect(x - barW / 2, Math.min(y0, y1), barW, Math.max(1, Math.abs(y1 - y0)))
  }

  // 3. squeeze marker：在 0 轴画 cross 标记（颜色取自后端 sqzmom_scolor）
  // Pine 原代码：plot(0, color=scolor, style=cross, linewidth=2)
  for (let i = 0; i < indexes.length; i++) {
    const idx = indexes[i]
    if (idx == null) continue
    const sc = scolors?.[idx]
    if (typeof sc !== 'string') continue
    const colorHex = SQZMOM_SCOLOR[sc]
    if (!colorHex) continue
    const x = g.l + (i + 0.5) * step
    const y = my(0)
    ctx.strokeStyle = colorHex
    // [PROMPT.md §5.3.4 V2] SQZMOM squeeze marker 线宽按 scale.strokes.sqzMomLine（mobile_capture 2.5px）
    ctx.lineWidth = scale.strokes.sqzMomLine
    ctx.beginPath()
    ctx.moveTo(x - 3, y)
    ctx.lineTo(x + 3, y)
    ctx.moveTo(x, y - 3)
    ctx.lineTo(x, y + 3)
    ctx.stroke()
  }

  // 4. 右侧刻度标签
  // [PROMPT.md §5.3.4 V2] 副图刻度字号按 scale.fonts.paneTick（mobile_capture 30px）
  const _sqzTickOffsetTop = Math.round(parseFloat(scale.fonts.paneTick) * 1.0)
  const _sqzTickOffsetBottom = Math.round(parseFloat(scale.fonts.paneTick) * 0.2)
  drawText(ctx, fmt(bound, 3), g.plotRight + 5, p.top + _sqzTickOffsetTop, C.text, scale.fonts.paneTick)
  drawText(ctx, fmt(-bound, 3), g.plotRight + 5, p.bottom - _sqzTickOffsetBottom, C.text, scale.fonts.paneTick)

  // 5. 当前 val 标签
  let lastVal: number | undefined
  for (let i = indexes.length - 1; i >= 0; i--) {
    const idx = indexes[i]
    if (idx == null) continue
    const v = vals[idx]
    if (typeof v === 'number') { lastVal = v; break }
  }
  if (typeof lastVal === 'number') {
    const yVal = my(lastVal)
    // 用最后一个有效 bcolor 作为标签底色
    let labelColor = '#26a69a'
    for (let i = indexes.length - 1; i >= 0; i--) {
      const idx = indexes[i]
      if (idx == null) continue
      const b = bcolors?.[idx]
      if (typeof b === 'string' && SQZMOM_BCOLOR[b]) {
        labelColor = SQZMOM_BCOLOR[b]
        break
      }
    }
    ctx.fillStyle = labelColor
    // [PROMPT.md §5.3.4 V2] SQZMOM 当前值标签背景按 scale.geometry.paneCurrentBox* 缩放
    ctx.fillRect(g.plotRight + 1, yVal - scale.geometry.paneCurrentBoxHeight / 2, scale.geometry.paneCurrentBoxWidth, scale.geometry.paneCurrentBoxHeight)
    drawText(ctx, fmt(lastVal, 3), g.plotRight + scale.geometry.paneCurrentBoxWidth / 2, yVal + scale.geometry.paneCurrentBoxHeight / 4, '#fff', scale.fonts.paneCurrent, 'center')
  }
}

// [CHANGE-011 SMC] - 智能资金概念图层渲染
// 描述: 渲染 BOS/CHoCH 线、internal order blocks、EQH/EQL、trailing strong/weak high/low。
// 视觉规则（盘迹 V1）:
//   - 上涨结构红 #FF4D4F（bullish bias=1）
//   - 下跌结构绿 #22C55E（bearish bias=-1）
//   - internal 虚线（dash=[5,3]）且更淡（alpha 0.7），swing 实线
//   - BOS 实线，CHoCH 虚线 dash=[4,3]
//   - OB 同方向低透明度区域（alpha 0.12）
//   - mitigated OB 进一步降低透明度（alpha 0.05）
//   - 标签小而克制（8px sans-serif）
//   - 完全排除 FVG：不渲染任何 Fair Value Gap 元素
// [CHANGE-20260715-007] anchor/confirmed 因果契约（view adapter 后）:
//   - 后端 view adapter 已将索引重基准到展示窗口（offset = max(0, total - display)）
//   - SMC time 数组与 K 线 displayTimes 应 1:1 对齐（同 timeframe/adj/bars）
//   - BOS/CHoCH: anchor_index → confirmed_index
//   - OB: anchor_index → mitigated_index 或右端；clipped_left=True 时左端 clamp 到 plotLeft
//   - EQH/EQL: 线段画到 second_pivot_index（新 pivot 所在 bar）；
//     confirmed_index 用于因果确认/回放测试（不作为线段终点）
//   - swing_bias 字段显式返回，trailing 强/弱高/低直接使用该字段
//   - 仅渲染最近 5 个未 mitigated 的 internal OB
// [CHANGE-20260715-008] SMC 类型/配色/纯函数抽离至 ./smcRendering（可独立测试，无 React/Canvas 依赖）
import {
  selectVisibleSmcOrderBlocks,
  collectVisibleSmcPriceCandidates,
  intersectSmcRangeWithViewport,
  hexToRgba,
  layoutSmcLabels,
  SMC_BULL_COLOR,
  SMC_BEAR_COLOR,
  type SmcEvent,
  type SmcOrderBlock,
  type SmcEqualHighLow,
  type SmcTrailing,
  type SmcSwingBias,
  type SmcLabelAnchor,
  type SmcVisibleContext,
} from './smcRendering'

function renderIndicatorSmc(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  _layer: ChartLayer,
  data: Record<string, (number | string | null)[]>,
  displayTimes: string[],
  step: number,
  py: (v: number) => number,
  timeframe: string,
  scale: ChartRenderScale,
): void {
  // [CHANGE-011 SMC] - 数据字段为对象数组（非基本类型数组），按 any 取值后 cast 到内部类型
  // FVG 完全排除：本函数不渲染任何 Fair Value Gap 元素
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const smcData = data as unknown as Record<string, any>
  const events: SmcEvent[] = Array.isArray(smcData.events) ? smcData.events : []
  const orderBlocks: SmcOrderBlock[] = Array.isArray(smcData.order_blocks) ? smcData.order_blocks : []
  const equalHLs: SmcEqualHighLow[] = Array.isArray(smcData.equal_highs_lows) ? smcData.equal_highs_lows : []
  const trailing: SmcTrailing | null = smcData.trailing && typeof smcData.trailing === 'object' ? smcData.trailing : null
  // CHANGE-20260715-007: swing_bias 为数值（1/-1/0），缺失时默认 0
  const swingBias: SmcSwingBias = (typeof smcData.swing_bias === 'number'
    ? smcData.swing_bias
    : 0) as SmcSwingBias
  const smcTimes: (string | null)[] = Array.isArray(smcData.time) ? smcData.time : []

  if (!displayTimes.length) return

  // 构造 SMC time 数组索引 → K 线 display 索引的映射
  // [CHANGE-20260715-007] view adapter 已将索引重基准到展示窗口，
  // SMC time 数组与 K 线 displayTimes 应 1:1 对齐；时间匹配作为防御性回退
  const klineTimeIndex = new Map<string, number>()
  displayTimes.forEach((t, i) => {
    const key = normalizeChartTime(t, timeframe)
    if (key != null) klineTimeIndex.set(key, i)
  })

  // [CP-V3-C] 直接 time → display index 查找（viewport 250→90 切换时最可靠）
  // 用于 SmcVisibleContext.timeToDisplayIndex 回调，被 smcRendering 纯函数使用
  const smcTimeToDisplay = (time: string | null | undefined): number | undefined => {
    if (time == null) return undefined
    const key = normalizeChartTime(time, timeframe)
    if (key == null) return undefined
    const idx = klineTimeIndex.get(key)
    return idx  // undefined when not in viewport
  }

  // [CP-V3-C] SmcVisibleContext: 提供 timeToDisplayIndex 回调，让纯函数优先用 time 匹配
  const smcVisCtx: SmcVisibleContext = {
    displayCount: displayTimes.length,
    timeToDisplayIndex: smcTimeToDisplay,
  }

  // 辅助：SMC time 数组索引 → K 线 display 索引
  // [CP-V3-C] 新增可选 time 参数：primary 路径直接用 anchor_time 等字段匹配
  // （比 smcTimes[smcIdx] 间接匹配更可靠，viewport 250→90 切换时不会因索引 rebasing 错位）
  const smcToDisplay = (
    smcIdx: number | null | undefined,
    time?: string | null,
  ): number | undefined => {
    // [CP-V3-C] Primary: 直接用 time 字段匹配（最可靠）
    if (time != null) {
      const idx = smcTimeToDisplay(time)
      if (idx != null) return idx
    }
    // Fallback: 索引路径（依赖 adapter rebasing）
    if (smcIdx == null) return undefined
    // 负索引（view adapter clipped_left 时 anchor 在窗口左侧）→ clamp 到 0
    if (smcIdx < 0) return 0
    // 索引在 SMC time 数组范围内 → 时间匹配
    if (smcIdx < smcTimes.length) {
      const t = smcTimes[smcIdx]
      if (t != null) {
        const key = normalizeChartTime(t, timeframe)
        if (key != null) {
          const klineIdx = klineTimeIndex.get(key)
          if (klineIdx != null) return klineIdx
        }
      }
    }
    // 索引超出 SMC time 数组但可能在 K 线范围内 → 直接用作 display 索引（adapter 已重基准）
    if (smcIdx < displayTimes.length) return smcIdx
    return undefined
  }

  // [CP-V3-C] SMC 事件区间（anchor + confirmed）映射到 display 索引
  // 与 mapSmcIndexToDisplay 不同：保留 raw index 作为 fallback，不 clamp，
  // 让 intersectSmcRangeWithViewport 正确判断 clipped_left/clipped_right
  const mapSmcEventRange = (
    anchorIdx: number | null | undefined,
    anchorTime: string | null | undefined,
    confirmedIdx: number | null | undefined,
    confirmedTime: string | null | undefined,
  ): { anchorDisplay: number | null; confirmedDisplay: number | null } => {
    // anchor: primary = time, fallback = raw index（保留负值表示 clipped_left）
    let anchorDisplay: number | null = null
    if (anchorTime != null) {
      const idx = smcTimeToDisplay(anchorTime)
      if (idx != null) anchorDisplay = idx
    }
    if (anchorDisplay == null && anchorIdx != null) {
      anchorDisplay = anchorIdx  // 保留 raw（可能 < 0 或 >= displayCount）
    }
    // confirmed: primary = time, fallback = raw index（保留 >= displayCount 表示 clipped_right）
    let confirmedDisplay: number | null = null
    if (confirmedTime != null) {
      const idx = smcTimeToDisplay(confirmedTime)
      if (idx != null) confirmedDisplay = idx
    }
    if (confirmedDisplay == null && confirmedIdx != null) {
      confirmedDisplay = confirmedIdx
    }
    return { anchorDisplay, confirmedDisplay }
  }

  // [2026-07-21 P0 反馈] SMC 标签碰撞布局：收集所有标签锚点，mobile_capture 下用 layoutSmcLabels 防重叠
  // desktop 模式保持自然位置（250 bar 窗口下碰撞罕见）
  const labelAnchors: SmcLabelAnchor[] = []
  const isMobileCapture = scale.density === 'mobile_capture'

  // ===== 1. 渲染 Order Blocks（低透明度矩形区域）=====
  // [CHANGE-20260715-008] OB 选择逻辑抽离至 selectVisibleSmcOrderBlocks（可独立测试）
  // 规则（PROMPT.md §四.2）：
  //   - 只画 internal===true && mitigated===false（mitigated OB 不再渲染）
  //   - 后端最新 OB 在数组头部 → slice(0, 5)
  //   - clipped_left=True 时左端 clamp 到 plotLeft（g.l）
  //   - x2 = 可见区右端（OB 未 mitigated → 延伸到当前可见区末尾）
  //   - 与 viewport 无交集时跳过（由 selectVisibleSmcOrderBlocks 过滤）
  const visibleObs = selectVisibleSmcOrderBlocks(orderBlocks, smcVisCtx)
  for (const ob of visibleObs) {
    // [CP-V3-C] 优先用 ob.anchor_time 匹配（viewport 250→90 切换时最可靠）
    const anchorDisplayIdx = smcToDisplay(ob.anchor_index, ob.anchor_time)
    if (anchorDisplayIdx == null) continue
    // x2 = 可见区右端（plotRight）：OB 未 mitigated → 延伸到当前可见区末尾
    const x2 = g.plotRight
    // x1: clipped_left 时 clamp 到 plotLeft（g.l）；否则取 anchor 位置
    let x1 = g.l + (anchorDisplayIdx + 0.5) * step
    // [CHANGE-20260715-007] clipped_left: anchor 在窗口左侧时 clamp 到 plotLeft
    if (ob.clipped_left === true || ob.anchor_index < 0) {
      x1 = g.l
    }
    // 与 viewport 无交集时跳过（x1 已超出右端）
    if (x1 > x2) continue
    const yHigh = py(ob.bar_high)
    const yLow = py(ob.bar_low)
    const yTop = Math.min(yHigh, yLow)
    const height = Math.max(2, Math.abs(yHigh - yLow))

    // 颜色与透明度（仅未 mitigated OB，alpha 0.12）
    const isBull = ob.bias === 1
    const color = isBull ? SMC_BULL_COLOR : SMC_BEAR_COLOR
    ctx.fillStyle = hexToRgba(color, 0.12)
    ctx.fillRect(x1, yTop, Math.max(1, x2 - x1), height)

    // OB 边框（更淡）
    ctx.strokeStyle = hexToRgba(color, 0.3)
    // [PROMPT.md §5.3.4 V2] OB 边框线宽按 scale.strokes.obBorder（mobile_capture 2px）
    ctx.lineWidth = scale.strokes.obBorder
    ctx.strokeRect(x1, yTop, Math.max(1, x2 - x1), height)

    // [2026-07-21 反馈] OB 区域内加中文文字标签（多头承接区/空头压制区）
    //   [P0 碰撞布局] mobile_capture 下收集锚点，由 layoutSmcLabels 统一布局
    //   desktop 模式不渲染 OB 文字标签（区域太窄）
    if (isMobileCapture && height > 40 && (x2 - x1) > 80) {
      const obLabel = getSmcObLabel(ob.bias)
      const obLabelY = yTop + height / 2
      labelAnchors.push({
        kind: 'ob',
        anchorX: x1 + 8,
        anchorY: obLabelY,
        text: obLabel,
        color: hexToRgba(color, 0.85),
        fontSize: scale.fonts.smcInternalLabel,
        align: 'left',
        preferredVertical: 'center',
      })
    }
  }

  // ===== 2. 渲染 BOS/CHoCH 线 =====
  // [CHANGE-20260716-001] viewport 区间求交（PROMPT.md §三.2）：
  //   只要区间与viewport相交就绘制，不再要求 anchor 和 confirmed 都在 displayTimes 中。
  //   anchor 在左侧时 x1=plotLeft（g.l）；confirmed 在右侧时 x2=plotRight。
  //   仅完全不相交时跳过。
  // CHANGE-20260715-002: internal=虚线 dash=[4,3] + tiny(8px)标签；swing=实线 + small(11px)标签
  // 标签位于结构线中点（非左端）
  // internal: 更淡（alpha 0.7），更细（width 1）；swing: 实色，更粗（width 1.5）
  // [CHANGE-20260716-001] 标签不加 ·I，与 TV 文字一致（PROMPT.md §三.3）
  // [CP-V3-C] 优先用 anchor_time/confirmed_time 匹配（viewport 250→90 切换时最可靠）
  // mapSmcEventRange 保留 raw index 作为 fallback（不 clamp），让 intersectSmcRangeWithViewport
  // 正确判断 clipped_left/clipped_right
  for (const ev of events) {
    if (ev.level == null) continue
    const { anchorDisplay, confirmedDisplay } = mapSmcEventRange(
      ev.anchor_index, ev.anchor_time,
      ev.confirmed_index, ev.confirmed_time,
    )
    const range = intersectSmcRangeWithViewport(anchorDisplay, confirmedDisplay, smcVisCtx)
    if (range == null) continue

    const x1 = g.l + (range.startIdx + 0.5) * step
    const x2 = g.l + (range.endIdx + 0.5) * step
    const y = py(ev.level)

    const isBull = ev.bias === 1
    const baseColor = isBull ? SMC_BULL_COLOR : SMC_BEAR_COLOR
    const isInternal = ev.internal === true
    const alpha = isInternal ? 0.7 : 1.0
    // [PROMPT.md §5.3.4 V2] SMC internal 线宽按 scale.strokes.smcInternal（mobile_capture 2px），
    //   swing 线宽按 scale.strokes.smcSwing（mobile_capture 3px）
    const lineWidth = isInternal ? scale.strokes.smcInternal : scale.strokes.smcSwing
    const color = hexToRgba(baseColor, alpha)

    // CHANGE-20260715-002: internal=虚线，swing=实线（不再按 BOS/CHoCH 区分线型）
    if (isInternal) {
      drawLine(ctx, x1, y, x2, y, color, lineWidth, [4, 3])
    } else {
      // swing: 实线
      drawLine(ctx, x1, y, x2, y, color, lineWidth, [])
    }

    // 标签位于结构线中点（CHANGE-20260715-002）
    // [CHANGE-20260716-001] 标签不加 ·I，与 TV 文字一致
    // [2026-07-21 反馈] 标签改用中文通俗名词（突破前高/跌破前低/转强拐点/转弱拐点）
    // [P0 碰撞布局] 收集锚点，由 layoutSmcLabels（mobile_capture）或自然位置（desktop）统一绘制
    const midX = (x1 + x2) / 2
    const label = getSmcEventLabel(ev.type, ev.bias)
    const fontSize = isInternal ? scale.fonts.smcInternalLabel : scale.fonts.smcSwingLabel
    const labelOffset = Math.round(parseFloat(fontSize) * 0.3)
    labelAnchors.push({
      kind: ev.type === 'BOS' ? 'bos' : 'choch',
      anchorX: midX,
      anchorY: y - labelOffset,
      text: label,
      color,
      fontSize,
      align: 'center',
      preferredVertical: 'up',
    })
  }

  // ===== 3. 渲染 EQH/EQL（两端点线，非水平线）=====
  // [CHANGE-20260717-001 Pine parity] Pine L396: 两端点线
  //   line.new(p_ivot.barTime/currentLevel, time[size]/level)
  //   - 起点：anchor_index, prev_level（前一 pivot 的 level）
  //   - 终点：second_pivot_index, level（新 pivot 的 level，可能不水平）
  //   - EQH: bearish 色 (SMC_BEAR_COLOR 绿) + label_down
  //   - EQL: bullish 色 (SMC_BULL_COLOR 红) + label_up
  //   - 标签位于两 pivot 中点（Pine L397: math.round(0.5*(p_ivot.barIndex + bar_index - size))）
  for (const eq of equalHLs) {
    // [CP-V3-C] 优先用 anchor_time/second_pivot_time 匹配（viewport 250→90 切换时最可靠）
    const { anchorDisplay, confirmedDisplay } = mapSmcEventRange(
      eq.anchor_index, eq.anchor_time,
      eq.second_pivot_index, eq.second_pivot_time,
    )
    const range = intersectSmcRangeWithViewport(anchorDisplay, confirmedDisplay, smcVisCtx)
    if (range == null) continue

    const x1 = g.l + (range.startIdx + 0.5) * step
    const x2 = g.l + (range.endIdx + 0.5) * step
    // 两端点 Y：起点用 prev_level，终点用 level（可能不水平）
    const y1 = py(eq.prev_level)
    const y2 = py(eq.level)

    const isEQH = eq.type === 'EQH'
    // Pine L384/L389: EQH=swingBearishColor, EQL=swingBullishColor
    const eqColor = isEQH ? SMC_BEAR_COLOR : SMC_BULL_COLOR

    drawLine(ctx, x1, y1, x2, y2, eqColor, scale.strokes.eqLine, [2, 2])

    // 标签位于两 pivot 中点（Pine L397）
    // [2026-07-21 反馈] 标签改用中文通俗名词（双顶压力/双底支撑）
    // [P0 碰撞布局] 收集锚点，由 layoutSmcLabels（mobile_capture）或自然位置（desktop）统一绘制
    const midX = (x1 + x2) / 2
    const midY = (y1 + y2) / 2
    // EQH: label_down（标签在上方）；EQL: label_up（标签在下方）
    // [PROMPT.md §5.3.4 V2] EQ 标签字号按 scale.fonts.eqLabel（mobile_capture 34px）
    const _eqSize = parseFloat(scale.fonts.eqLabel)
    const labelYOffset = isEQH ? -Math.round(_eqSize * 0.3) : Math.round(_eqSize * 0.9)
    labelAnchors.push({
      kind: isEQH ? 'eqh' : 'eql',
      anchorX: midX,
      anchorY: midY + labelYOffset,
      text: getSmcEqLabel(eq.type),
      color: eqColor,
      fontSize: scale.fonts.eqLabel,
      align: 'center',
      preferredVertical: isEQH ? 'up' : 'down',
    })
  }

  // ===== 4. 渲染 trailing strong/weak high/low =====
  // CHANGE-20260715-007: Strong/Weak 直接读取 DTO swing_bias（state.swing_trend.bias）
  // 禁止从最后一个可见 swing 事件猜测
  // 规则：bias===-1 → 强高（否则弱高）；bias===1 → 强低（否则弱低）
  // [CHANGE-20260717-001 Pine parity] Pine L721-727:
  //   线起点 = trailing.lastTopTime/lastBottomTime（非最后可见 bar）
  //   终点 = last_bar_time + 20*(time-time[1])（向右延伸约 20 bar）
  //   标签位于终点

  // 辅助：ISO 时间字符串 → display index（通过 normalizeChartTime 匹配）
  const timeToDisplayIdx = (isoTime: string | null | undefined): number => {
    if (isoTime == null) return 0  // 缺失时 clamp 到窗口左端
    const key = normalizeChartTime(isoTime, timeframe)
    if (key != null) {
      const idx = klineTimeIndex.get(key)
      if (idx != null) return idx
    }
    return 0  // 找不到时 clamp 到窗口左端
  }

  if (trailing && trailing.top != null) {
    // Pine L721: 线起点 = trailing.lastTopTime
    const startIdx = timeToDisplayIdx(trailing.last_top_time)
    const x1 = g.l + (startIdx + 0.5) * step
    // Pine L722: 线终点 = last_bar_time + 20 bar（向右延伸，clamp 到 plotRight）
    const x2 = g.plotRight
    const y = py(trailing.top)
    // 强高=红色，弱高=绿色（bias=-1 时为强高，否则弱高）
    const isStrong = swingBias === -1
    const labelColor = isStrong ? SMC_BULL_COLOR : SMC_BEAR_COLOR
    const label = `${isStrong ? '强高' : '弱高'} ${fmt(trailing.top)}`
    // [PROMPT.md §5.3.4 V2] trailing 线宽按 scale.strokes.smcSwing，标签按 scale.fonts.smcSwingLabel
    drawLine(ctx, x1, y, x2, y, hexToRgba(labelColor, 0.5), scale.strokes.smcSwing, [3, 3])
    // [P0 碰撞布局] 收集锚点，由 layoutSmcLabels 统一布局
    labelAnchors.push({
      kind: 'trailing_high',
      anchorX: g.plotRight - 4,
      anchorY: y - 3,
      text: label,
      color: labelColor,
      fontSize: scale.fonts.smcSwingLabel,
      align: 'right',
      preferredVertical: 'up',
    })
  }
  if (trailing && trailing.bottom != null) {
    // Pine L726: 线起点 = trailing.lastBottomTime
    const startIdx = timeToDisplayIdx(trailing.last_bottom_time)
    const x1 = g.l + (startIdx + 0.5) * step
    const x2 = g.plotRight
    const y = py(trailing.bottom)
    // 强低=绿色，弱低=红色（bias=1 时为强低，否则弱低）
    const isStrong = swingBias === 1
    const labelColor = isStrong ? SMC_BEAR_COLOR : SMC_BULL_COLOR
    const label = `${isStrong ? '强低' : '弱低'} ${fmt(trailing.bottom)}`
    drawLine(ctx, x1, y, x2, y, hexToRgba(labelColor, 0.5), scale.strokes.smcSwing, [3, 3])
    // [P0 碰撞布局] 收集锚点，由 layoutSmcLabels 统一布局
    labelAnchors.push({
      kind: 'trailing_low',
      anchorX: g.plotRight - 4,
      anchorY: y + 9,
      text: label,
      color: labelColor,
      fontSize: scale.fonts.smcSwingLabel,
      align: 'right',
      preferredVertical: 'down',
    })
  }

  // ===== 5. 绘制所有 SMC 标签 =====
  // [P0 碰撞布局] mobile_capture: 用 layoutSmcLabels 防重叠 + 短引导线
  //   desktop: 自然位置绘制（250 bar 窗口下碰撞罕见，保持原行为）
  if (labelAnchors.length === 0) return

  if (isMobileCapture) {
    // mobile_capture: 碰撞布局 + 引导线
    // [P0 fix] Geometry 接口无 t/b，主图 pane 边界通过 g.panes.price.top/bottom 获取
    //   [P0 fix] scale 无 fontFamily，fontSize 字段已是完整 CSS font 字符串（如 "36px sans-serif"）
    const fontSizeNum = parseFloat(scale.fonts.smcSwingLabel)
    const laneHeight = fontSizeNum + 4
    const laidOut = layoutSmcLabels(
      labelAnchors,
      {
        plotLeft: g.l,
        plotRight: g.plotRight,
        plotTop: g.panes.price.top,
        plotBottom: g.panes.price.bottom,
        laneHeight,
        laneGap: 4,
        maxLanes: 4,
      },
      (text: string, fs: string) => {
        ctx.font = fs  // fs 已是完整 CSS font 字符串（如 "36px sans-serif"）
        return ctx.measureText(text).width
      },
    )
    // 先画引导线（在标签下方）
    // [P0 fix] 引导线颜色需同时支持 hex 和 rgba 输入：
    //   - OB/BOS/CHoCH 标签 color 是 rgba(...)（已带 alpha）
    //   - EQH/EQL/trailing 标签 color 是 #RRGGBB hex
    //   统一提取 r,g,b 后用 0.4 alpha 重绘引导线
    for (const label of laidOut) {
      if (label.lane === 0) continue  // lane 0 = 自然位置，无需引导线
      const guideColor = (() => {
        const c = label.anchor.color
        const m = c.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/)
        if (m) return `rgba(${m[1]}, ${m[2]}, ${m[3]}, 0.4)`
        return hexToRgba(c, 0.4)
      })()
      ctx.strokeStyle = guideColor
      ctx.lineWidth = 1
      ctx.setLineDash([2, 2])
      ctx.beginPath()
      ctx.moveTo(label.guideStartX, label.guideStartY)
      ctx.lineTo(label.guideEndX, label.guideEndY)
      ctx.stroke()
      ctx.setLineDash([])
    }
    // 再画标签
    for (const label of laidOut) {
      const align = label.anchor.align === 'left' ? 'left'
        : label.anchor.align === 'right' ? 'right' : 'center'
      const tx = align === 'left' ? label.boxX + 4
        : align === 'right' ? label.boxX + label.boxW - 4
        : label.boxX + label.boxW / 2
      const ty = label.boxY + label.boxH / 2 + parseFloat(label.anchor.fontSize) * 0.35
      drawText(ctx, label.anchor.text, tx, ty, label.anchor.color, label.anchor.fontSize, align)
    }
  } else {
    // desktop: 自然位置绘制（无碰撞布局，无引导线）
    for (const anchor of labelAnchors) {
      drawText(ctx, anchor.text, anchor.anchorX, anchor.anchorY, anchor.color, anchor.fontSize, anchor.align)
    }
  }
}

// [CHANGE-011 SMC] - hexToRgba 已抽离至 ./smcRendering（与 SMC 纯函数同居）

// ===== 事件映射与可见性 =====

// 事件类型 -> 颜色
function eventColor(type: string): string {
  if (/selection|hit/i.test(type)) return C.cyan
  if (/node/i.test(type)) return C.yellow
  if (/atr|rope/i.test(type)) return C.blue2
  if (/delta/i.test(type)) return C.cyan
  if (/composite|combo|confirmed/i.test(type)) return C.orange
  return C.text2
}

// 是否为选股命中事件（三角形标记在 K 线下方）
function isSelectionHit(type: string): boolean {
  return /selection|hit/i.test(type)
}

// 将归一化时间字符串转成可比较大小的分钟序数（不依赖 Date.getTime）
function normalizedTimeValue(t: string): number {
  const m = t.match(/^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?$/)
  if (!m) return NaN
  const [, y, mo, d, h = '00', min = '00'] = m
  // 近似分钟序数：仅用于在同一周期内找最近 bar，不要求绝对精确
  return ((+y * 12 + +mo) * 31 + +d) * 1440 + (+h * 60 + +min)
}

// 将 props 事件映射到 bar 索引
function mapEvents(events: ChartEvent[], display: CalculatedBar[], timeframe: string): MappedEvent[] {
  const timeIndex = new Map<string, number>()
  display.forEach((d, i) => {
    const key = normalizeChartTime(d.time, timeframe)
    if (key != null && !timeIndex.has(key)) timeIndex.set(key, i)
  })
  return events.map((ev, n) => {
    const key = normalizeChartTime(ev.time, timeframe)
    let bestIdx = key != null ? timeIndex.get(key) : undefined
    if (bestIdx == null && key != null) {
      // fallback：按归一化时间字符串找最近 bar，避免 Date.getTime() 时区歧义
      const evVal = normalizedTimeValue(key)
      if (!Number.isNaN(evVal)) {
        bestIdx = 0
        let bestDiff = Infinity
        display.forEach((d, i) => {
          const dKey = normalizeChartTime(d.time, timeframe)
          if (dKey == null) return
          const dVal = normalizedTimeValue(dKey)
          if (Number.isNaN(dVal)) return
          const diff = Math.abs(dVal - evVal)
          if (diff < bestDiff) {
            bestDiff = diff
            bestIdx = i
          }
        })
      }
    }
    const d = display[bestIdx ?? 0]
    const sel = isSelectionHit(ev.type)
    return {
      ...ev,
      id: `evt_${n}`,
      index: bestIdx ?? 0,
      price: sel ? d.low : d.high,
      color: eventColor(ev.type),
    }
  })
}

// 事件是否可见（基于图层开关）
function isEventVisible(ev: MappedEvent, layers: LayerVisibility): boolean {
  if (!layers.events) return false
  const type = ev.type
  if (/selection|hit/i.test(type)) return layers.selection
  if (/node/i.test(type)) return layers.node
  if (/atr|rope/i.test(type)) return layers.node
  if (/composite|combo|confirmed/i.test(type)) return layers.node || layers.delta
  return true
}

// [Volume Profile] - 行 tooltip 内容（从后端 ProfileRow + ProfileMeta 生成）
function profileTooltip(row: ProfileRow, profile: BackendProfile): string {
  const totalSum = profile.rows.reduce((s, r) => s + r.total_volume, 0)
  const share = row.total_volume / Math.max(1, totalSum) * 100
  const node = profile.nodes.find(n => row.price_mid >= n.lo && row.price_mid <= n.hi)
  return `<b>${fmt(row.price_low)}–${fmt(row.price_high)}</b><span>\u603b\u6210\u4ea4量 ${(row.total_volume / 10000).toFixed(1)}万 · ${share.toFixed(2)}%</span><span>\u4e70量 ${(row.bullish_volume / 10000).toFixed(1)}万 · \u5356量 ${(row.bearish_volume / 10000).toFixed(1)}万</span><span>价值区 ${row.is_value_area ? '是' : '否'} · 节点 ${node ? node.id : '—'}${row.is_poc ? ' · 核心共识价' : ''}${row.is_peak ? ' · 共识价' : ''}</span>`
}

// ===== 主绘制函数（对齐原型 drawTrading 渲染管线）=====
function drawTrading(
  canvas: HTMLCanvasElement,
  calc: CalculatedBar[],
  display: CalculatedBar[],
  mappedEvents: MappedEvent[],
  layers: LayerVisibility,
  timeframe: string,
  state: ChartState,
  indicators?: IndicatorResponse | undefined,
): void {
  if (!display.length) return
  const { ctx, w, h } = fit(canvas)
  // [PROMPT.md §5.3.4 V2] 从 state.scale 读取字体/线宽/几何（desktop | mobile_capture）
  //   所有 draw 子函数与内联 drawText/drawLine 调用必须使用 scale.*，禁止硬编码 '8px monospace' 等
  const { scale } = state
  const layerSet = new Set(Object.entries(layers).filter(([, v]) => v).map(([k]) => k))
  const g = geometry(layerSet, w, h, scale)
  // [Volume Profile] - 从后端 indicators 提取 VP 数据（SSOT，禁止前端重算）
  const profile = extractBackendProfile(indicators)
  const displayTimes = display.map(d => d.time)

  // [chartViewport] - 纵轴范围：可见 K 线 high/low + 可见 BB upper/lower + 可见 DSA VWAP + 可见节点区间，上下各留约 3% padding
  // [ChartRenderFrame] - 纵轴 domain policy：先计算可见 K线价格区间 + 容差，
  //   后续 Node/SMC trailing 候选过滤掉远端历史价位，避免纵轴被非可见指标扩张
  //   详见 PROMPT.md §五.255-282（一个远端 Node 把纵轴拉大 → K线被压缩）
  const priceCandidates: number[] = []
  display.forEach(d => {
    priceCandidates.push(d.low, d.high)
  })
  const visibleBounds = computeVisiblePriceBounds(
    display.map(d => d.low),
    display.map(d => d.high),
  )

  // [CHANGE-20260719-003 §四] time-index map 共享（PROMPT.md §4 要求"共享 time-index map"）
  //   每个 layer 的 (displayTimes, layerData.time, timeframe) → indexMap 在一次 drawTrading 中只计算一次，
  //   纵轴候选计算和后续 renderIndicatorLayer 共享同一份，避免每层重复 O(n) 构建。
  //   key = layer.strategy_id；layerData.time 不同的 layer 各自缓存独立 entry。
  const layerIndexMaps = new Map<string, (number | undefined)[]>()

  if (indicators?.layers && indicators?.data) {
    indicators.layers.forEach(layer => {
      // [DSA 数据契约] - union 类型（DsaSelectorData | Record）按 Record 索引访问，dsa_polyline 渲染器内部再 cast 到 DsaSelectorData
      const layerData = indicators.data![layer.strategy_id] as Record<string, (number | string | null)[]>
      if (!layerData) return
      const indexMap = buildDisplayIndexMap(displayTimes, layerData.time, timeframe)
      layerIndexMaps.set(layer.strategy_id, indexMap)
      if (layer.layer_id === 'bb' && layers.bb) {
        const upperVals = layerData[layer.fields[0]]
        const lowerVals = layerData[layer.fields[1]]
        indexMap.forEach(idx => {
          if (idx == null) return
          const u = upperVals?.[idx]
          const l = lowerVals?.[idx]
          if (typeof u === 'number') priceCandidates.push(u)
          if (typeof l === 'number') priceCandidates.push(l)
        })
      }
      // [DSA Overlay Policy] - DSA 纵轴范围候选决策：开关 / 周期支持
      // [PR #33] - 移除 `timeframe === '1d'` 硬编码，全周期 DSA 都参与 y-axis range
      if (shouldIncludeDsaInPriceRange(layer.layer_id, layers, timeframe)) {
        const vwapField = layer.fields.find(f => /vwap/i.test(f)) || layer.fields[0]
        const vwapVals = layerData[vwapField]
        indexMap.forEach(idx => {
          if (idx == null) return
          const v = vwapVals?.[idx]
          if (typeof v === 'number') priceCandidates.push(v)
        })
      }
    })
  }

  // [ChartRenderFrame] - Node 纵轴 domain policy（PROMPT.md §五.255-282）
  //   旧行为：profile.nodes.forEach(n => priceCandidates.push(n.lo, n.hi))
  //   问题：远端历史高位/低位 Node 把纵轴拉大，K线被压缩，指标比例看起来错误
  //   新行为：shouldIncludeNodeInPriceRange 过滤远端 Node（可见区间 ± 50% 容差）
  //   注意：被过滤的 Node 仍由 Node 图层正常渲染（被 Canvas 裁剪），只是不参与纵轴
  if (layers.node && profile?.nodes) {
    profile.nodes.forEach(n => {
      if (shouldIncludeNodeInPriceRange(n, visibleBounds)) {
        priceCandidates.push(n.lo, n.hi)
      }
    })
  }

  // [CHANGE-011 SMC] - SMC 纵轴价格候选（PROMPT.md §四.3）
  // 加入当前可见的：event.level / OB bar_high,bar_low / EQH,EQL level / trailing top,bottom
  // 目的：避免 SMC 元素被画出 Canvas（纵轴范围必须包含所有可见 SMC 价格）
  // [ChartRenderFrame] - trailing top/bottom 额外应用 domain policy：
  //   collectVisibleSmcPriceCandidates 把 trailing 视为始终可见（"当前最新结构极值"），
  //   但若 trailing 来自很久以前的 bar，可能远超当前可见价格，导致纵轴被拉大。
  //   这里对 smcCandidates 中等于 trailing.top/bottom 的值额外应用 domain policy 过滤。
  //   注意：若 event.level 恰好等于 trailing 值会被一并过滤（概率极低，可接受）。
  if (layers.smc && indicators?.layers && indicators?.data) {
    const smcLayer = indicators.layers.find(l => l.layer_id === 'smc')
    if (smcLayer) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const smcLayerData = indicators.data[smcLayer.strategy_id] as any
      if (smcLayerData && typeof smcLayerData === 'object') {
        const smcCandidates = collectVisibleSmcPriceCandidates(
          smcLayerData,
          { displayCount: displayTimes.length },
        )
        const trailing = smcLayerData.trailing
        const trailingTop = trailing?.top
        const trailingBottom = trailing?.bottom
        if (trailingTop != null || trailingBottom != null) {
          // 对 trailing 贡献的候选值应用 domain policy；其他候选（event/OB/EQH 已按窗口过滤）直接保留
          const filtered = smcCandidates.filter(v => {
            if (v === trailingTop || v === trailingBottom) {
              return shouldIncludeSmcTrailingInPriceRange(v, visibleBounds)
            }
            return true
          })
          priceCandidates.push(...filtered)
        } else {
          priceCandidates.push(...smcCandidates)
        }
      }
    }
  }

  const rawMin = priceCandidates.length ? Math.min(...priceCandidates) : Math.min(...display.map(d => d.low))
  const rawMax = priceCandidates.length ? Math.max(...priceCandidates) : Math.max(...display.map(d => d.high))
  const range = Math.max(rawMax - rawMin, rawMin * 0.001)
  const padding = range * 0.03
  const min = rawMin - padding
  const max = rawMax + padding
  const py = (v: number) => g.panes.price.top + (max - v) / (max - min) * (g.panes.price.bottom - g.panes.price.top)
  const plotW = g.plotRight - g.l
  // [ChartRightPadding] - 右侧 20% 留白：bars 只占据绘图区前 80%，最新 bar 位于约 80% 位置
  // 所有交互坐标映射（十字线/滚轮锚点/Pointer 拖拽/命中）统一使用此 step，自动同步到压缩后的 bar 分布
  const effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)
  const step = effectivePlotW / display.length
  // [PROMPT.md §5.3.4 V2] K 线最小实体宽按 scale.strokes.candleBodyMin（mobile_capture ≥4px）
  const barW = Math.max(scale.strokes.candleBodyMin, step * 0.56)

  // 1. 背景 + 网格
  drawGrid(ctx, w, h, g, min, max, scale)

  // 2. 右侧 Volume Profile（后端 profile_rows 直接渲染；缺失时显示提示）
  if (layers.profile) {
    if (profile && profile.rows.length > 0) {
      renderProfile(ctx, profile, g, py, state, layerSet)
    } else {
      // 后端 VP 数据缺失：在 VP 区域中央显示灰色提示（禁止降级到前端算法）
      // [筹码共识价] - 描述: 缺失提示文案统一为"筹码共识价暂不可用"（基于历史成交量分布的估算代理）
      const cx = (g.profileStart + g.profileEnd) / 2
      const cy = (g.panes.price.top + g.panes.price.bottom) / 2
      drawText(ctx, '筹码共识价暂不可用', cx, cy, C.text, scale.fonts.emptyHint, 'center')
    }
  }

  // 3. Node Cluster 主图叠加（从后端 upper_node/lower_node/peak_rows 提取的节点，含多空量标签与迷你多空柱）
  if (layers.node && profile && profile.nodes.length > 0) {
    const backendNodes = profile.nodes
    const maxVol = Math.max(...backendNodes.map(n => Math.max(n.bullish_volume, n.bearish_volume)), 1)
    // [PROMPT.md §5.3.4 V2] Node 标签/多空量标签字号按 scale.fonts.nodeLabel / nodeVolLabel 缩放
    const nodeLabelOffset = Math.round(parseFloat(scale.fonts.nodeLabel) * 0.9)
    const nodeVolLabelOffset = Math.round(parseFloat(scale.fonts.nodeVolLabel) * 2.4)
    backendNodes.forEach(n => {
      const y1 = py(n.hi)
      const y2 = py(n.lo)
      const selected = state.selectedNodeId === n.id
      ctx.fillStyle = n.poc ? 'rgba(255,152,0,.11)' : selected ? 'rgba(156,179,255,.15)' : 'rgba(79,124,255,.075)'
      ctx.fillRect(g.l, y1, plotW, y2 - y1)
      // [PROMPT.md §5.3.4 V2] Node 主线宽按 scale.strokes.nodeLine 缩放（mobile_capture 2.5px）
      drawLine(ctx, g.l, py(n.mid), g.plotRight, py(n.mid), n.poc ? C.orange : selected ? '#dce6ff' : C.blue, selected ? scale.strokes.nodeLine * 1.5 : scale.strokes.nodeLine, n.poc ? [8, 4] : [4, 5])
      // [筹码共识价] - 描述: 节点价格标签（POC=核心共识价，普通峰=共识价）
      // 文案仅为展示，内部 n.poc/字段名不变；筹码共识价是基于历史成交量分布的估算代理
      const labelText = n.poc ? `核心共识价 ${fmt(n.mid)}` : `共识价 ${fmt(n.mid)}`
      drawText(ctx, labelText, g.l + 5, y1 + nodeLabelOffset, n.poc ? C.orange : C.blue, scale.fonts.nodeLabel)
      // 多空量标签 + 迷你多空柱（A 股：多头红色 / 空头绿色）
      if (n.bullish_volume > 0 || n.bearish_volume > 0) {
        const volText = `多 ${formatVolume(n.bullish_volume)} / 空 ${formatVolume(n.bearish_volume)}`
        drawText(ctx, volText, g.l + 5, y1 + nodeVolLabelOffset, C.text2, scale.fonts.nodeVolLabel)
        // 迷你多空柱：在节点垂直中心绘制水平柱
        const nodeH = y2 - y1
        const barH = Math.max(2, nodeH * 0.3)
        const barY = y1 + nodeH * 0.5 - barH / 2
        const maxBarW = plotW * 0.25
        const bullW = n.bullish_volume / maxVol * maxBarW
        const bearW = n.bearish_volume / maxVol * maxBarW
        ctx.fillStyle = 'rgba(239,83,80,0.85)'
        ctx.fillRect(g.l + 5, barY, bullW, barH)
        ctx.fillStyle = 'rgba(38,166,154,0.85)'
        ctx.fillRect(g.l + 5 + bullW, barY, bearW, barH)
      }
    })
  }

  // 4. POC 中心线（从后端 profile_meta.poc_price 读取）
  // [筹码共识价] - 描述: POC 中心线标签显示"核心共识价"（基于历史成交量分布的估算代理）
  if (layers.poc && profile && profile.pocPrice != null) {
    const pocVal = profile.pocPrice
    // [PROMPT.md §5.3.4 V2] POC 线宽按 scale.strokes.pocLine（mobile_capture 3.5px），
    //   标签字号按 scale.fonts.pocLabel（mobile_capture 32px）
    drawLine(ctx, g.l, py(pocVal), layers.profile ? g.profileEnd : g.plotRight, py(pocVal), C.orange, scale.strokes.pocLine, [9, 4])
    drawText(ctx, `核心共识价 ${fmt(pocVal)}`, g.plotRight - 80, py(pocVal) - 5, C.orange, scale.fonts.pocLabel)
  }

  // 5. 突破压力区
  if (layers.breakout) {
    renderBreakout(ctx, g, display, py, scale)
  }

  // 7. 通用渲染器：渲染后端返回的策略指标图层（DSA VWAP 等）
  // [DSA 数据源校验] - 检测 K 线时间与 indicators.source_bar_times 一致性，
  //   不一致则跳过 DSA 渲染并在控制台输出诊断信息（避免指标与 K 线错位）
  //   [PR #31] - 仅在 1d 校验 mismatch（DSA 不在 15m/1h 渲染，校验无意义且会误报）
  let dsaSourceMismatch = false
  if (shouldCheckDsaMismatch(timeframe) && indicators?.source_bar_times && displayTimes.length > 0) {
    const klineKeys = new Set<string>()
    displayTimes.forEach(t => {
      const key = normalizeChartTime(t, timeframe)
      if (key != null) klineKeys.add(key)
    })
    const indicatorKeys = new Set<string>()
    indicators.source_bar_times.forEach(t => {
      const key = normalizeChartTime(t, timeframe)
      if (key != null) indicatorKeys.add(key)
    })
    let matched = 0
    klineKeys.forEach(k => { if (indicatorKeys.has(k)) matched++ })
    const ratio = klineKeys.size > 0 ? matched / klineKeys.size : 0
    if (ratio < 0.5) {
      dsaSourceMismatch = true
      // eslint-disable-next-line no-console
      console.warn(
        `[StrategyChart] DSA 数据源不一致，跳过渲染: K线时间匹配率 ${(ratio * 100).toFixed(1)}%` +
        ` (${matched}/${klineKeys.size}), timeframe=${timeframe}, source_bar_hash=${indicators.source_bar_hash ?? 'N/A'}`,
      )
    }
  }
  // [DSA 数据源校验] - 同步到 state 供组件 useEffect 读取并渲染页面 UI 提示横幅
  state.dsaSourceMismatch = dsaSourceMismatch

  // [PR #31] - debugIndicatorAlignment 诊断输出已移除（P1 清理）

  if (indicators && indicators.layers && indicators.data) {
    indicators.layers.forEach(layer => {
      // [DSA Overlay Policy] - DSA 渲染决策：开关 / source mismatch / 周期支持
      // [PR #33] - 移除 `timeframe !== '1d'` 硬编码 skip，全周期按 shouldRenderDsaLayer 决策
      if (layer.layer_id === 'dsa_vwap' && !shouldRenderDsaLayer(layer.layer_id, layers, dsaSourceMismatch, timeframe)) return
      // [BB Overlay Policy] - BB 渲染决策：开关 / 周期支持
      // [PR #33] - 移除 `timeframe === '1w' || '1mo'` 硬编码 skip，全周期按 shouldRenderBbLayer 决策
      if (layer.layer_id === 'bb' && !shouldRenderBbLayer(layer.layer_id, layers, timeframe)) return
      // [MACD 副图] - 受 macd 图层开关控制
      if (layer.layer_id === 'macd' && !layers.macd) return
      // [SQZMOM_LB 副图] - 受 sqzmom 图层开关控制
      if (layer.layer_id === 'sqzmom_lb' && !layers.sqzmom) return
      // [CHANGE-011 SMC] - SMC 图层受 smc 开关控制
      if (layer.layer_id === 'smc' && !layers.smc) return
      // [DSA 数据契约] - union 类型（DsaSelectorData | Record）按 Record 索引访问，dsa_polyline 渲染器内部再 cast 到 DsaSelectorData
      const layerData = indicators.data![layer.strategy_id] as Record<string, (number | string | null)[]>
      if (layerData) {
        // [CHANGE-20260719-003 §四] 复用纵轴候选阶段构建的 indexMap（time-index map 共享）
        const sharedIndexMap = layerIndexMaps.get(layer.strategy_id)
        renderIndicatorLayer(ctx, g, layer, layerData, display.length, step, py, displayTimes, timeframe, sharedIndexMap ?? [], scale)
      }
    })
  }

  // 8. K 线蜡烛图
  // [PROMPT.md §5.3.4 V2] K线 wick 线宽按 scale.strokes.candleWick（mobile_capture 2.5px）
  display.forEach((d, i) => {
    const x = g.l + (i + 0.5) * step
    const col = d.close >= d.open ? C.up : C.down
    drawLine(ctx, x, py(d.high), x, py(d.low), col, scale.strokes.candleWick)
    ctx.fillStyle = col
    const yy = Math.min(py(d.open), py(d.close))
    const hh = Math.max(1, Math.abs(py(d.open) - py(d.close)))
    ctx.fillRect(x - barW / 2, yy, barW, hh)
  })

  // 9. 事件标记
  state.eventHit = []
  mappedEvents.forEach(ev => {
    if (!isEventVisible(ev, layers)) return
    const d = display[ev.index]
    if (!d) return
    const x = g.l + (ev.index + 0.5) * step
    const y = isSelectionHit(ev.type) ? py(d.low) + 11 : py(d.high) - 9
    ctx.fillStyle = ev.color
    ctx.beginPath()
    if (isSelectionHit(ev.type)) {
      ctx.moveTo(x, y)
      ctx.lineTo(x - 5, y + 8)
      ctx.lineTo(x + 5, y + 8)
      ctx.closePath()
    } else {
      ctx.arc(x, y, 4, 0, Math.PI * 2)
    }
    ctx.fill()
    if (state.focusEventId === ev.id) {
      ctx.strokeStyle = '#fff'
      // [PROMPT.md §5.3.4 V2] 事件焦点环线宽按 scale.strokes.eventMarker
      ctx.lineWidth = scale.strokes.eventMarker * 2
      ctx.beginPath()
      ctx.arc(x, y, 8, 0, Math.PI * 2)
      ctx.stroke()
    }
    state.eventHit.push({ ...ev, x, y })
  })

  // 10. Volume 副图
  if (layers.volume) renderVolume(ctx, g, display, step, barW, scale)

  // 11. 时间轴刻度
  // [ChartRightPadding] - 时间轴标签跟随 bar 分布（使用 effectivePlotW），不延伸到留白区
  // [PROMPT.md §5.3.4 V2] 时间轴字号按 scale.fonts.axisLabel（mobile_capture 32px）
  const labels = timeTicks(display, 7, timeframe)
  labels.forEach((item, i) => {
    drawText(ctx, item.label, g.l + effectivePlotW * i / (labels.length - 1), h - 7, C.text, scale.fonts.axisLabel, i === 0 ? 'left' : i === labels.length - 1 ? 'right' : 'center')
  })

  // 12. 最新价虚线 + 右侧价格标签
  // [PROMPT.md §5.3.4 V2] 最新价虚线线宽/标签字号按 scale.strokes.grid / fonts.axisLabel
  const last = display[display.length - 1]
  drawLine(ctx, g.l, py(last.close), g.plotRight, py(last.close), last.close >= last.open ? C.up : C.down, scale.strokes.grid, [3, 3])
  ctx.fillStyle = last.close >= last.open ? C.up : C.down
  ctx.fillRect(w - g.axis + 2, py(last.close) - 8, 50, 16)
  drawText(ctx, fmt(last.close), w - g.axis + 27, py(last.close) + 3, '#fff', scale.fonts.axisLabel, 'center')

  // 更新运行时状态
  state.ctx = ctx
  state.w = w
  state.h = h
  state.g = g
  state.data = display
  state.calc = calc
  state.min = min
  state.max = max
  state.py = py
  state.step = step
  state.profile = profile
  state.events = mappedEvents
}

// 计算窗格底部最低值（用于十字线垂直线）
function hBottom(g: Geometry): number {
  const panes = Object.values(g.panes)
  return Math.max(...panes.map(p => p.bottom))
}

// ===== React 组件 =====
const TIMEFRAMES = [
  { id: '15m', label: '15m' },
  { id: '1h', label: '1h' },
  { id: '1d', label: '日' },
  { id: '1w', label: '周' },
  { id: '1mo', label: '月' },
] as const

// 默认图层可见性
function getDefaultLayers(strategyId?: string): LayerVisibility {
  const layers: LayerVisibility = {
    volume: true,
    dsa: false,
    macd: false,
    breakout: false,
    selection: false,
    node: false,
    poc: false,
    profile: false,
    bb: false,
    delta: false,
    events: false,
    sqzmom: false,
    // [CHANGE-011 SMC] - 默认关闭，用户通过 IndicatorToolbar 显式开启
    smc: false,
  }
  if (strategyId && STRATEGIES[strategyId]) {
    STRATEGIES[strategyId].defaultLayers.forEach(id => {
      if (id in layers) layers[id as keyof LayerVisibility] = true
    })
  }
  return layers
}

// [chartLayerVisibility] - 将用户侧 8 键 ChartLayerVisibility 映射为内部 13 键 LayerVisibility
// trend → dsa + selection, node → profile + node + poc, 其余一一对应
// delta/events 为派生层（非用户可控）：delta 固定 false，events 固定 true（有事件数据时绘制）
// [CHANGE-011 SMC] - smc 一一对应，默认 false
function chartLayerVisibilityToInternal(
  vis: ChartLayerVisibility,
  source: string,
): LayerVisibility {
  return {
    volume: vis.volume,
    dsa: vis.trend,
    selection: vis.trend,
    node: vis.node,
    poc: vis.node,
    profile: vis.node,
    bb: vis.boll,
    macd: vis.macd,
    sqzmom: vis.sqzmom,
    breakout: source === 'selection' ? vis.breakout : false,
    delta: false,
    events: true,
    // [CHANGE-011 SMC] - smc 开关由用户在 IndicatorToolbar 控制
    smc: vis.smc,
  }
}

export function StrategyChart({
  symbol,
  bars,
  events = [],
  indicators,
  strategyId = 'default',
  source = 'watchlist',
  displayName,
  height = 660,
  timeframe = '1d',
  onTimeframeChange,
  viewport: viewportProp,
  onViewportChange,
  defaultVisibleBars,
  isCaptureMode = false,
  indicatorView = null,
  renderDensity = 'desktop',
  layerVisibility,
  barsFrame,
  indicatorsFetching = false,
  onIndicatorsRetry,
}: StrategyChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)

  // [PROMPT.md §5.3.4 V2] Canvas 缩放密度：根据 renderDensity prop 选择 desktop / mobile_capture
  //   - desktop: 保持现有 8-11px 字号 / 1-1.5px 线宽（普通详情页）
  //   - mobile_capture: 32-36px 字号 / 2-3.5px 线宽（飞书移动舞台截图）
  //   scale 在 drawTrading 调用前写入 stateRef.current.scale，供所有 draw 子函数读取。
  const scale = useMemo(() => getRenderScale(renderDensity), [renderDensity])

  // 图表运行时状态（供交互命中检测使用，不触发 re-render）
  const stateRef = useRef<ChartState>({
    ctx: null,
    w: 0,
    h: 0,
    g: null,
    data: [],
    calc: [],
    min: 0,
    max: 0,
    py: null,
    step: 0,
    profile: null,
    events: [],
    hoverProfileIndex: null,
    selectedNodeId: null,
    focusEventId: null,
    profileHit: [],
    eventHit: [],
    dsaSourceMismatch: false,
    scale,
    frameMismatch: false,
  })

  // [chartLayerVisibility] - 受控图层可见性（单一真源 v2）
  // 父组件 StockResearchWorkspace 持有唯一 ChartLayerVisibility state 并传入；
  // 截图模式时不传（undefined），由此处派生 forced layers（不读写 localStorage）。
  const effectiveLayers: LayerVisibility = useMemo(() => {
    // [feishu-capture] - 描述: 截图模式强制开启 FEISHU_CAPTURE_LAYERS，忽略用户偏好
    //   advice.md v6 第 2 条：dsa/bb/profile/node/poc 必须开启
    //
    // [CHANGE-20260720-Phase4 §四] 当 indicatorView 提供时，优先使用
    //   INDICATOR_VIEW_LAYER_PRESETS（每张图只渲染一个指标视图），
    //   替代 FEISHU_CAPTURE_LAYERS（同时开 5 层，与新语义冲突）。
    //   未提供 indicatorView 时回退到 FEISHU_CAPTURE_LAYERS（向后兼容）。
    if (isCaptureMode) {
      if (indicatorView) {
        return chartLayerVisibilityToInternal(
          getIndicatorViewLayerPreset(indicatorView),
          source,
        )
      }
      const forced = getDefaultLayers(strategyId)
      FEISHU_CAPTURE_LAYERS.forEach(layerId => {
        forced[layerId as keyof LayerVisibility] = true
      })
      return forced
    }
    // 父组件传入用户偏好 → 映射为内部 12 键 LayerVisibility
    if (layerVisibility) {
      return chartLayerVisibilityToInternal(layerVisibility, source)
    }
    // fallback：无父组件偏好时使用策略默认值（不应出现在正常流程中）
    return getDefaultLayers(strategyId)
  }, [layerVisibility, isCaptureMode, indicatorView, strategyId, source])

  // [chartViewport] - 显示 bar 数量（缩放控制）：保留为内部状态作为 fallback，
  //   当父组件未传入 viewport 时使用；受控时由 viewportProp 驱动
  //   [2026-07-21 反馈] 飞书舞台可传 defaultVisibleBars=90 限制默认显示窗口
  const initialVisibleBars = defaultVisibleBars ?? MAX_VISIBLE_BARS
  const [displayBars, setDisplayBars] = useState(initialVisibleBars)

  // 十字线联动图例 bar 索引（-1 表示无十字线，显示最后一根）
  const [legendIdx, setLegendIdx] = useState(-1)

  // [DSA 数据源校验] - K 线时间与 indicators.source_bar_times 不一致时显示页面 UI 提示横幅
  //   drawTrading 写入 stateRef，重绘 useEffect 读取后同步到此 state 触发 JSX 重渲染
  const [dsaMismatch, setDsaMismatch] = useState(false)

  // 计算指标
  const calc = useMemo(() => {
    if (!bars.length) return []
    const win = CALCULATION_WINDOWS[timeframe]?.bars || 180
    return addIndicators(bars.slice(-win))
  }, [bars, timeframe])

  // [chartViewport] - 当前 viewport：优先使用父组件受控值，否则用 displayBars 构造末尾视区
  //   切换周期时父组件清空 viewportProp，自动回退到默认末尾视区（advice.md 第三节问题 3）
  const viewport: ChartViewport = useMemo(() => {
    if (viewportProp) return clampViewport(viewportProp, calc.length)
    const visibleCount = clamp(displayBars, MIN_VISIBLE_BARS, Math.min(MAX_VISIBLE_BARS, calc.length))
    return createDefaultViewport(calc.length, visibleCount)
  }, [viewportProp, calc.length, displayBars])

  // [chartViewport] - 当 viewportProp 失效（超出 calc 范围）时通知父组件 clamp 后的值
  useEffect(() => {
    if (!onViewportChange || !viewportProp || !calc.length) return
    const clamped = clampViewport(viewportProp, calc.length)
    if (clamped.fromIndex !== viewportProp.fromIndex || clamped.toIndex !== viewportProp.toIndex) {
      onViewportChange(clamped)
    }
  }, [viewportProp, calc.length, onViewportChange])

  // [chartViewport] - 新行情追加自动跟随：calc.length 增长且用户位于最右端时，
  //   自动平移到最新 bar 并保持原可见根数；用户已主动平移到历史区域时不强制拉回。
  //   - viewportProp 为 undefined 时由内部 fallback createDefaultViewport(calc.length) 自动显示末尾，无需处理
  //   - viewportProp.toIndex >= prevLen 表示用户原来位于最右端
  const prevCalcLengthRef = useRef(calc.length)
  useEffect(() => {
    const prevLen = prevCalcLengthRef.current
    prevCalcLengthRef.current = calc.length
    if (calc.length <= prevLen) return
    if (!viewportProp || !onViewportChange) return
    // 用户原来位于最右端 → 自动跟随新 bar，保持原可见根数
    if (viewportProp.toIndex >= prevLen) {
      const delta = calc.length - prevLen
      onViewportChange({
        fromIndex: viewportProp.fromIndex + delta,
        toIndex: calc.length,
      })
    }
  }, [calc.length]) // eslint-disable-line react-hooks/exhaustive-deps -- viewportProp/onViewportChange 从 ref 读取避免循环

  // 可见 bars：基于 viewport 切片 calc（统一 viewport，所有图层共用，advice.md 第三节问题 2）
  const display = useMemo(() => {
    if (!calc.length) return []
    return calc.slice(viewport.fromIndex, viewport.toIndex)
  }, [calc, viewport])

  // 映射事件到 bar 索引
  const mappedEvents = useMemo(() => {
    if (!events.length || !display.length) return []
    return mapEvents(events, display, timeframe)
  }, [events, display, timeframe])

  // 最新数据 ref（供 draw 函数读取）
  const dataRef = useRef({ calc, display, mappedEvents, layers: effectiveLayers, timeframe })
  dataRef.current = { calc, display, mappedEvents, layers: effectiveLayers, timeframe }

  // indicators ref（避免 draw 函数依赖 indicators 导致频繁重绘）
  const indicatorsRef = useRef<IndicatorResponse | undefined>(undefined)
  indicatorsRef.current = indicators

  // [ChartRenderFrame] - bars 帧与 indicators 帧比对（PROMPT.md §五.296-307）
  //   周期切换过程中 bars 已返回新周期，indicators 仍是旧周期；此时若用旧指标覆盖
  //   新 K线会导致指标位置/比例错误。mismatch 时把 indicators 传给 drawTrading 改为
  //   undefined，仅渲染 K线/网格/profile 基础图层，state.frameMismatch 驱动 JSX 提示。
  //   barsFrame 未传入时降级到"不检查"（保持向后兼容，不阻塞现有调用方）。
  const barsFrameRef = useRef<ChartRenderFrame | null | undefined>(undefined)
  barsFrameRef.current = barsFrame
  const [frameMismatch, setFrameMismatch] = useState(false)

  // 绘制函数（稳定引用，从 dataRef 读取最新数据）
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || !canvas.offsetParent) return
    const { calc: c, display: d, mappedEvents: ev, layers: ly, timeframe: tf } = dataRef.current
    if (!d.length) return
    // [PROMPT.md §5.3.4 V2] 同步 scale 到 stateRef，供 drawTrading 内所有 draw 子函数读取
    stateRef.current.scale = scale
    const ind = indicatorsRef.current
    const barsF = barsFrameRef.current
    // frame 检查：barsFrame 传入且 indicators 存在时才比对（任一缺失降级到不检查）
    // [PROMPT.md §二.1] 优先比对 display_frame.display_hash（与 bars API 共用 build_display_frame 生成），
    //   display_frame 缺失时降级到 source_bar_hash（向后兼容旧后端响应）。
    //   避免 1d 周期 bars.source_bar_hash（100 根展示窗口）与 indicators.source_bar_hash
    //   （250 根算法输入）严格比对导致永久 mismatch。
    let effectiveIndicators = ind
    let mismatch = false
    if (barsF != null && ind != null) {
      const indFrame = buildIndicatorsFrame({
        instrumentId: barsF.instrumentId,
        timeframe: tf,
        adj: barsF.adj,
        sourceBarHash: ind.source_bar_hash,
        sourceBarTimes: ind.source_bar_times,
        displayFrame: ind.display_frame ?? null,
      })
      if (!isFrameMatched(barsF, indFrame)) {
        // mismatch：不传 indicators 给 drawTrading，仅渲染 K线/网格/profile
        effectiveIndicators = undefined
        mismatch = true
      }
    }
    stateRef.current.frameMismatch = mismatch
    drawTrading(canvas, c, d, ev, ly, tf, stateRef.current, effectiveIndicators)
  }, [])

  // 数据/图层变化时重绘
  useEffect(() => {
    draw()
    // [DSA 数据源校验] - drawTrading 将 mismatch 写入 stateRef，此处同步到 React state 驱动横幅渲染
    //   setDsaMismatch 相同值时 React 自动 bailout，不会触发额外重渲染循环
    setDsaMismatch(stateRef.current.dsaSourceMismatch)
    // [ChartRenderFrame] - frame mismatch 同步到 React state 驱动"指标加载中"提示
    setFrameMismatch(stateRef.current.frameMismatch)
  }, [draw, calc, display, mappedEvents, effectiveLayers, viewport, indicators, barsFrame, scale])

  // 交互事件绑定（仅一次）
  useEffect(() => {
    const canvas = canvasRef.current
    const tip = tipRef.current
    if (!canvas) return
    // 默认 grab 光标，拖动时切换为 grabbing
    canvas.style.cursor = 'grab'

    const handleMouseMove = (e: MouseEvent) => {
      const s = stateRef.current
      if (!s.g || !s.py || !s.ctx) return
      const r = canvas.getBoundingClientRect()
      const mx = e.clientX - r.left
      const my = e.clientY - r.top
      const { g, data, step, w } = s

      // Profile 行命中检测
      if (g.profileW && mx >= g.profileStart && mx <= g.profileEnd && my >= g.panes.price.top && my <= g.panes.price.bottom) {
        const hit = s.profileHit.find(x => my >= x.y1 && my <= x.y2)
        s.hoverProfileIndex = hit?.i ?? null
        draw()
        if (hit && s.profile && tip) {
          tip.classList.add('show')
          tip.style.left = Math.max(g.profileStart - 245, mx - 245) + 'px'
          tip.style.top = Math.max(44, my - 56) + 'px'
          tip.innerHTML = profileTooltip(hit.row, s.profile)
        }
        return
      }

      s.hoverProfileIndex = null
      if (mx < g.l || mx > g.plotRight || my < g.panes.price.top || my > hBottom(g)) {
        if (tip) tip.classList.remove('show')
        setLegendIdx(-1)
        draw()
        return
      }

      const i = Math.max(0, Math.min(data.length - 1, Math.floor((mx - g.l) / step)))
      const d = data[i]
      draw()
      // 绘制十字线
      const s2 = stateRef.current
      if (!s2.ctx || !s2.py) return
      const x = g.l + (i + 0.5) * step
      drawLine(s2.ctx, x, g.panes.price.top, x, hBottom(g), 'rgba(210,219,235,.38)', 1, [3, 3])
      drawLine(s2.ctx, g.l, my, g.plotRight, my, 'rgba(210,219,235,.38)', 1, [3, 3])
      setLegendIdx(i)
      if (tip) {
        tip.classList.add('show')
        tip.style.left = Math.min(w - 235, mx + 14) + 'px'
        tip.style.top = Math.max(42, my - 58) + 'px'
        // 追加后端策略指标
        // [chartViewport] - 描述: hover 时所有图层统一按 normalizeChartTime 时间键对齐
        const tf = dataRef.current.timeframe
        let indicatorHtml = ''
        if (indicatorsRef.current?.layers && indicatorsRef.current?.data) {
          indicatorsRef.current.layers.forEach(layer => {
            const layerData = indicatorsRef.current!.data[layer.strategy_id] as Record<string, (number | string | null)[]>
            if (!layerData) return
            const fields = layer.hover_fields.length ? layer.hover_fields : layer.fields
            const timeIndex = Array.isArray(layerData.time)
              ? buildTimeIndex(layerData.time, tf)
              : null
            const parts: string[] = []
            fields.forEach(f => {
              const vals = layerData[f]
              if (!vals) return
              let idx: number | undefined
              const key = normalizeChartTime(d.time, tf)
              idx = key != null ? timeIndex?.get(key) : undefined
              if (idx == null || idx < 0 || idx >= vals.length) return
              const v = vals[idx]
              if (v != null) {
                const label = f.replace(/_/g, ' ').toUpperCase()
                parts.push(`${label} ${fmt(v)}`)
              }
            })
            if (parts.length) {
              indicatorHtml += `<span>${layer.layer_name}: ${parts.join(' · ')}</span>`
            }
          })
        }
        tip.innerHTML = `<b>${formatTime(d.time)}</b><span>开 ${fmt(d.open)}　高 ${fmt(d.high)}</span><span>低 ${fmt(d.low)}　收 ${fmt(d.close)}</span><span>\u6210\u4ea4量 ${formatVolume(d.volume)}</span><span>Delta ${formatVolume(d.delta)}</span>${indicatorHtml}`
      }
    }

    const handleClick = (e: MouseEvent) => {
      const s = stateRef.current
      if (!s.g) return
      // 拖动后抑制 click，避免误触发 profile/event 选中
      if (dragMovedRef.current) return
      const r = canvas.getBoundingClientRect()
      const mx = e.clientX - r.left
      const my = e.clientY - r.top
      // Profile 行点击：选中对应 peak 节点
      if (s.g.profileW && mx >= s.g.profileStart && mx <= s.g.profileEnd) {
        const hit = s.profileHit.find(x => my >= x.y1 && my <= x.y2)
        if (hit && s.profile) {
          // 用后端 price_mid 匹配 peak 节点区间
          const node = s.profile.nodes.find(n => hit.row.price_mid >= n.lo && hit.row.price_mid <= n.hi)
          s.selectedNodeId = node?.id || null
          draw()
        }
        return
      }
      // 事件点击
      const ev = s.eventHit.find(x => x.x !== undefined && x.y !== undefined && Math.hypot(mx - x.x, my - x.y) < 10)
      if (ev) {
        s.focusEventId = ev.id
        draw()
      }
    }

    const handleMouseLeave = () => {
      const s = stateRef.current
      s.hoverProfileIndex = null
      if (tip) tip.classList.remove('show')
      setLegendIdx(-1)
      draw()
    }

    // [Task 16] - 滚轮缩放：以鼠标所在 K 线为锚点，缩放前后锚点保持相同横向位置
    // Shift+滚轮：水平平移（向左查看更早数据，向右回到最近数据）
    const handleWheel = (e: WheelEvent) => {
      const s = stateRef.current
      if (!s.g || !calc.length) return
      e.preventDefault()
      const r = canvas.getBoundingClientRect()
      const mx = e.clientX - r.left
      const { g, step } = s
      // 鼠标 X → display index → calc index（锚点）
      const dispIdx = Math.max(0, Math.min(display.length - 1, Math.floor((mx - g.l) / step)))
      const anchorIndex = viewport.fromIndex + dispIdx

      if (e.shiftKey) {
        // Shift+滚轮：水平平移（deltaY > 0 向右/未来，< 0 向左/过去）
        const deltaBars = e.deltaY > 0 ? 5 : -5
        const newVp = panViewport(viewport, deltaBars, calc.length)
        if (onViewportChange) onViewportChange(newVp)
      } else {
        // 普通滚轮：缩放（deltaY < 0 放大，> 0 缩小）
        const zoom = e.deltaY < 0 ? 1.15 : 1 / 1.15
        const newVp = zoomAtAnchor(viewport, anchorIndex, zoom, calc.length)
        if (onViewportChange) onViewportChange(newVp)
      }
    }

    // [Task 16] - 拖动平移：指针拖动画布，向左查看更早数据，向右回到最近数据
    // 使用 Pointer Events + 捕获 + 锚定起始视区，保证拖动稳定不漂移
    const handlePointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return // 仅左键
      const s = stateRef.current
      if (!s.g) return
      const r = canvas.getBoundingClientRect()
      dragRef.current = {
        startClientX: e.clientX - r.left,
        startViewport: viewport,
        pointerId: e.pointerId,
      }
      dragMovedRef.current = false
      try { canvas.setPointerCapture(e.pointerId) } catch { /* ignore */ }
      canvas.style.cursor = 'grabbing'
    }

    const handlePointerMove = (e: PointerEvent) => {
      if (!dragRef.current) return
      const s = stateRef.current
      if (!s.g) return
      const r = canvas.getBoundingClientRect()
      const { step } = s
      // 始终从起始 clientX 计算总位移，从起始视区平移（不累积，避免漂移）
      const deltaPx = (e.clientX - r.left) - dragRef.current.startClientX
      // 拖动距离 → bar 数（向左拖动 = 查看更早数据 = fromIndex 减小）
      const deltaBars = -Math.round(deltaPx / step)
      // 点击阈值：移动超过 4px 视为拖动
      if (Math.abs(deltaPx) > 4) dragMovedRef.current = true
      const newVp = panViewport(dragRef.current.startViewport, deltaBars, calc.length)
      if (onViewportChange) onViewportChange(newVp)
    }

    const handlePointerUp = (e: PointerEvent) => {
      if (dragRef.current) {
        try { canvas.releasePointerCapture(e.pointerId) } catch { /* ignore */ }
        dragRef.current = null
        canvas.style.cursor = 'grab'
      }
    }

    const handlePointerCancel = (e: PointerEvent) => {
      if (dragRef.current) {
        try { canvas.releasePointerCapture(e.pointerId) } catch { /* ignore */ }
        dragRef.current = null
        canvas.style.cursor = 'grab'
      }
    }

    // [Task 16] - 双击恢复自动范围（回到最新数据末尾视区）
    //   [2026-07-21 反馈] 飞书舞台传 defaultVisibleBars=90 时，双击恢复也用 90
    const handleDoubleClick = () => {
      if (onViewportChange) {
        onViewportChange(createDefaultViewport(calc.length, initialVisibleBars))
      }
    }

    // [Task 16] - 移动端双指缩放
    const handleTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 2) {
        const t0 = e.touches[0]
        const t1 = e.touches[1]
        const dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY)
        pinchRef.current = { startDist: dist, startViewport: viewport }
      }
    }

    const handleTouchMove = (e: TouchEvent) => {
      if (e.touches.length === 2 && pinchRef.current?.startViewport) {
        e.preventDefault()
        const t0 = e.touches[0]
        const t1 = e.touches[1]
        const dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY)
        const ratio = dist / pinchRef.current.startDist
        if (ratio > 1.1 || ratio < 0.9) {
          const zoom = ratio > 1 ? 1.1 : 1 / 1.1
          const anchor = Math.floor((viewport.fromIndex + viewport.toIndex) / 2)
          const newVp = zoomAtAnchor(pinchRef.current.startViewport, anchor, zoom, calc.length)
          if (onViewportChange) onViewportChange(newVp)
          pinchRef.current.startDist = dist
          pinchRef.current.startViewport = newVp
        }
      }
    }

    const handleTouchEnd = () => {
      pinchRef.current = null
    }

    canvas.addEventListener('mousemove', handleMouseMove)
    canvas.addEventListener('click', handleClick)
    canvas.addEventListener('mouseleave', handleMouseLeave)
    canvas.addEventListener('wheel', handleWheel, { passive: false })
    canvas.addEventListener('pointerdown', handlePointerDown)
    canvas.addEventListener('pointermove', handlePointerMove)
    canvas.addEventListener('pointerup', handlePointerUp)
    canvas.addEventListener('pointercancel', handlePointerCancel)
    canvas.addEventListener('dblclick', handleDoubleClick)
    canvas.addEventListener('touchstart', handleTouchStart, { passive: false })
    canvas.addEventListener('touchmove', handleTouchMove, { passive: false })
    canvas.addEventListener('touchend', handleTouchEnd)
    return () => {
      canvas.removeEventListener('mousemove', handleMouseMove)
      canvas.removeEventListener('click', handleClick)
      canvas.removeEventListener('mouseleave', handleMouseLeave)
      canvas.removeEventListener('wheel', handleWheel)
      canvas.removeEventListener('pointerdown', handlePointerDown)
      canvas.removeEventListener('pointermove', handlePointerMove)
      canvas.removeEventListener('pointerup', handlePointerUp)
      canvas.removeEventListener('pointercancel', handlePointerCancel)
      canvas.removeEventListener('dblclick', handleDoubleClick)
      canvas.removeEventListener('touchstart', handleTouchStart)
      canvas.removeEventListener('touchmove', handleTouchMove)
      canvas.removeEventListener('touchend', handleTouchEnd)
    }
  }, [draw, calc.length, viewport, onViewportChange, display.length])

  // CHANGE-20260716-006: ResizeObserver 改为下一帧立即 draw + trailing draw
  // 旧 120ms 纯防抖在快速切周期/全屏时中间宽度绘制导致右边界错位
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    let rafId: number | null = null
    let trailingTimer: ReturnType<typeof setTimeout> | null = null
    const ro = new ResizeObserver(() => {
      // 下一帧立即 draw（响应布局变化）
      if (rafId !== null) cancelAnimationFrame(rafId)
      rafId = requestAnimationFrame(() => {
        draw()
        rafId = null
      })
      // 布局稳定后补一次 trailing draw（120ms）
      if (trailingTimer !== null) clearTimeout(trailingTimer)
      trailingTimer = setTimeout(() => draw(), 120)
    })
    ro.observe(wrap)
    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId)
      if (trailingTimer !== null) clearTimeout(trailingTimer)
      ro.disconnect()
    }
  }, [draw])

  // 图例数据
  const legendData = useMemo(() => {
    const idx = legendIdx < 0 ? display.length - 1 : legendIdx
    const d = display[idx]
    if (!d) return null
    const prev = display[idx - 1] || d
    const change = (d.close - prev.close) / prev.close * 100
    return { d, change, idx }
  }, [legendIdx, display])

  // [chartViewport] - 拖动平移状态（Task 16: TradingView 风格缩放）
  // 使用 Pointer Events + 锚定起始视区，避免累积漂移
  const dragRef = useRef<{ startClientX: number; startViewport: ChartViewport; pointerId: number } | null>(null)
  // [chartViewport] - 拖动点击阈值：拖动超过 4px 后抑制后续 click
  const dragMovedRef = useRef(false)
  // [chartViewport] - 移动端双指缩放状态
  const pinchRef = useRef<{ startDist: number; startViewport: ChartViewport | null } | null>(null)

  // [chartViewport] - 缩放控制：受控时通知父组件，否则更新内部 displayBars
  //   锚点取当前视区中央，保留 +/-/复位 按钮交互（Task 16 新增滚轮/拖动/双击）
  const zoomIn = () => {
    const anchor = Math.floor((viewport.fromIndex + viewport.toIndex) / 2)
    if (onViewportChange) {
      onViewportChange(zoomAtAnchor(viewport, anchor, 1.2, calc.length))
    } else {
      setDisplayBars(n => Math.max(MIN_VISIBLE_BARS, n - 15))
    }
  }
  const zoomOut = () => {
    const anchor = Math.floor((viewport.fromIndex + viewport.toIndex) / 2)
    if (onViewportChange) {
      onViewportChange(zoomAtAnchor(viewport, anchor, 1 / 1.2, calc.length))
    } else {
      setDisplayBars(n => Math.min(MAX_VISIBLE_BARS, n + 15))
    }
  }
  const resetZoom = () => {
    if (onViewportChange) {
      onViewportChange(createDefaultViewport(calc.length, initialVisibleBars))
    } else {
      setDisplayBars(initialVisibleBars)
    }
  }

  const hasData = bars.length > 0

  // [ChartRenderFrame 3 态] - PROMPT.md §二.1：loading 只在请求 pending 时显示；
  //   请求结束后不匹配必须显示明确错误码 + 重试按钮，禁止无限 loading。
  //   - pending: indicators 未到（首次加载）或 indicatorsFetching=true（refetch 中，旧数据临时不匹配）
  //   - success: indicators 已到且 frame 匹配
  //   - mismatch-error: indicators 已到且 frame 不匹配（请求已结束，数据不对）
  //   bars 未就绪（hasData=false）时不显示任何状态（避免与"暂无行情数据"重复）
  const indicatorsLoadState: 'pending' | 'success' | 'mismatch-error' =
    !hasData
      ? 'success'
      : indicators == null || indicatorsFetching
        ? 'pending'
        : frameMismatch
          ? 'mismatch-error'
          : 'success'

  return (
    <div className="strategy-chart-wrap">
      {/* 工具栏 */}
      <div className="tv-chart-toolbar">
        <b className="tv-symbol-code">{displayName ? `${displayName}（${symbol}）` : symbol}</b>
        {legendData && (
          <div className="chart-ohlc">
            <span>{formatTime(legendData.d.time)}</span>
            <span>开 {fmt(legendData.d.open)}</span>
            <span>高 {fmt(legendData.d.high)}</span>
            <span>低 {fmt(legendData.d.low)}</span>
            <span>收 {fmt(legendData.d.close)}</span>
            <span className={legendData.change >= 0 ? 'market-up' : 'market-down'}>
              {legendData.change >= 0 ? '+' : ''}{legendData.change.toFixed(2)}%
            </span>
            <span>量 {formatVolume(legendData.d.volume)}</span>
          </div>
        )}
        <div className="tv-toolbar-spacer" />
        {/* 周期切换 */}
        <div className="tv-tf-group">
          {TIMEFRAMES.map(tf => (
            <button
              key={tf.id}
              className={clsx('tv-tf-btn', timeframe === tf.id && 'active')}
              onClick={() => onTimeframeChange?.(tf.id)}
            >
              {tf.label}
            </button>
          ))}
        </div>
        {/* 缩放/复位 */}
        <button className="btn small" onClick={zoomOut} title="缩小">−</button>
        <button className="btn small" onClick={resetZoom} title="复位">复位</button>
        <button className="btn small" onClick={zoomIn} title="放大">+</button>
        {/* [Task 16] 范围按钮：日线 1月/3月/6月/1年/全部；15m/1h 1日/5日/20日/60日 */}
        <div className="tv-range-group">
          {(timeframe === '1d' || timeframe === '1w' || timeframe === '1mo'
            ? [{ label: '1月', bars: 22 }, { label: '3月', bars: 66 }, { label: '6月', bars: 132 }, { label: '1年', bars: 250 }, { label: '全部', bars: MAX_VISIBLE_BARS }]
            : [{ label: '1日', bars: 24 }, { label: '5日', bars: 120 }, { label: '20日', bars: 240 }, { label: '60日', bars: 240 }]
          ).map(opt => {
            const visibleCount = viewport.toIndex - viewport.fromIndex
            const targetVisible = Math.min(opt.bars, calc.length, MAX_VISIBLE_BARS)
            const isActive = viewport.toIndex === calc.length && visibleCount === targetVisible
            return (
              <button
                key={opt.label}
                className={clsx('tv-range-btn', isActive && 'active')}
                onClick={() => {
                  const visible = Math.min(opt.bars, calc.length, MAX_VISIBLE_BARS)
                  if (onViewportChange) {
                    onViewportChange(createDefaultViewport(calc.length, Math.max(MIN_VISIBLE_BARS, visible)))
                  }
                }}
              >
                {opt.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* 图表画布 */}
      <div className="tv-canvas-wrap" ref={wrapRef} style={{ '--tv-chart-height': `${height}px` } as React.CSSProperties}>
        <canvas ref={canvasRef} />
        <div className="chart-crosshair-tooltip" ref={tipRef} />
        {/* [DSA 数据源校验] - K 线时间与指标 source_bar_times 不一致时显示页面提示横幅（替代仅 console.warn）。
            当 indicatorsLoadState=mismatch-error 时优先显示 mismatch-error 横幅，DSA 横幅隐藏避免堆叠。 */}
        {dsaMismatch && indicatorsLoadState === 'success' && (
          <div className="dsa-source-mismatch-banner" style={{ position: 'absolute', top: 10, left: '50%', transform: 'translateX(-50%)', padding: '4px 12px', background: 'rgba(255,193,7,0.9)', color: '#333', fontSize: 12, borderRadius: 4, zIndex: 10, whiteSpace: 'nowrap' }}>
            DSA 数据源不一致，已暂停渲染
          </div>
        )}
        {/* [ChartRenderFrame 3 态] - PROMPT.md §二.1：loading 只在请求 pending 时显示；
            请求结束后不匹配必须显示明确错误码 + 重试按钮，禁止无限 loading。
            - pending: 请求在飞（首次加载 / 周期切换 refetch 中）→ 显示"指标加载中"
            - mismatch-error: 请求已结束但 display_frame 不匹配 → 显示错误码 + 重试按钮
            success 不显示横幅。 */}
        {indicatorsLoadState === 'pending' && (
          <div className="chart-frame-mismatch-banner chart-frame-pending" style={{ position: 'absolute', top: 10, left: '50%', transform: 'translateX(-50%)', padding: '4px 12px', background: 'rgba(13,17,24,0.85)', color: C.text, fontSize: 12, borderRadius: 4, zIndex: 10, whiteSpace: 'nowrap' }}>
            指标加载中...
          </div>
        )}
        {indicatorsLoadState === 'mismatch-error' && (
          <div
            className="chart-frame-mismatch-banner chart-frame-error"
            data-testid="chart-frame-mismatch-banner"
            style={{
              position: 'absolute',
              top: 10,
              left: '50%',
              transform: 'translateX(-50%)',
              padding: '6px 12px',
              background: 'rgba(255,77,79,0.92)',
              color: '#fff',
              fontSize: 12,
              borderRadius: 4,
              zIndex: 10,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              maxWidth: '90%',
              flexWrap: 'wrap',
            }}
          >
            <span>指标加载失败：display_frame 不匹配</span>
            {/* [PROMPT.md §二 V2] 显示两端 count/time/hash/as_of 差异，便于运维定位 */}
            {(() => {
              const barsDf = barsFrame?.displayFrame
              const indDf = indicators?.display_frame ?? null
              const diffs: string[] = []
              if (barsDf && indDf) {
                if (barsDf.actual_count != null || indDf.actual_count != null) {
                  diffs.push(`count: ${barsDf.actual_count ?? 'N/A'} / ${indDf.actual_count ?? 'N/A'}`)
                }
                if (barsDf.first_time || indDf.first_time) {
                  diffs.push(`first: ${barsDf.first_time ?? 'N/A'} / ${indDf.first_time ?? 'N/A'}`)
                }
                if (barsDf.last_time || indDf.last_time) {
                  diffs.push(`last: ${barsDf.last_time ?? 'N/A'} / ${indDf.last_time ?? 'N/A'}`)
                }
                if (barsDf.display_hash || indDf.display_hash) {
                  diffs.push(`hash: ${barsDf.display_hash || 'N/A'} / ${indDf.display_hash || 'N/A'}`)
                }
                if (barsDf.adjustment_as_of || indDf.adjustment_as_of) {
                  diffs.push(`as_of: ${barsDf.adjustment_as_of ?? 'N/A'} / ${indDf.adjustment_as_of ?? 'N/A'}`)
                }
              }
              return diffs.length > 0 ? (
                <span style={{ opacity: 0.9, fontSize: 11 }}>
                  （{diffs.join(' | ')}）
                </span>
              ) : null
            })()}
            {onIndicatorsRetry && (
              <button
                type="button"
                onClick={onIndicatorsRetry}
                style={{ padding: '2px 8px', background: 'rgba(255,255,255,0.18)', color: '#fff', border: '1px solid rgba(255,255,255,0.35)', borderRadius: 3, fontSize: 11, cursor: 'pointer' }}
              >
                重试
              </button>
            )}
          </div>
        )}
        {/* [CHANGE-20260717-001 Pine parity] SMC 状态提示
            SMC 基于"最新已完成K线"计算（Pine 语义），盘中实时合成K线不参与 parity 计算。
            仅在 SMC 图层开启时显示，避免实时合成K线被误解为实时 SMC parity。 */}
        {effectiveLayers.smc && (
          <div className="smc-completed-bar-hint" style={{ position: 'absolute', bottom: 6, right: 60, padding: '2px 8px', background: 'rgba(13,17,24,0.7)', color: C.text, fontSize: 11, borderRadius: 3, zIndex: 8, whiteSpace: 'nowrap', pointerEvents: 'none' }}>
            SMC 基于最新已完成K线
          </div>
        )}
        {!hasData && <div className="tv-chart-empty">暂无行情数据</div>}
      </div>
    </div>
  )
}

export default StrategyChart
