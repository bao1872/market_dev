import { useEffect, useMemo, useRef, useState } from 'react'
import type { ResearchPoint } from '@/api/strategyResearch'
import { modelVisual } from './evaluationLogic'

type Props = {
  points: ResearchPoint[]
  modelIds: string[]
  gain: number
  risk: number
  onSelect: (point: ResearchPoint) => void
}

/** Empirical 2-D binned density + observed points. No normality assumption. */
export default function OpportunityJointChart({ points, modelIds, gain, risk, onSelect }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [width, setWidth] = useState(800)
  const [height] = useState(350)
  const visible = useMemo(() => points.filter(p => modelIds.includes(p.model_id)), [points, modelIds])
  const xmax = Math.max(12, gain + 3, ...visible.map(p => p.mfe_pct)) * 1.07
  const ymax = Math.max(12, risk + 3, ...visible.map(p => p.mae_pct)) * 1.07
  const axes = { l: 54, t: 22, r: 18, b: 38 }
  const plotW = width - axes.l - axes.r
  const plotH = height - axes.t - axes.b
  const px = (x: number) => axes.l + (x / xmax) * plotW
  const py = (y: number) => height - axes.b - (y / ymax) * plotH

  useEffect(() => {
    const el = canvas.current
    if (!el) return
    const ro = new ResizeObserver(entries => setWidth(Math.max(340, Math.round(entries[0].contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    const el = canvas.current
    if (!el) return
    const ratio = Math.min(2, window.devicePixelRatio || 1)
    el.width = Math.round(width * ratio)
    el.height = Math.round(height * ratio)
    const c = el.getContext('2d')
    if (!c) return
    c.scale(ratio, ratio)
    c.fillStyle = '#111A23'; c.fillRect(0, 0, width, height)
    c.lineWidth = 1
    c.font = '12px system-ui'
    for (let n = 0; n <= 5; n++) {
      const x = axes.l + (plotW * n) / 5
      const y = axes.t + (plotH * n) / 5
      c.strokeStyle = '#263440'; c.beginPath(); c.moveTo(x, axes.t); c.lineTo(x, height - axes.b); c.stroke()
      c.beginPath(); c.moveTo(axes.l, y); c.lineTo(width - axes.r, y); c.stroke()
      c.fillStyle = '#98A1B3'; c.fillText(`${(xmax * n / 5).toFixed(0)}%`, x - 9, height - 17)
      c.fillText(`${(ymax * (5 - n) / 5).toFixed(0)}%`, 9, y + 4)
    }
    // Binned empirical 2-D density, normalized within each model.
    const cells = 17
    modelIds.forEach((id, mi) => {
      const subset = visible.filter(p => p.model_id === id)
      if (!subset.length) return
      const counts = Array.from({ length: cells * cells }, () => 0)
      for (const p of subset) {
        const xi = Math.max(0, Math.min(cells - 1, Math.floor(p.mfe_pct / xmax * cells)))
        const yi = Math.max(0, Math.min(cells - 1, Math.floor(p.mae_pct / ymax * cells)))
        counts[yi * cells + xi]++
      }
      const max = Math.max(...counts)
      const color = modelVisual(id, mi).color
      counts.forEach((n, i) => {
        if (!n) return
        const xi = i % cells, yi = Math.floor(i / cells)
        c.globalAlpha = .07 + .30 * Math.sqrt(n / max)
        c.fillStyle = color
        c.fillRect(px(xi * xmax / cells), py((yi + 1) * ymax / cells), plotW / cells + .4, plotH / cells + .4)
      })
      c.globalAlpha = 1
    })
    // Threshold region: target achieved at risk <= limit (not executable P&L).
    c.setLineDash([5, 4]); c.strokeStyle = '#F2F6F8'; c.lineWidth = 1.1
    c.beginPath(); c.moveTo(px(gain), axes.t); c.lineTo(px(gain), height - axes.b); c.stroke()
    c.beginPath(); c.moveTo(axes.l, py(risk)); c.lineTo(width - axes.r, py(risk)); c.stroke()
    c.setLineDash([])
    visible.forEach((p) => {
      const idx = modelIds.indexOf(p.model_id)
      const visual = modelVisual(p.model_id, idx)
      const x = px(p.mfe_pct), y = py(p.mae_pct), size = 3.4
      c.strokeStyle = visual.color; c.fillStyle = visual.color; c.globalAlpha = .78
      c.beginPath()
      if (visual.shape === 'circle') c.arc(x, y, size, 0, Math.PI * 2)
      else if (visual.shape === 'triangle') { c.moveTo(x, y - size); c.lineTo(x + size, y + size); c.lineTo(x - size, y + size) }
      else if (visual.shape === 'diamond') { c.moveTo(x, y - size); c.lineTo(x + size, y); c.lineTo(x, y + size); c.lineTo(x - size, y) }
      else c.rect(x - size, y - size, size * 2, size * 2)
      c.closePath(); c.fill()
    })
    c.globalAlpha = 1
    c.fillStyle = '#98A1B3'; c.fillText('MFE：最大有利空间 →', width - 176, height - 2)
    c.save(); c.translate(13, 190); c.rotate(-Math.PI / 2); c.fillText('MAE 风险幅度 →', 0, 0); c.restore()
  }, [width, height, modelIds, visible, gain, risk, xmax, ymax, plotW, plotH])

  function click(ev: React.MouseEvent<HTMLCanvasElement>) {
    const rect = ev.currentTarget.getBoundingClientRect()
    const x = (ev.clientX - rect.left) * width / rect.width
    const y = (ev.clientY - rect.top) * height / rect.height
    let best: ResearchPoint | null = null
    let dist = 90
    for (const p of visible) {
      const d = (px(p.mfe_pct) - x) ** 2 + (py(p.mae_pct) - y) ** 2
      if (d < dist) { dist = d; best = p }
    }
    if (best) onSelect(best)
  }
  return <canvas ref={canvas} onClick={click} style={{ width: '100%', height, cursor: 'crosshair', display: 'block' }} aria-label="多模型 MFE MAE 二维经验分布与事件散点图" />
}
