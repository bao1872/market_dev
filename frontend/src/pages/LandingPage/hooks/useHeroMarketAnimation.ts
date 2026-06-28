// [门户] - 描述: Hero 区 K线 + 成交分布 Canvas 动画 hook
// 演示数据，非实时行情；支持 prefers-reduced-motion 与卸载时清理
import { useEffect, useRef, useState, type RefObject } from 'react'
import { scenarios, type Scenario } from '../landingData'

// ===== 演示数据生成（移植自原型 buildScenario） =====
interface Candle { o: number; c: number; h: number; l: number; v: number; time: string }

function seededNoise(i: number, k = 1): number {
  return Math.sin(i * 1.73 * k) * 0.24 + Math.cos(i * 0.61 * k) * 0.18 + Math.sin(i * 0.19 * k) * 0.11
}
function makeTime(i: number): string {
  return `${String(9 + Math.floor((30 + i * 15) / 60)).padStart(2, '0')}:${String((30 + i * 15) % 60).padStart(2, '0')}`
}
function buildScenario(type: Scenario['triggerType'], total: number): Candle[] {
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
function computeVolumeProfile(visible: Candle[], bins = 42, bounds: PriceBounds): VolumeProfile {
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
function yMap(p: number, b: PriceBounds, h: number, top = 20, bottom = 32): number {
  return top + (b.max - p) / (b.max - b.min) * (h - top - bottom)
}
function drawGrid(c: CanvasRenderingContext2D, w: number, h: number) {
  c.strokeStyle = 'rgba(143,171,212,.10)'; c.lineWidth = 1
  for (let i = 1; i < 6; i++) { const y = i * h / 6; c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke() }
  for (let i = 1; i < 8; i++) { const x = i * w / 8; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, h); c.stroke() }
}
function drawCandles(c: CanvasRenderingContext2D, arr: Candle[], b: PriceBounds, w: number, h: number, opts: { top: number; bottom: number }) {
  const gap = (w - 44) / Math.max(arr.length, 1), barW = Math.max(3, gap * 0.58)
  arr.forEach((x, i) => {
    const px = 22 + i * gap, up = x.c >= x.o, col = up ? '#ff4f56' : '#21c477'
    const yo = yMap(x.o, b, h, opts.top, opts.bottom), yc = yMap(x.c, b, h, opts.top, opts.bottom)
    const yh = yMap(x.h, b, h, opts.top, opts.bottom), yl = yMap(x.l, b, h, opts.top, opts.bottom)
    c.strokeStyle = col; c.beginPath(); c.moveTo(px + barW / 2, yh); c.lineTo(px + barW / 2, yl); c.stroke()
    c.fillStyle = col; c.fillRect(px, Math.min(yo, yc), barW, Math.max(2, Math.abs(yc - yo)))
  })
}
function roundRect(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number, fill: boolean) {
  c.beginPath(); c.roundRect(x, y, w, h, r); if (fill) c.fill()
}

// ===== 场景状态文案 =====
function sceneStatus(triggerType: Scenario['triggerType'], frame: number): [string, string] {
  if (triggerType === 'breakout') {
    if (frame < 34) return ['成交正在聚集', '成交逐步集中']
    if (frame < 50) return ['价格接近集中区', '等待向上穿过']
    return ['突破后继续走高', '新的高点正在形成']
  }
  if (triggerType === 'fail') {
    if (frame < 34) return ['上方成交逐步集中', '观察承压区域']
    if (frame < 50) return ['价格上冲集中区', '尝试向上穿过']
    return ['上冲后回落', '未能站稳']
  }
  if (frame < 34) return ['下方成交逐步集中', '等待回踩']
  if (frame < 60) return ['价格回到集中区', '观察能否企稳']
  return ['企稳后重新向上', '接近上方区域']
}

export interface HeroRefs {
  klineCanvas: RefObject<HTMLCanvasElement>
  profileCanvas: RefObject<HTMLCanvasElement>
  clusterLabel: RefObject<HTMLDivElement>
  eventToast: RefObject<HTMLDivElement>
  triggerPulse: RefObject<HTMLDivElement>
  pocLine: RefObject<HTMLDivElement>
  tooltip: RefObject<HTMLDivElement>
  phaseLabel: RefObject<HTMLSpanElement>
  scenarioCount: RefObject<HTMLSpanElement>
  profileStatus: RefObject<HTMLElement>
}

export function useHeroMarketAnimation(refs: HeroRefs) {
  const [currentScenarioIndex, setCurrentScenarioIndex] = useState(0)
  const [playing, setPlaying] = useState(true)

  // 动画可变状态（ref 避免 RAF 闭包过期）
  const candlesRef = useRef<Candle[]>([])
  const boundsRef = useRef<PriceBounds>({ min: 0, max: 1 })
  const frameIndexRef = useRef(16)
  const hoverIndexRef = useRef(-1)
  const pinnedIndexRef = useRef(-1)
  const triggerShownRef = useRef(false)
  const lastFrameRef = useRef(0)
  const playingRef = useRef(true)
  const scenarioIndexRef = useRef(0)
  const rafRef = useRef<number>(0)

  const setScenario = (i: number) => {
    const sc = scenarios[i]
    scenarioIndexRef.current = i
    candlesRef.current = buildScenario(sc.triggerType, sc.total)
    boundsRef.current = priceBounds(candlesRef.current)
    frameIndexRef.current = 16
    hoverIndexRef.current = -1
    pinnedIndexRef.current = -1
    triggerShownRef.current = false
    setCurrentScenarioIndex(i)
    // 清理触发态
    const toast = refs.eventToast.current, pulse = refs.triggerPulse.current, label = refs.clusterLabel.current
    if (toast) toast.classList.remove('show')
    if (pulse) pulse.classList.remove('show')
    if (label) label.classList.remove('triggered')
    drawAll()
  }

  const togglePlay = () => {
    playingRef.current = !playingRef.current
    setPlaying(playingRef.current)
  }

  // 核心绘制
  function drawAll() {
    const kline = refs.klineCanvas.current
    if (!kline) return
    const ctx = kline.getContext('2d')!
    const w = kline.getBoundingClientRect().width, h = kline.getBoundingClientRect().height
    const candles = candlesRef.current, bounds = boundsRef.current, frame = frameIndexRef.current
    const visible = candles.slice(0, Math.max(8, frame))
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = '#050d1b'; ctx.fillRect(0, 0, w, h)
    drawGrid(ctx, w, h)
    const ps = computeVolumeProfile(visible, 42, bounds)
    const y1 = yMap(ps.high, bounds, h), y2 = yMap(ps.low, bounds, h)
    ctx.fillStyle = 'rgba(23,111,215,.13)'
    roundRect(ctx, 25, y1, w - 50, Math.max(8, y2 - y1), 8, true)
    const label = refs.clusterLabel.current
    if (label) {
      label.textContent = `成交集中区 ${ps.low.toFixed(2)} - ${ps.high.toFixed(2)}`
      label.style.top = Math.max(6, y1 - 18) + 'px'
      label.style.left = Math.max(34, w * 0.18) + 'px'
    }
    drawCandles(ctx, visible, bounds, w, h, { top: 20, bottom: 32 })
    const gap = (w - 44) / candles.length, barW = Math.max(5, gap * 0.58)
    const hi = pinnedIndexRef.current >= 0 ? pinnedIndexRef.current : hoverIndexRef.current
    if (hi >= 0 && hi < visible.length) {
      const x = 22 + hi * gap + barW / 2
      ctx.strokeStyle = 'rgba(110,210,255,.8)'; ctx.setLineDash([4, 4])
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); ctx.setLineDash([])
    }
    const last = visible[visible.length - 1]
    const sc = scenarios[scenarioIndexRef.current]
    if (last) {
      const x = 22 + (visible.length - 1) * gap + barW / 2
      const y = yMap(last.c, bounds, h)
      let entered = false
      if (sc.triggerType === 'breakout') entered = visible.length > 50 && last.c > ps.high
      else if (sc.triggerType === 'fail') entered = visible.length > 50 && last.c < ps.high && last.c > ps.low && visible[visible.length - 2] && visible[visible.length - 2].c > last.c
      else entered = visible.length > 62 && last.c > ps.pocPrice && last.c < ps.high + 1.8
      if (entered && !triggerShownRef.current) showTrigger(ps, last, sc)
      if (triggerShownRef.current) {
        const pulse = refs.triggerPulse.current
        if (pulse) { pulse.style.left = (x - 18) + 'px'; pulse.style.top = (y - 18) + 'px' }
      }
    }
    drawProfile(ps)
    updatePhase(sc, frame)
  }

  function drawProfile(ps: VolumeProfile) {
    const profile = refs.profileCanvas.current
    if (!profile) return
    const pctx = profile.getContext('2d')!
    const w = profile.getBoundingClientRect().width, h = profile.getBoundingClientRect().height
    pctx.clearRect(0, 0, w, h); pctx.fillStyle = '#071224'; pctx.fillRect(0, 0, w, h)
    const rowH = h / ps.total.length
    for (let i = 0; i < ps.total.length; i++) {
      const y = h - (i + 1) * rowH + 1
      const totalLen = ps.total[i] / ps.max * (w - 8)
      const upLen = ps.up[i] / ps.max * (w - 8)
      const downLen = ps.down[i] / ps.max * (w - 8)
      const dense = i >= ps.left && i <= ps.right
      pctx.fillStyle = dense ? 'rgba(255,75,91,.82)' : 'rgba(255,75,91,.43)'
      pctx.fillRect(2, y, upLen, Math.max(3, rowH - 2))
      pctx.fillStyle = dense ? 'rgba(43,112,244,.88)' : 'rgba(43,112,244,.43)'
      pctx.fillRect(2 + upLen, y, downLen, Math.max(3, rowH - 2))
      if (i === ps.poc) {
        pctx.strokeStyle = 'rgba(94,194,255,.95)'
        pctx.strokeRect(1, y - 1, Math.max(9, totalLen + 2), Math.max(4, rowH))
      }
    }
    const py = (ps.bounds.max - ps.pocPrice) / (ps.bounds.max - ps.bounds.min) * h
    const pocLine = refs.pocLine.current
    if (pocLine) {
      pocLine.style.top = (py + 58) + 'px'
      const span = pocLine.querySelector('span')
      if (span) span.textContent = '成交最集中价 ' + ps.pocPrice.toFixed(2)
    }
  }

  function showTrigger(ps: VolumeProfile, last: Candle, sc: Scenario) {
    triggerShownRef.current = true
    const toast = refs.eventToast.current, pulse = refs.triggerPulse.current, label = refs.clusterLabel.current
    if (toast) toast.classList.add('show')
    if (pulse) pulse.classList.add('show')
    if (label) label.classList.add('triggered')
    if (toast) {
      const timeEl = toast.querySelector('[data-toast-time]')
      if (timeEl) timeEl.textContent = sc.eventTime
      const textEl = toast.querySelector('[data-toast-text]')
      if (textEl) {
        let html = ''
        if (sc.triggerType === 'breakout') html = `价格 ${last.c.toFixed(2)} 向上穿过成交集中区<br>突破区间 ${ps.low.toFixed(2)} - ${ps.high.toFixed(2)}`
        else if (sc.triggerType === 'fail') html = `价格上冲成交集中区后回落<br>观察区间 ${ps.low.toFixed(2)} - ${ps.high.toFixed(2)}`
        else html = `价格回踩成交集中区后重新向上<br>当前区域 ${ps.low.toFixed(2)} - ${ps.high.toFixed(2)}`
        textEl.innerHTML = html
      }
    }
  }

  function updatePhase(sc: Scenario, frame: number) {
    const [labelText, statusText] = sceneStatus(sc.triggerType, frame)
    if (refs.phaseLabel.current) refs.phaseLabel.current.textContent = labelText
    if (refs.scenarioCount.current) refs.scenarioCount.current.textContent = `${scenarioIndexRef.current + 1} / ${scenarios.length}`
    if (refs.profileStatus.current) refs.profileStatus.current.textContent = statusText
  }

  function nextScenario() {
    setScenario((scenarioIndexRef.current + 1) % scenarios.length)
  }

  // RAF 循环
  function animate(ts: number) {
    if (playingRef.current && ts - lastFrameRef.current > 270) {
      frameIndexRef.current++
      lastFrameRef.current = ts
      if (frameIndexRef.current > candlesRef.current.length + 12) nextScenario()
      drawAll()
    }
    rafRef.current = requestAnimationFrame(animate)
  }

  useEffect(() => {
    // 初始化场景
    setScenario(0)
    // 检测 prefers-reduced-motion
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (prefersReduced) {
      // 仅渲染静态首帧，不启动 RAF
      drawAll()
      return
    }
    // resize 监听
    const onResize = () => {
      const kline = refs.klineCanvas.current, profile = refs.profileCanvas.current
      if (kline) fitCanvas(kline)
      if (profile) fitCanvas(profile)
      drawAll()
    }
    window.addEventListener('resize', onResize)
    // 初始 fitCanvas
    const kline = refs.klineCanvas.current, profile = refs.profileCanvas.current
    if (kline) fitCanvas(kline)
    if (profile) fitCanvas(profile)
    drawAll()
    // 启动 RAF
    rafRef.current = requestAnimationFrame(animate)

    // 鼠标交互
    const klineEl = refs.klineCanvas.current
    const onMouseMove = (e: MouseEvent) => {
      if (!klineEl) return
      const r = klineEl.getBoundingClientRect()
      const x = e.clientX - r.left
      const gap = (r.width - 44) / candlesRef.current.length
      hoverIndexRef.current = Math.max(0, Math.min(frameIndexRef.current - 1, Math.floor((x - 22) / gap)))
      const c = candlesRef.current[hoverIndexRef.current]
      const tooltip = refs.tooltip.current
      if (tooltip && c) {
        tooltip.style.display = 'block'
        tooltip.style.left = (e.clientX + 14) + 'px'
        tooltip.style.top = (e.clientY + 14) + 'px'
        tooltip.innerHTML = `${c.time}<br>开 ${c.o.toFixed(2)}　高 ${c.h.toFixed(2)}<br>低 ${c.l.toFixed(2)}　收 ${c.c.toFixed(2)}<br>成交量 ${Math.round(c.v)} 万`
      }
      drawAll()
    }
    const onMouseLeave = () => {
      hoverIndexRef.current = -1
      const tooltip = refs.tooltip.current
      if (tooltip) tooltip.style.display = 'none'
      drawAll()
    }
    const onClick = () => {
      pinnedIndexRef.current = hoverIndexRef.current
      playingRef.current = false
      setPlaying(false)
      drawAll()
    }
    if (klineEl) {
      klineEl.addEventListener('mousemove', onMouseMove)
      klineEl.addEventListener('mouseleave', onMouseLeave)
      klineEl.addEventListener('click', onClick)
    }

    return () => {
      cancelAnimationFrame(rafRef.current)
      window.removeEventListener('resize', onResize)
      if (klineEl) {
        klineEl.removeEventListener('mousemove', onMouseMove)
        klineEl.removeEventListener('mouseleave', onMouseLeave)
        klineEl.removeEventListener('click', onClick)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { currentScenarioIndex, playing, setScenario, togglePlay }
}
