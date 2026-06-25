// StrategyChart：纯 Canvas 2D 策略图表组件（V1.6.3）
// 对应原型 assets/charts.js 的 drawTrading 渲染管线
// 支持 K 线 + 成交量 + VWAP + Volume Profile + Node Cluster + Volume Delta + 事件标记
// 图层可见性持久化到 localStorage，十字线联动 OHLC 图例
// 用法：<StrategyChart symbol="688112" bars={bars} events={events} strategyId="node" source="watchlist" height={660} />

import { useEffect, useRef, useState, useMemo, useCallback } from 'react'
import clsx from 'clsx'
import {
  CALCULATION_WINDOWS,
  DISPLAY_GROUPS,
  STRATEGIES,
  type DisplayGroupDef,
} from '../lib/strategy-manifest'
import type { ChartLayer, IndicatorResponse } from '../api/endpoints'

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
  breakout: boolean
  selection: boolean
  node: boolean
  poc: boolean
  profile: boolean
  bb: boolean
  delta: boolean
  events: boolean
}

export interface StrategyChartProps {
  symbol: string
  bars: BarData[]
  events?: ChartEvent[]
  indicators?: IndicatorResponse | undefined
  strategyId?: string
  source?: string
  height?: number
  timeframe?: string
  onTimeframeChange?: (tf: string) => void
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
}

// ===== 通用工具函数 =====
const clamp = (v: number, a: number, b: number): number => Math.max(a, Math.min(b, v))
const fmt = (v: number, d = 2): string => Number(v).toFixed(d)

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

// 时间轴刻度（对齐原型 timeTicks）
function timeTicks(data: CalculatedBar[], count: number, tf: string): { idx: number; label: string }[] {
  const out: { idx: number; label: string }[] = []
  const mdFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
  })
  const timeFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const ymFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
  })

  for (let i = 0; i < count; i++) {
    const idx = Math.round((data.length - 1) * i / (count - 1))
    const d = new Date(data[idx].time)
    let label: string
    if (tf === '15m' || tf === '1h') {
      label = `${mdFmt.format(d).replace(/\//g, '-')} ${timeFmt.format(d)}`
    } else if (tf === '1d') {
      label = mdFmt.format(d).replace(/\//g, '-')
    } else {
      label = ymFmt.format(d)
    }
    out.push({ idx, label })
  }
  return out
}

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

// [Volume Profile] - 从后端 indicators 提取 VP 数据（SSOT: volume_node_monitor.compute_indicators）
// profile_rows/profile_meta/peak_rows 为价格档位快照，非 bar 对齐时间序列，禁止前端重算
function extractBackendProfile(indicators: IndicatorResponse | undefined): BackendProfile | null {
  if (!indicators?.data) return null
  const vn = indicators.data['volume_node_monitor'] as unknown as
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

  // 从 upper_node/lower_node 收集 peak 节点价格区间 + peak_rows 多空量
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
  let pocPrice: number | null = null
  if (meta.poc_price != null) {
    pocPrice = meta.poc_price
  } else if (Array.isArray(vn.poc_price)) {
    for (const p of vn.poc_price) {
      if (p != null) { pocPrice = Number(p); break }
    }
  }
  const nodes: BackendNode[] = Array.from(peakMap.entries())
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

  return { rows, meta, peaks, nodes, pocPrice }
}

// ===== 布局几何模块 =====
// 根据启用的图层动态分配窗格高度
function geometry(layers: Set<string>, w: number, h: number): Geometry {
  const profileOn = layers.has('profile')
  const volumeOn = layers.has('volume')
  const l = 58
  const axis = 57
  const profileW = profileOn ? 148 : 0
  const gap = profileOn ? 8 : 0
  const plotRight = w - axis - profileW - gap
  const bottom = 25
  const paneGap = 7
  let cursor = h - bottom
  const panes: Record<string, PaneRect> = {}
  if (volumeOn) {
    panes.volume = { bottom: cursor, top: cursor - 76 }
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
  font = '10px ui-monospace, SFMono-Regular, Menlo, monospace',
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
): void {
  const p = g.panes[pane]
  if (!p) return
  drawLine(ctx, g.l, (p.top + p.bottom) / 2, g.plotRight, (p.top + p.bottom) / 2, C.grid2)
  drawText(ctx, label, g.l + 5, p.top + 12, color, '9px sans-serif')
  drawText(ctx, fmt(max, 2), g.plotRight + 5, p.top + 9, C.text, '8px monospace')
  drawText(ctx, fmt(min, 2), g.plotRight + 5, p.bottom - 2, C.text, '8px monospace')
  if (current !== undefined) {
    const y = p.top + (max - current) / Math.max(0.0001, max - min) * (p.bottom - p.top)
    ctx.fillStyle = color
    ctx.fillRect(g.plotRight + 1, y - 7, 54, 14)
    drawText(ctx, fmt(current, 2), g.plotRight + 28, y + 3, '#fff', '8px monospace', 'center')
  }
}

// ===== 渲染函数 =====

// 背景 + 副图底色 + 价格网格 + 垂直网格 + 右侧价格刻度
function drawGrid(
  ctx: CanvasRenderingContext2D,
  w: number, h: number,
  g: Geometry,
  min: number, max: number,
): void {
  ctx.fillStyle = C.bg
  ctx.fillRect(0, 0, w, h)
  Object.entries(g.panes).forEach(([name, p]) => {
    if (name !== 'price') {
      ctx.fillStyle = C.panel
      ctx.fillRect(g.l, p.top, g.plotRight - g.l, p.bottom - p.top)
      drawLine(ctx, g.l, p.top, g.plotRight, p.top, C.grid)
    }
  })
  if (g.profileW) {
    ctx.fillStyle = '#0b0f16'
    ctx.fillRect(g.profileStart, g.panes.price.top, g.profileEnd - g.profileStart, g.panes.price.bottom - g.panes.price.top)
    drawLine(ctx, g.profileStart, g.panes.price.top, g.profileStart, g.panes.price.bottom, C.grid)
  }
  for (let i = 0; i < 7; i++) {
    const y = g.panes.price.top + (g.panes.price.bottom - g.panes.price.top) * i / 6
    drawLine(ctx, g.l, y, g.plotRight, y, C.grid)
    drawText(ctx, fmt(max - (max - min) * i / 6), w - g.axis + 7, y + 3)
  }
  for (let i = 0; i < 9; i++) {
    const x = g.l + (g.plotRight - g.l) * i / 8
    drawLine(ctx, x, g.panes.price.top, x, h - g.bottom, C.grid2)
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

  // 价值区填充 + VAH/VAL 虚线（从后端 profile_meta 读取）
  if (profile.meta.vah_price != null && profile.meta.val_price != null) {
    const valueTop = py(profile.meta.vah_price)
    const valueBottom = py(profile.meta.val_price)
    ctx.fillStyle = 'rgba(95,127,216,.055)'
    ctx.fillRect(g.l, valueTop, g.profileEnd - g.l, Math.max(1, valueBottom - valueTop))
    drawLine(ctx, g.l, valueTop, g.profileEnd, valueTop, 'rgba(130,160,255,.58)', 1, [3, 3])
    drawLine(ctx, g.l, valueBottom, g.profileEnd, valueBottom, 'rgba(130,160,255,.58)', 1, [3, 3])
    drawText(ctx, 'VAH', g.plotRight - 4, valueTop - 4, C.blue2, '8px sans-serif', 'right')
    drawText(ctx, 'VAL', g.plotRight - 4, valueBottom - 4, C.blue2, '8px sans-serif', 'right')
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
      ctx.lineWidth = 1.3
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

  drawText(ctx, '卖量', g.profileStart + 3, g.panes.price.top + 12, C.profileSell, '8px sans-serif')
  drawText(ctx, '买量', g.profileStart + 32, g.panes.price.top + 12, C.profileBuy, '8px sans-serif')

  // Node Cluster 节点矩形框（从后端 upper_node/lower_node 提取的节点区间）
  if (layers.has('node') && profile.nodes.length > 0) {
    profile.nodes.forEach(n => {
      const y1 = py(n.hi)
      const y2 = py(n.lo)
      const selected = state.selectedNodeId === n.id
      ctx.strokeStyle = n.poc ? C.orange : selected ? '#dce6ff' : 'rgba(79,124,255,.72)'
      ctx.lineWidth = selected ? 2 : n.poc ? 1.4 : 1
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
  drawPaneTicks(ctx, g, 'volume', 0, vmax, 'VOL', data[data.length - 1].volume, C.text)
}

// 突破压力区
function renderBreakout(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  data: CalculatedBar[],
  py: (v: number) => number,
): void {
  const pressure = Math.max(...data.slice(-55, -18).map(d => d.high))
  drawLine(ctx, g.l, py(pressure), g.plotRight, py(pressure), C.down, 1, [7, 4])
  drawText(ctx, '结构压力', g.plotRight - 54, py(pressure) - 5, C.down, '8px sans-serif')
}

// ===== 通用渲染器（根据后端返回的 ChartLayer.renderer 分发）=====

// 通用渲染器：根据 layer.renderer 分发
function renderIndicatorLayer(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | null)[]>,
  barsCount: number,
  step: number,
  py: (v: number) => number,
): void {
  switch (layer.renderer) {
    case 'line':
      renderIndicatorLine(ctx, g, layer, data, barsCount, step, py)
      break
    case 'price_zone':
      renderIndicatorPriceZone(ctx, g, layer, data, barsCount, step, py)
      break
    case 'band':
      renderIndicatorBand(ctx, g, layer, data, barsCount, step, py)
      break
  }
}

// 线图渲染（支持 direction_colored）
function renderIndicatorLine(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | null)[]>,
  barsCount: number,
  step: number,
  py: (v: number) => number,
): void {
  // layer.fields[0] 是主值字段（如 dsa_vwap）
  // layer.fields[1] 是方向字段（如 dsa_dir，1=上涨，0=下跌）
  const valueField = layer.fields[0]
  const dirField = layer.fields[1]
  const values = data[valueField]
  if (!values || !values.length) return
  // 对齐可见 bar 数量，避免指标数组长度超过 display 时越界绘制
  const len = Math.min(values.length, barsCount)

  if (layer.direction_colored && dirField && data[dirField]) {
    const dirs = data[dirField]
    // 分段绘制：相邻方向相同的点连成一段
    let segStart = 0
    for (let i = 1; i <= len; i++) {
      const curDir = i < len ? dirs[i] : null
      const prevDir = dirs[i - 1]
      const dirChanged = i === len || curDir !== prevDir
      if (dirChanged && i > segStart + 1) {
        // 绘制 segStart 到 i-1 的线段
        const dir = prevDir
        const color = dir === 1 ? (layer.direction_up_color || '#ff1744') : (layer.direction_down_color || '#00e676')
        ctx.beginPath()
        let started = false
        for (let j = segStart; j < i; j++) {
          const v = values[j]
          if (v == null) { started = false; continue }
          const x = g.l + (j + 0.5) * step
          const y = py(v)
          if (!started) { ctx.moveTo(x, y); started = true }
          else ctx.lineTo(x, y)
        }
        ctx.strokeStyle = color
        ctx.lineWidth = 1.5
        ctx.stroke()
        segStart = i - 1
      }
    }
  } else {
    // 单色线
    ctx.beginPath()
    let started = false
    for (let i = 0; i < len; i++) {
      const v = values[i]
      if (v == null) { started = false; continue }
      const x = g.l + (i + 0.5) * step
      const y = py(v)
      if (!started) { ctx.moveTo(x, y); started = true }
      else ctx.lineTo(x, y)
    }
    ctx.strokeStyle = layer.color || C.yellow
    ctx.lineWidth = 1.5
    ctx.stroke()
  }
}

// 价格区间渲染（半透明矩形）
function renderIndicatorPriceZone(
  ctx: CanvasRenderingContext2D,
  g: Geometry,
  layer: ChartLayer,
  data: Record<string, (number | null)[]>,
  barsCount: number,
  step: number,
  py: (v: number) => number,
): void {
  // layer.fields: [upper_node, lower_node, poc_price]
  const upperField = layer.fields[0]
  const lowerField = layer.fields[1]
  const upperVals = data[upperField]
  const lowerVals = data[lowerField]
  if (!upperVals || !lowerVals) return
  // 对齐可见 bar 数量，避免指标数组长度超过 display 时越界绘制
  const len = Math.min(upperVals.length, lowerVals.length, barsCount)

  ctx.fillStyle = layer.color || 'rgba(33,150,243,0.50)'
  for (let i = 0; i < len; i++) {
    const upper = upperVals[i]
    const lower = lowerVals[i]
    if (upper == null || lower == null) continue
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
  data: Record<string, (number | null)[]>,
  barsCount: number,
  step: number,
  py: (v: number) => number,
): void {
  const upperField = layer.fields[0]
  const lowerField = layer.fields[1]
  const middleField = layer.fields[2]
  const upperVals = data[upperField]
  const lowerVals = data[lowerField]
  const middleVals = middleField ? data[middleField] : null
  if (!upperVals || !lowerVals) return
  const len = Math.min(upperVals.length, lowerVals.length, barsCount)
  // A 股 BB 配色：填充浅蓝半透明、上轨/下轨蓝色、中轨橙黄
  const bandColor = C.bbFill
  const upperLowerColor = C.bbUpperLower
  const middleColor = C.bbMiddle

  // 1. 半透明填充带
  ctx.beginPath()
  let started = false
  for (let i = 0; i < len; i++) {
    const u = upperVals[i]
    const l = lowerVals[i]
    if (u == null || l == null) { started = false; continue }
    const x = g.l + (i + 0.5) * step
    if (!started) { ctx.moveTo(x, py(u)); started = true }
    else ctx.lineTo(x, py(u))
  }
  for (let i = len - 1; i >= 0; i--) {
    const l = lowerVals[i]
    if (l == null) continue
    const x = g.l + (i + 0.5) * step
    ctx.lineTo(x, py(l))
  }
  ctx.closePath()
  ctx.fillStyle = bandColor
  ctx.fill()

  // 2. 上轨线（浅蓝）
  ctx.beginPath()
  started = false
  for (let i = 0; i < len; i++) {
    const v = upperVals[i]
    if (v == null) { started = false; continue }
    const x = g.l + (i + 0.5) * step
    if (!started) { ctx.moveTo(x, py(v)); started = true }
    else ctx.lineTo(x, py(v))
  }
  ctx.strokeStyle = upperLowerColor
  ctx.lineWidth = 1
  ctx.setLineDash([5, 3])
  ctx.stroke()
  ctx.setLineDash([])

  // 3. 下轨线（浅蓝）
  ctx.beginPath()
  started = false
  for (let i = 0; i < len; i++) {
    const v = lowerVals[i]
    if (v == null) { started = false; continue }
    const x = g.l + (i + 0.5) * step
    if (!started) { ctx.moveTo(x, py(v)); started = true }
    else ctx.lineTo(x, py(v))
  }
  ctx.strokeStyle = upperLowerColor
  ctx.lineWidth = 1
  ctx.setLineDash([5, 3])
  ctx.stroke()
  ctx.setLineDash([])

  // 4. 中轨线（橙黄实线）
  if (middleVals) {
    ctx.beginPath()
    started = false
    for (let i = 0; i < len; i++) {
      const v = middleVals[i]
      if (v == null) { started = false; continue }
      const x = g.l + (i + 0.5) * step
      if (!started) { ctx.moveTo(x, py(v)); started = true }
      else ctx.lineTo(x, py(v))
    }
    ctx.strokeStyle = middleColor
    ctx.lineWidth = 1.5
    ctx.stroke()
  }
}

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

// 将 props 事件映射到 bar 索引
function mapEvents(events: ChartEvent[], display: CalculatedBar[]): MappedEvent[] {
  return events.map((ev, n) => {
    const evTime = new Date(ev.time).getTime()
    let bestIdx = 0
    let bestDiff = Infinity
    display.forEach((d, i) => {
      const diff = Math.abs(new Date(d.time).getTime() - evTime)
      if (diff < bestDiff) {
        bestDiff = diff
        bestIdx = i
      }
    })
    const d = display[bestIdx]
    const sel = isSelectionHit(ev.type)
    return {
      ...ev,
      id: `evt_${n}`,
      index: bestIdx,
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
  return `<b>${fmt(row.price_low)}–${fmt(row.price_high)}</b><span>\u603b\u6210\u4ea4量 ${(row.total_volume / 10000).toFixed(1)}万 · ${share.toFixed(2)}%</span><span>\u4e70量 ${(row.bullish_volume / 10000).toFixed(1)}万 · \u5356量 ${(row.bearish_volume / 10000).toFixed(1)}万</span><span>价值区 ${row.is_value_area ? '是' : '否'} · 节点 ${node ? node.id : '—'}${row.is_poc ? ' · POC' : ''}${row.is_peak ? ' · PEAK' : ''}</span>`
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
  const layerSet = new Set(Object.entries(layers).filter(([, v]) => v).map(([k]) => k))
  const g = geometry(layerSet, w, h)
  const min = Math.min(...calc.map(d => d.low)) - 0.25
  const max = Math.max(...calc.map(d => d.high)) + 0.25
  const py = (v: number) => g.panes.price.top + (max - v) / (max - min) * (g.panes.price.bottom - g.panes.price.top)
  const plotW = g.plotRight - g.l
  const step = plotW / display.length
  const barW = Math.max(2.2, step * 0.56)
  // [Volume Profile] - 从后端 indicators 提取 VP 数据（SSOT，禁止前端重算）
  const profile = extractBackendProfile(indicators)

  // 1. 背景 + 网格
  drawGrid(ctx, w, h, g, min, max)

  // 2. 右侧 Volume Profile（后端 profile_rows 直接渲染；缺失时显示提示）
  if (layers.profile) {
    if (profile && profile.rows.length > 0) {
      renderProfile(ctx, profile, g, py, state, layerSet)
    } else {
      // 后端 VP 数据缺失：在 VP 区域中央显示灰色提示（禁止降级到前端算法）
      const cx = (g.profileStart + g.profileEnd) / 2
      const cy = (g.panes.price.top + g.panes.price.bottom) / 2
      drawText(ctx, '筹码分布暂不可用', cx, cy, C.text, '11px sans-serif', 'center')
    }
  }

  // 3. Node Cluster 主图叠加（从后端 upper_node/lower_node/peak_rows 提取的节点，含多空量标签与迷你多空柱）
  if (layers.node && profile && profile.nodes.length > 0) {
    const backendNodes = profile.nodes
    const maxVol = Math.max(...backendNodes.map(n => Math.max(n.bullish_volume, n.bearish_volume)), 1)
    backendNodes.forEach(n => {
      const y1 = py(n.hi)
      const y2 = py(n.lo)
      const selected = state.selectedNodeId === n.id
      ctx.fillStyle = n.poc ? 'rgba(255,152,0,.11)' : selected ? 'rgba(156,179,255,.15)' : 'rgba(79,124,255,.075)'
      ctx.fillRect(g.l, y1, plotW, y2 - y1)
      drawLine(ctx, g.l, py(n.mid), g.plotRight, py(n.mid), n.poc ? C.orange : selected ? '#dce6ff' : C.blue, selected ? 2 : 1, n.poc ? [8, 4] : [4, 5])
      // 峰价格标签
      const labelText = n.poc ? `POC 峰 ${fmt(n.mid)}` : `峰 ${fmt(n.mid)}`
      drawText(ctx, labelText, g.l + 5, y1 + 10, n.poc ? C.orange : C.blue, '11px sans-serif')
      // 多空量标签 + 迷你多空柱（A 股：多头红色 / 空头绿色）
      if (n.bullish_volume > 0 || n.bearish_volume > 0) {
        const volText = `多 ${formatVolume(n.bullish_volume)} / 空 ${formatVolume(n.bearish_volume)}`
        drawText(ctx, volText, g.l + 5, y1 + 22, C.text2, '9px sans-serif')
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
  if (layers.poc && profile && profile.pocPrice != null) {
    const pocVal = profile.pocPrice
    drawLine(ctx, g.l, py(pocVal), layers.profile ? g.profileEnd : g.plotRight, py(pocVal), C.orange, 1.35, [9, 4])
    drawText(ctx, `POC ${fmt(pocVal)}`, g.plotRight - 62, py(pocVal) - 5, C.orange, '9px sans-serif')
  }

  // 5. 突破压力区
  if (layers.breakout) {
    renderBreakout(ctx, g, display, py)
  }

  // 7. 通用渲染器：渲染后端返回的策略指标图层（DSA VWAP 等）
  if (indicators && indicators.layers && indicators.data) {
    indicators.layers.forEach(layer => {
      // DSA VWAP 指标受 dsa 图层开关控制
      if (layer.layer_id === 'dsa_vwap' && !layers.dsa) return
      if (layer.layer_id === 'bb' && !layers.bb) return
      const layerData = indicators.data![layer.strategy_id]
      if (layerData) {
        renderIndicatorLayer(ctx, g, layer, layerData, display.length, step, py)
      }
    })
  }

  // 8. K 线蜡烛图
  display.forEach((d, i) => {
    const x = g.l + (i + 0.5) * step
    const col = d.close >= d.open ? C.up : C.down
    drawLine(ctx, x, py(d.high), x, py(d.low), col)
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
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.arc(x, y, 8, 0, Math.PI * 2)
      ctx.stroke()
    }
    state.eventHit.push({ ...ev, x, y })
  })

  // 10. Volume 副图
  if (layers.volume) renderVolume(ctx, g, display, step, barW)

  // 11. 时间轴刻度
  const labels = timeTicks(display, 7, timeframe)
  labels.forEach((item, i) => {
    drawText(ctx, item.label, g.l + plotW * i / (labels.length - 1), h - 7, C.text, '9px sans-serif', i === 0 ? 'left' : i === labels.length - 1 ? 'right' : 'center')
  })

  // 12. 最新价虚线 + 右侧价格标签
  const last = display[display.length - 1]
  drawLine(ctx, g.l, py(last.close), g.plotRight, py(last.close), last.close >= last.open ? C.up : C.down, 1, [3, 3])
  ctx.fillStyle = last.close >= last.open ? C.up : C.down
  ctx.fillRect(w - g.axis + 2, py(last.close) - 8, 50, 16)
  drawText(ctx, fmt(last.close), w - g.axis + 27, py(last.close) + 3, '#fff', '10px monospace', 'center')

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
    breakout: false,
    selection: false,
    node: false,
    poc: false,
    profile: false,
    bb: false,
    delta: false,
    events: false,
  }
  if (strategyId && STRATEGIES[strategyId]) {
    STRATEGIES[strategyId].defaultLayers.forEach(id => {
      if (id in layers) layers[id as keyof LayerVisibility] = true
    })
  }
  return layers
}

// 判断 display group 是否全部激活
function isGroupActive(groupId: string, layers: LayerVisibility): boolean {
  const group = DISPLAY_GROUPS[groupId]
  if (!group) return false
  return group.layers.every(l => layers[l as keyof LayerVisibility])
}

// 切换 display group
function toggleGroup(groupId: string, layers: LayerVisibility): LayerVisibility {
  const group = DISPLAY_GROUPS[groupId]
  if (!group) return layers
  const allOn = group.layers.every(l => layers[l as keyof LayerVisibility])
  const newLayers = { ...layers }
  group.layers.forEach(l => {
    newLayers[l as keyof LayerVisibility] = !allOn
  })
  return newLayers
}

export function StrategyChart({
  symbol,
  bars,
  events = [],
  indicators,
  strategyId = 'default',
  source = 'watchlist',
  height = 660,
  timeframe = '1d',
  onTimeframeChange,
}: StrategyChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)

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
  })

  // 图层可见性（localStorage 持久化）
  const storageKey = `detail-chart-strategy-groups:${source}:${strategyId}`
  const [layers, setLayers] = useState<LayerVisibility>(() => {
    try {
      const saved = localStorage.getItem(storageKey)
      if (saved) return { ...getDefaultLayers(strategyId), ...JSON.parse(saved) }
    } catch {
      // ignore
    }
    return getDefaultLayers(strategyId)
  })

  // 显示 bar 数量（缩放控制）
  const [displayBars, setDisplayBars] = useState(250)

  // 十字线联动图例 bar 索引（-1 表示无十字线，显示最后一根）
  const [legendIdx, setLegendIdx] = useState(-1)

  // 计算指标
  const calc = useMemo(() => {
    if (!bars.length) return []
    const win = CALCULATION_WINDOWS[timeframe]?.bars || 180
    return addIndicators(bars.slice(-win))
  }, [bars, timeframe])

  // 可见 bars
  const display = useMemo(() => {
    const visibleCount = clamp(displayBars, 30, Math.min(250, calc.length))
    return calc.slice(-visibleCount)
  }, [calc, displayBars])

  // 映射事件到 bar 索引
  const mappedEvents = useMemo(() => {
    if (!events.length || !display.length) return []
    return mapEvents(events, display)
  }, [events, display])

  // 最新数据 ref（供 draw 函数读取）
  const dataRef = useRef({ calc, display, mappedEvents, layers, timeframe })
  dataRef.current = { calc, display, mappedEvents, layers, timeframe }

  // indicators ref（避免 draw 函数依赖 indicators 导致频繁重绘）
  const indicatorsRef = useRef<IndicatorResponse | undefined>(undefined)
  indicatorsRef.current = indicators

  // 绘制函数（稳定引用，从 dataRef 读取最新数据）
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || !canvas.offsetParent) return
    const { calc: c, display: d, mappedEvents: ev, layers: ly, timeframe: tf } = dataRef.current
    if (!d.length) return
    drawTrading(canvas, c, d, ev, ly, tf, stateRef.current, indicatorsRef.current)
  }, [])

  // 数据/图层变化时重绘
  useEffect(() => {
    draw()
  }, [draw, calc, display, mappedEvents, layers, displayBars, indicators])

  // 持久化图层可见性
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(layers))
    } catch {
      // ignore
    }
  }, [layers, storageKey])

  // 交互事件绑定（仅一次）
  useEffect(() => {
    const canvas = canvasRef.current
    const tip = tipRef.current
    if (!canvas) return

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
        let indicatorHtml = ''
        if (indicatorsRef.current?.layers && indicatorsRef.current?.data) {
          indicatorsRef.current.layers.forEach(layer => {
            const layerData = indicatorsRef.current!.data[layer.strategy_id]
            if (!layerData) return
            const fields = layer.hover_fields.length ? layer.hover_fields : layer.fields
            const parts: string[] = []
            fields.forEach(f => {
              const vals = layerData[f]
              if (vals && vals[i] != null) {
                const label = f.replace(/_/g, ' ').toUpperCase()
                parts.push(`${label} ${fmt(vals[i]!)}`)
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

    canvas.addEventListener('mousemove', handleMouseMove)
    canvas.addEventListener('click', handleClick)
    canvas.addEventListener('mouseleave', handleMouseLeave)
    return () => {
      canvas.removeEventListener('mousemove', handleMouseMove)
      canvas.removeEventListener('click', handleClick)
      canvas.removeEventListener('mouseleave', handleMouseLeave)
    }
  }, [draw])

  // ResizeObserver 自动缩放
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    let timer: ReturnType<typeof setTimeout>
    const ro = new ResizeObserver(() => {
      clearTimeout(timer)
      timer = setTimeout(() => draw(), 120)
    })
    ro.observe(wrap)
    return () => {
      clearTimeout(timer)
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

  // 图层切换
  const handleToggleGroup = (groupId: string) => {
    setLayers(prev => toggleGroup(groupId, prev))
  }

  // 缩放控制
  const zoomIn = () => setDisplayBars(n => Math.max(30, n - 15))
  const zoomOut = () => setDisplayBars(n => Math.min(250, n + 15))
  const resetZoom = () => setDisplayBars(250)

  const hasData = bars.length > 0

  return (
    <div className="strategy-chart-wrap">
      {/* 工具栏 */}
      <div className="tv-chart-toolbar">
        <b className="tv-symbol-code">{symbol}</b>
        {legendData && (
          <div className="chart-ohlc">
            <span>{formatTime(legendData.d.time)}</span>
            <span>开 {fmt(legendData.d.open)}</span>
            <span>高 {fmt(legendData.d.high)}</span>
            <span>低 {fmt(legendData.d.low)}</span>
            <span>收 {fmt(legendData.d.close)}</span>
            <span className={legendData.change >= 0 ? 'pos' : 'neg'}>
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
      </div>

      {/* 策略图示区：按 DISPLAY_GROUPS 驱动 */}
      <div className="tv-strategy-legend">
        <span className="tv-strategy-legend-label">策略图层</span>
        {Object.values(DISPLAY_GROUPS).map((g: DisplayGroupDef) => {
          const active = isGroupActive(g.id, layers)
          return (
            <label
              key={g.id}
              className={clsx('tv-strategy-legend-item', !active && 'off')}
              onClick={() => handleToggleGroup(g.id)}
            >
              <i className="tv-legend-dot" style={{ '--legend-color': g.color } as React.CSSProperties} />
              <b>{g.shortName}</b>
              <i className="tv-mini-switch" />
            </label>
          )
        })}
      </div>

      {/* 图表画布 */}
      <div className="tv-canvas-wrap" ref={wrapRef} style={{ '--tv-chart-height': `${height}px` } as React.CSSProperties}>
        <canvas ref={canvasRef} />
        <div className="chart-crosshair-tooltip" ref={tipRef} />
        {!hasData && <div className="tv-chart-empty">暂无行情数据</div>}
      </div>
    </div>
  )
}

export default StrategyChart
