/**
 * Candidate markers are anchored on anchor_date; model score badges are placed at
 * observation.decision_date, NEVER on the earlier anchor bar. The connecting
 * line is deliberately dotted. Both H and L candidates remain visible.
 * Score values are uncalibrated and must not be labelled probabilities.
 */
import type { KeyboardEvent } from 'react'
import type { Bar, Candidate } from '@/api/strategyCandidates'
import { displayedObservation, scoreBadgeDate } from './candidateObservation'
import styles from './strategyResearchPage.module.scss'

const fmt = (n: number | null | undefined): string => n == null ? '—' : n.toFixed(2)

function keyActivate(event: KeyboardEvent<SVGGElement>, fn: () => void) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    fn()
  }
}

export default function CandidateScoreChart({ bars, candidates, selectedId, stage, onSelect }: {
  bars: Bar[]
  candidates: Candidate[]
  selectedId: string | null
  stage: string
  onSelect: (candidateId: string, stage: string) => void
}) {
  if (!bars.length) return <div className={styles.empty}>该股票暂无已发布 K 线证据。</div>

  const chosen = candidates.find(c => c.candidate_id === selectedId)
  const chosenObs = chosen && displayedObservation(chosen, selectedId, stage)
  const center = chosenObs?.decision_date ?? chosen?.anchor_date
  const focus = center ? bars.findIndex(b => b.date === center) : -1
  const start = focus >= 0 ? Math.max(0, focus - 24) : Math.max(0, bars.length - 66)
  const visible = bars.slice(start, start + 66)
  const width = Math.max(860, visible.length * 16 + 100)
  const height = 420
  const lows = visible.map(b => b.low)
  const highs = visible.map(b => b.high)
  const priceMin = Math.min(...lows)
  const priceMax = Math.max(...highs)
  const spread = Math.max(.001, priceMax - priceMin)
  const y = (p: number) => 320 - 245 * (p - priceMin) / spread
  const xByDay = new Map(visible.map((b, i) => [b.date, 55 + i * 16]))
  const inView = candidates.filter(c => xByDay.has(c.anchor_date) || !!displayedObservation(c, selectedId, stage) && xByDay.has(displayedObservation(c, selectedId, stage)!.decision_date))

  return <div className={styles.chartScroll}>
    <svg className={styles.chart} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="历史候选高低点及实际模型决策日的第一层、第二层评分（同时显示买卖）">
      {[0,1,2,3,4].map(t => {
        const p = priceMin + spread * t / 4
        return <g key={t}>
          <line x1="38" x2={width-24} y1={y(p)} y2={y(p)} stroke="rgba(151,163,181,.22)"/>
          <text x="3" y={y(p)+4} fill="#9aa6b8" fontSize="10">{p.toFixed(2)}</text>
        </g>
      })}
      {visible.map((bar,i) => {
        const x = xByDay.get(bar.date)!
        const rising = bar.close >= bar.open
        return <g key={bar.date}>
          <line x1={x} x2={x} y1={y(bar.high)} y2={y(bar.low)} stroke={rising?'#39a995':'#df806d'} strokeWidth="1.5" />
          <rect x={x-4} y={Math.min(y(bar.open), y(bar.close))} width="8" height={Math.max(1, Math.abs(y(bar.open)-y(bar.close)))} fill={rising?'#39a995':'#df806d'} />
          {i%7 === 0 && <text x={x-14} y="384" fill="#8994a5" fontSize="10">{bar.date.slice(5)}</text>}
        </g>
      })}
      {inView.map((cand, i) => {
        const observation = displayedObservation(cand, selectedId, stage)
        const isHigh = cand.kind === 'H'
        const picked = cand.candidate_id === selectedId
        const anchorX = xByDay.get(cand.anchor_date)
        const scoreDate = scoreBadgeDate(cand, selectedId, stage)
        const decisionX = scoreDate ? xByDay.get(scoreDate) : undefined
        const anchorY = y(cand.anchor_price)
        // Labels are deliberately attached to DECISION days, not anchor days.
        const labelY = isHigh ? 40 + (i%4)*46 : 205 + (i%3)*46
        const selectedStage = observation?.stage ?? cand.observations[0]?.stage ?? 'D1'
        return <g key={cand.candidate_id} role="button" tabIndex={0}
          aria-label={`${isHigh?'卖出':'买入'}候选，锚点日${cand.anchor_date}，${observation?`模型决策日${observation.decision_date}、L1 ${fmt(observation.l1_score)}、L2 ${fmt(observation.l2_score)}`:'暂无模型决策评分'}`}
          onClick={() => onSelect(cand.candidate_id, selectedStage)}
          onKeyDown={e => keyActivate(e, () => onSelect(cand.candidate_id, selectedStage))}
          style={{ cursor:'pointer' }}>
          {anchorX != null && <g>
            <title>{`结构候选锚点 ${cand.anchor_date}（首次可观察 ${cand.visible_at}）。此处不是模型评分的产生日。`}</title>
            <path d={isHigh?`M${anchorX} ${anchorY-13} l-7 -11 h14 Z`:`M${anchorX} ${anchorY+13} l-7 11 h14 Z`}
              fill={isHigh?'#eaa477':'#6aaefa'} stroke={picked?'#fff':'none'} strokeWidth="1.8" />
          </g>}
          {anchorX != null && decisionX != null && anchorX!==decisionX && <line x1={anchorX} y1={anchorY} x2={decisionX} y2={labelY+12} stroke={picked?'#eaf2fc':'#8697aa'} strokeDasharray="3 4" opacity=".78"/>}
          {decisionX != null && observation && <g>
            <title>{`模型决策 ${observation.decision_date}，输入截至 ${observation.data_cutoff}；历史锚点 ${cand.anchor_date}。L1/L2 为未经校准的分数。`}</title>
            <line x1={decisionX} x2={decisionX} y1={labelY+18} y2={labelY+34} stroke={isHigh?'#eaa477':'#6aaefa'} strokeDasharray="2 3"/>
            <rect x={decisionX-52} y={labelY-16} width="104" height="48" rx="5" fill={picked?'#304963':'#19222d'} stroke={picked?'#fff':'#697a89'} strokeWidth={picked?1.7:.7}/>
            <text x={decisionX} y={labelY-4} textAnchor="middle" fill={isHigh?'#f3b68d':'#8abefa'} fontSize="9.5" fontWeight="bold">{isHigh?'▼ SELL':'▲ BUY'} · {observation.stage}</text>
            <text x={decisionX} y={labelY+8} textAnchor="middle" fill="#dbe5f2" fontSize="9">决策 {observation.decision_date.slice(5)}</text>
            <text x={decisionX} y={labelY+20} textAnchor="middle" fill="white" fontSize="9.4">L1 {fmt(observation.l1_score)} · L2 {fmt(observation.l2_score)}</text>
            {observation.joint_alert && <circle cx={decisionX+49} cy={labelY-13} r="4" fill="#9cd6c1"/>}
          </g>}
        </g>
      })}
    </svg>
    <p className={styles.hint}>▲ / ▼ 在候选锚点日标出结构位置；评分卡位于实际 <strong>决策日</strong>，虚线连接二者。L1/L2 为原始模型分数，不是成功概率。</p>
  </div>
}
