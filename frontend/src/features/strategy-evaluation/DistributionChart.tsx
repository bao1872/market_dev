import type { MetricKey, ModelEvaluation } from '@/api/strategyResearch'
import { modelVisual } from './evaluationLogic'

type Props = { models: ModelEvaluation[]; metric: MetricKey }

/** Relative-frequency histograms. The chart is a display-only transform of server bins. */
export default function DistributionChart({ models, metric }: Props) {
  const charts = models.map((m, index) => ({ id: m.model_id, name: m.model_name, bins: m.distributions[metric], color: modelVisual(m.model_id, index).color }))
  const all = charts.flatMap(c => c.bins)
  if (!all.length) return <div style={{ padding: 36, color: '#98A1B3' }}>当前无可用的历史路径分布</div>
  const min = Math.min(...all.map(b => b.low))
  const max = Math.max(...all.map(b => b.high))
  const span = Math.max(.000001, max - min)
  const pad = { l: 35, r: 16, t: 13, b: 29 }
  const w = 800, h = 245
  const pointX = (x: number) => pad.l + ((x - min) / span) * (w - pad.l - pad.r)
  const normalized = charts.map(c => {
    const n = c.bins.reduce((s, b) => s + b.count, 0)
    return { ...c, bins: c.bins.map(b => ({ ...b, probability: n ? b.count / n : 0 })) }
  })
  const ymax = Math.max(.01, ...normalized.flatMap(c => c.bins.map(b => b.probability))) * 1.08
  const pointY = (y: number) => h - pad.b - (y / ymax) * (h - pad.t - pad.b)
  return <svg viewBox={`0 0 ${w} ${h}`} style={{ width: '100%', display: 'block' }} role="img" aria-label={`${metric} 多模型历史经验频率分布`}>
    {[0, .25, .5, .75, 1].map(t => <g key={t}><line x1={pad.l} y1={pointY(ymax * t)} x2={w - pad.r} y2={pointY(ymax * t)} stroke="#263440" /><text fill="#98A1B3" x="3" y={pointY(ymax * t) + 4} fontSize="11">{(ymax * t * 100).toFixed(0)}%</text></g>)}
    {normalized.map(c => {
      const pts = c.bins.map(b => [pointX((b.low + b.high) / 2), pointY(b.probability)] as const)
      const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
      return <path key={c.id} d={d} fill="none" stroke={c.color} strokeWidth="2.5" strokeLinejoin="round" opacity=".95" />
    })}
    {[0,.25,.5,.75,1].map(t => <text key={t} x={pointX(min + t * span)} y={h - 7} textAnchor="middle" fill="#98A1B3" fontSize="11">{(min + t * span).toFixed(1)}%</text>)}
  </svg>
}
