// [门户] - 描述: 工作流 Canvas + 个股详情 Canvas 动画 hook
// 演示数据，非实时行情；支持 prefers-reduced-motion 与卸载时清理
import { useEffect, useRef, useState, type RefObject } from 'react'
import { detailCases, workflowCopy } from '../landingData'

// ===== 复用 Hero hook 的演示数据生成逻辑（独立副本，避免循环依赖） =====
interface Candle { o: number; c: number; h: number; l: number; v: number; time: string }
type TriggerType = 'breakout' | 'fail' | 'rebound'

function seededNoise(i: number, k = 1): number {
  return Math.sin(i * 1.73 * k) * 0.24 + Math.cos(i * 0.61 * k) * 0.18 + Math.sin(i * 0.19 * k) * 0.11
}
function makeTime(i: number): string {
  return `${String(9 + Math.floor((30 + i * 15) / 60)).padStart(2, '0')}:${String((30 + i * 15) % 60).padStart(2, '0')}`
}
function buildScenario(type: TriggerType, total: number): Candle[] {
  const a: Candle[] = []
  let p = 132
  for (let i = 0; i < total; i++) {
    let target: number, volBase: number
    if (type === 'breakout') {
      if (i < 18) { target = 124 + i * 0.62; volBase = 72 + i * 1.8 }
      else if (i < 40) { target = 136.8 + Math.sin((i - 18) * 0.7) * 1.2; volBase = 148 + Math.abs(Math.sin(i * 0.45)) * 64 }
      else if (i < 52) { target = 137.2 + (i - 40) * 0.95; volBase = 126 + Math.abs(Math.cos(i * 0.35)) * 52 }
      else { target = 148.8 + (i - 52) * 0.33 + Math.sin(i * 0.32) * 0.35; volBase = 102 + Math.abs(Math.sin(i * 0.4)) * 44 }
      if (i === 48 || i === 49 || i === 50) volBase += 60
    } else if (type === 'fail') {
      if (i < 18) { target = 124 + i * 0.52; volBase = 70 + i * 1.6 }
      else if (i < 40) { target = 137.2 + Math.sin((i - 18) * 0.65) * 1.35; volBase = 152 + Math.abs(Math.sin(i * 0.48)) * 66 }
      else if (i < 50) { target = 136.5 + (i - 40) * 0.34; volBase = 110 + Math.abs(Math.cos(i * 0.39)) * 48 }
      else if (i < 58) { target = 139.1 - (i - 50) * 0.58; volBase = 116 + Math.abs(Math.sin(i * 0.36)) * 40 }
      else { target = 134.6 - (i - 58) * 0.18 + Math.sin(i * 0.33) * 0.28; volBase = 92 + Math.abs(Math.cos(i * 0.31)) * 32 }
      if (i === 49 || i === 50) volBase += 42
    } else {
      if (i < 18) { target = 126 + i * 0.46; volBase = 72 + i * 1.4 }
      else if (i < 32) { target = 133.2 + Math.sin((i - 18) * 0.6) * 1.05; volBase = 130 + Math.abs(Math.sin(i * 0.47)) * 56 }
      else if (i < 46) { target = 140.8 + (i - 32) * 0.18 + Math.sin(i * 0.28) * 0.4; volBase = 118 + Math.abs(Math.cos(i * 0.32)) * 48 }
      else if (i < 58) { target = 141.4 - (i - 46) * 0.78; volBase = 96 + Math.abs(Math.sin(i * 0.29)) * 34 }
      else if (i < 67) { target = 131.8 + Math.sin((i - 58) * 0.52) * 0.85; volBase = 145 + Math.abs(Math.sin(i * 0.51)) * 60 }
      else { target = 133.2 + (i - 67) * 0.66 + Math.sin(i * 0.26) * 0.38; volBase = 108 + Math.abs(Math.cos(i * 0.4)) * 46 }
      if (i === 60 || i === 61 || i === 62) volBase += 55
    }
    const o = p + seededNoise(i, 0.9) * 0.42
    const c = o + (target - o) * 0.62 + seededNoise(i + 7, 1.08) * 0.32
    const body = Math.abs(c - o)
    const h = Math.max(o, c) + 0.32 + Math.abs(Math.sin(i * 0.72)) * 0.58
    const l = Math.min(o, c) - 0.28 - Math.abs(Math.cos(i * 0.68)) * 0.52
    const v = volBase + body * 68
    a.push({ o, c, h, l, v, time: makeTime(i) })
    p = c
  }
  return a
}

interface PriceBounds { min: number; max: number }
function priceBounds(arr: Candle[]): PriceBounds {
  let min = Math.min(...arr.map(x => x.l)), max = Math.max(...arr.map(x => x.h))
  const pad = (max - min) * 0.07
  return { min: min - pad, max: max + pad }
}
interface VolumeProfile {
  up: number[]; down: number[]; total: number[]; poc: number; left: number; right: number
  step: number; bounds: PriceBounds; pocPrice: number; low: number; high: number; max: number
}
function computeVolumeProfile(visible: Candle[], bins: number, bounds: PriceBounds): VolumeProfile {
  const b = bounds, step = (b.max - b.min) / bins
  const up = new Array(bins).fill(0), down = new Array(bins).fill(0)
  visible.forEach(c => {
    const typical = (c.h + c.l + c.c) / 3
    const low = Math.max(0, Math.floor((c.l - b.min) / step))
    const high = Math.min(bins - 1, Math.ceil((c.h - b.min) / step))
    const weights: [number, number][] = []
    let sum = 0
    for (let i = low; i <= high; i++) {
      const price = b.min + (i + 0.5) * step
      const spread = Math.max(step, (c.h - c.l) * 0.34)
      const weight = Math.exp(-0.5 * Math.pow((price - typical) / spread, 2))
      weights.push([i, weight]); sum += weight
    }
    weights.forEach(([i, w]) => {
      const amount = c.v * w / (sum || 1)
      ;(c.c >= c.o ? up : down)[i] += amount
    })
  })
  const total = up.map((v, i) => v + down[i])
  let poc = 0
  for (let i = 1; i < bins; i++) if (total[i] > total[poc]) poc = i
  const max = Math.max(...total, 1), threshold = max * 0.68
  let left = poc, right = poc
  while (left > 0 && total[left - 1] >= threshold && right - left < 4) left--
  while (right < bins - 1 && total[right + 1] >= threshold && right - left < 4) right++
  if (left === right) {
    const lv = left > 0 ? total[left - 1] : -1, rv = right < bins - 1 ? total[right + 1] : -1
    if (rv >= lv && right < bins - 1) right++
    else if (left > 0) left--
  }
  return { up, down, total, poc, left, right, step, bounds: b, pocPrice: b.min + (poc + 0.5) * step, low: b.min + left * step, high: b.min + (right + 1) * step, max }
}

// ===== Canvas 绘制工具 =====
function fitCanvas(c: HTMLCanvasElement) {
  const r = c.getBoundingClientRect(), d = window.devicePixelRatio || 1
  c.width = Math.max(1, Math.round(r.width * d))
  c.height = Math.max(1, Math.round(r.height * d))
  const x = c.getContext('2d')!
  x.setTransform(d, 0, 0, d, 0, 0)
}
function yMap(p: number, b: PriceBounds, h: number, top: number, bottom: number): number {
  return top + (b.max - p) / (b.max - b.min) * (h - top - bottom)
}
function drawGrid(c: CanvasRenderingContext2D, w: number, h: number) {
  c.strokeStyle = 'rgba(143,171,212,.10)'; c.lineWidth = 1
  for (let i = 1; i < 6; i++) { const y = i * h / 6; c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke() }
  for (let i = 1; i < 8; i++) { const x = i * w / 8; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, h); c.stroke() }
}
function drawCandles(c: CanvasRenderingContext2D, arr: Candle[], b: PriceBounds, w: number, h: number, top: number, bottom: number) {
  const gap = (w - 44) / Math.max(arr.length, 1), barW = Math.max(3, gap * 0.58)
  arr.forEach((x, i) => {
    const px = 22 + i * gap, up = x.c >= x.o, col = up ? '#ff4f56' : '#21c477'
    const yo = yMap(x.o, b, h, top, bottom), yc = yMap(x.c, b, h, top, bottom)
    const yh = yMap(x.h, b, h, top, bottom), yl = yMap(x.l, b, h, top, bottom)
    c.strokeStyle = col; c.beginPath(); c.moveTo(px + barW / 2, yh); c.lineTo(px + barW / 2, yl); c.stroke()
    c.fillStyle = col; c.fillRect(px, Math.min(yo, yc), barW, Math.max(2, Math.abs(yc - yo)))
  })
}
function roundRect(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number, fill: boolean) {
  c.beginPath(); c.roundRect(x, y, w, h, r); if (fill) c.fill()
}

export interface WorkflowRefs {
  workflowCanvas: RefObject<HTMLCanvasElement>
  detailCanvas: RefObject<HTMLCanvasElement>
  workflowScan: RefObject<HTMLDivElement>
  workflowZone: RefObject<HTMLDivElement>
  workflowAlert: RefObject<HTMLDivElement>
  industryPanel: RefObject<HTMLDivElement>
  workflowChart: RefObject<HTMLDivElement>
  detailZone: RefObject<HTMLDivElement>
  detailMarker: RefObject<HTMLDivElement>
}

export function useWorkflowAnimation(refs: WorkflowRefs) {
  const [workflowStage, setWorkflowStageState] = useState(0)
  const [activeDetail, setActiveDetailState] = useState(0)

  // 工作流动画状态
  const workflowStageRef = useRef(0)
  const workflowStageStartRef = useRef(0)
  const workflowDataRef = useRef<Candle[]>(buildScenario('breakout', 82))
  const workflowBoundsRef = useRef<PriceBounds>(priceBounds(workflowDataRef.current))

  // 详情动画状态
  const activeDetailRef = useRef(0)
  const detailFrameRef = useRef(18)
  const detailLastRef = useRef(0)
  const detailDataRef = useRef<Candle[]>(buildScenario('breakout', 72))
  const detailBoundsRef = useRef<PriceBounds>(priceBounds(detailDataRef.current))

  const rafRef = useRef<number>(0)

  // 工作流阶段切换
  function applyWorkflowStage(i: number) {
    workflowStageRef.current = i
    workflowStageStartRef.current = performance.now()
    setWorkflowStageState(i)
    const d = workflowCopy[i]
    const scan = refs.workflowScan.current
    const panel = refs.industryPanel.current
    const chart = refs.workflowChart.current
    const zone = refs.workflowZone.current
    const alert = refs.workflowAlert.current
    if (scan) scan.classList.toggle('show', i === 0)
    if (panel) panel.classList.toggle('show', i === 1)
    if (chart) chart.classList.toggle('stageIndustry', i === 1)
    if (zone) zone.classList.toggle('show', i === 2)
    if (alert) alert.classList.toggle('show', i === 2)
    // 更新文案由组件通过 state 渲染，这里只管动画层
    void d
  }

  const setWorkflowStage = (i: number) => applyWorkflowStage(i)

  // 详情切换
  function applyDetail(i: number) {
    activeDetailRef.current = i
    const d = detailCases[i]
    detailDataRef.current = buildScenario(d.type, d.type === 'rebound' ? 78 : 72)
    detailBoundsRef.current = priceBounds(detailDataRef.current)
    detailFrameRef.current = 18
    setActiveDetailState(i)
    drawDetail()
  }

  const setActiveDetail = (i: number) => applyDetail(i)

  // 工作流 Canvas 绘制
  function drawWorkflow(ts: number) {
    const canvas = refs.workflowCanvas.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')!
    const w = canvas.getBoundingClientRect().width, h = canvas.getBoundingClientRect().height
    if (!w || !h) return
    ctx.clearRect(0, 0, w, h); ctx.fillStyle = '#061224'; ctx.fillRect(0, 0, w, h)
    const stage = workflowStageRef.current
    if (stage === 1) return // 产业验证阶段不画 K 线
    const elapsed = (ts - workflowStageStartRef.current) % 5200
    const progress = Math.min(1, elapsed / 4000)
    drawGrid(ctx, w, h)
    const count = stage === 0 ? Math.floor(24 + progress * 16) : Math.floor(52 + progress * 26)
    const visible = workflowDataRef.current.slice(0, count)
    const ps = computeVolumeProfile(visible, 34, workflowBoundsRef.current)
    drawCandles(ctx, visible, workflowBoundsRef.current, w, h, 18, 24)
    if (stage === 0) {
      const scanX = w * (0.12 + 0.72 * progress)
      ctx.fillStyle = 'rgba(45,141,255,.08)'
      ctx.fillRect(scanX - 32, 20, 64, h - 44)
      ctx.strokeStyle = 'rgba(79,188,255,.9)'
      ctx.beginPath(); ctx.moveTo(scanX, 20); ctx.lineTo(scanX, h - 24); ctx.stroke()
    }
    if (stage === 2) {
      const y1 = yMap(ps.high, workflowBoundsRef.current, h, 18, 24)
      const y2 = yMap(ps.low, workflowBoundsRef.current, h, 18, 24)
      ctx.fillStyle = 'rgba(24,111,218,.13)'
      roundRect(ctx, 18, y1, w - 36, Math.max(7, y2 - y1), 7, true)
      const zone = refs.workflowZone.current
      if (zone) {
        zone.style.top = Math.max(54, 48 + y1 - 15) + 'px'
        zone.style.height = 'auto'
      }
      if (progress > 0.58) {
        const alert = refs.workflowAlert.current
        if (alert) alert.classList.add('show')
      }
    }
  }

  // 详情 Canvas 绘制
  function drawDetail() {
    const canvas = refs.detailCanvas.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')!
    const w = canvas.getBoundingClientRect().width, h = canvas.getBoundingClientRect().height
    if (!w || !h) return
    const visible = detailDataRef.current.slice(0, Math.max(8, detailFrameRef.current))
    const ps = computeVolumeProfile(visible, 34, detailBoundsRef.current)
    ctx.clearRect(0, 0, w, h); ctx.fillStyle = '#061224'; ctx.fillRect(0, 0, w, h)
    drawGrid(ctx, w, h)
    const y1 = yMap(ps.high, detailBoundsRef.current, h, 14, 26)
    const y2 = yMap(ps.low, detailBoundsRef.current, h, 14, 26)
    ctx.fillStyle = 'rgba(26,110,214,.13)'
    roundRect(ctx, 16, y1, w - 32, Math.max(7, y2 - y1), 7, true)
    drawCandles(ctx, visible, detailBoundsRef.current, w, h, 14, 26)
    const zone = refs.detailZone.current
    if (zone) {
      zone.style.top = Math.max(6, y1 - 17) + 'px'
      zone.style.height = 'auto'
    }
    const marker = refs.detailMarker.current
    const last = visible[visible.length - 1]
    if (marker && last && visible.length > 50) {
      const gap = (w - 44) / detailDataRef.current.length
      const x = 22 + (visible.length - 1) * gap
      const y = yMap(last.c, detailBoundsRef.current, h, 14, 26)
      marker.style.left = (x - 9) + 'px'
      marker.style.top = (y - 9) + 'px'
      marker.classList.add('show')
    } else if (marker) {
      marker.classList.remove('show')
    }
  }

  // RAF 循环
  function animate(ts: number) {
    if (ts - detailLastRef.current > 250) {
      detailFrameRef.current++
      detailLastRef.current = ts
      if (detailFrameRef.current > detailDataRef.current.length + 8) {
        applyDetail((activeDetailRef.current + 1) % detailCases.length)
      }
      drawDetail()
    }
    if (!workflowStageStartRef.current) workflowStageStartRef.current = ts
    if (ts - workflowStageStartRef.current > 5400) {
      applyWorkflowStage((workflowStageRef.current + 1) % 3)
    }
    drawWorkflow(ts)
    rafRef.current = requestAnimationFrame(animate)
  }

  useEffect(() => {
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    // 初始化
    const wfCanvas = refs.workflowCanvas.current, dtCanvas = refs.detailCanvas.current
    if (wfCanvas) fitCanvas(wfCanvas)
    if (dtCanvas) fitCanvas(dtCanvas)
    applyWorkflowStage(0)
    applyDetail(0)
    drawDetail()
    drawWorkflow(performance.now())

    if (prefersReduced) return

    const onResize = () => {
      if (wfCanvas) fitCanvas(wfCanvas)
      if (dtCanvas) fitCanvas(dtCanvas)
      drawWorkflow(performance.now())
      drawDetail()
    }
    window.addEventListener('resize', onResize)
    rafRef.current = requestAnimationFrame(animate)

    return () => {
      cancelAnimationFrame(rafRef.current)
      window.removeEventListener('resize', onResize)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { workflowStage, activeDetail, setWorkflowStage, setActiveDetail }
}
