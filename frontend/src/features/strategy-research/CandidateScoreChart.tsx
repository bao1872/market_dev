/**
 * Strategy-specific adapter over the project's EXISTING lightweight-charts 4.2
 * dependency. No custom OHLC renderer; no invented ZigZag final pivots.
 * Candles always render before markers and are never covered by score cards.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ColorType, CrosshairMode, createChart,
  type IChartApi, type ISeriesApi, type Time, type SeriesMarker,
} from 'lightweight-charts'
import type { Bar, Candidate } from '@/api/strategyCandidates'
import {
  allStageObservations, assertUniqueSortedBars,
  candidateLifecycleAsOf, lifecycleLabel, stageObservation,
} from './candidateTimeline'
import styles from './strategyResearchPage.module.scss'

const UP = '#ef5350'       // A股红涨
const DOWN = '#26a69a'     // A股绿跌
const BLUE = '#67b5ff'     // L 候选标记颜色（非K线颜色）
const ORANGE = '#f5ae72'   // H 候选标记颜色
const GREY = '#8994a5'
const fmt = (x: number | null | undefined) => x == null ? '—' : x.toFixed(3)
const toTime = (day: string) => day as Time
function dateFromTime(t: Time): string {
  if (typeof t === 'string') return t
  if (typeof t === 'number') return new Date(t * 1000).toISOString().slice(0,10)
  return `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`
}

export default function CandidateScoreChart({
  bars, candidates, selectedId, stage, onSelect,
}: {
  bars: Bar[]
  candidates: Candidate[]
  selectedId: string | null
  stage: string
  onSelect: (candidateId: string, stage: string) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const callbacks = useRef({ candidates, onSelect })
  callbacks.current = { candidates, onSelect }
  const [hoverDate, setHoverDate] = useState<string | null>(null)
  const [range, setRange] = useState('全部')
  const [showH, setShowH] = useState(true)
  const [showL, setShowL] = useState(true)

  const selected = candidates.find(c => c.candidate_id === selectedId)
  const observations = useMemo(
    () => selected ? allStageObservations(selected) : [],
    [selected],
  )
  const current = selected ? (stageObservation(selected, stage) ?? observations[0]) : undefined
  const lifecycle = selected && current
    ? candidateLifecycleAsOf(bars, selected, current)
    : undefined
  const currentBar = hoverDate ? bars.find(b => b.date === hoverDate) : undefined

  const validationError = useMemo(() => {
    try {
      assertUniqueSortedBars(bars)
      return null
    } catch (e) {
      return e instanceof Error ? e.message : 'INVALID_BARS'
    }
  }, [bars])

  useEffect(() => {
    if (!ref.current || !bars.length || validationError) return
    const host = ref.current
    const c = createChart(host, {
      width: Math.max(host.clientWidth, 320),
      height: 490,
      layout: { background: { type: ColorType.Solid, color: '#141d29' }, textColor: '#b1becf' },
      grid: { vertLines: { color: '#242f3c' }, horzLines: { color: '#293643' } },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { borderColor: '#435269', rightOffset: 6, barSpacing: 11, timeVisible: false },
      rightPriceScale: { borderColor: '#435269' },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
    })
    const candle = c.addCandlestickSeries({
      upColor: UP, downColor: DOWN,
      borderUpColor: UP, borderDownColor: DOWN,
      wickUpColor: UP, wickDownColor: DOWN,
    })
    candle.setData(bars.map(b => ({
      time: toTime(b.date), open: b.open, high: b.high,
      low: b.low, close: b.close,
    })))
    c.timeScale().fitContent()
    chart.current = c
    series.current = candle

    const resize = new ResizeObserver(([entry]) => {
      c.applyOptions({ width: Math.max(Math.floor(entry.contentRect.width), 320) })
    })
    resize.observe(host)

    c.subscribeCrosshairMove(param => setHoverDate(param.time ? dateFromTime(param.time) : null))
    c.subscribeClick(param => {
      if (!param.time) return
      const date = dateFromTime(param.time)
      const matches = callbacks.current.candidates.filter(x => x.anchor_date === date)
      if (matches.length) {
        const item = matches[0]
        callbacks.current.onSelect(item.candidate_id, item.observations[0]?.stage ?? 'D1')
      }
    })
    return () => {
      resize.disconnect()
      c.remove()
      chart.current = null
      series.current = null
    }
  }, [bars, validationError])

  useEffect(() => {
    if (!series.current || validationError) return
    const markers: SeriesMarker<Time>[] = []
    for (const candidate of candidates) {
      if ((candidate.kind === 'H' && !showH) || (candidate.kind === 'L' && !showL)) continue
      if (!bars.some(b => b.date === candidate.anchor_date)) continue
      const obs = selectedId === candidate.candidate_id
        ? (stageObservation(candidate, stage) ?? candidate.observations[0])
        : candidate.observations[candidate.observations.length - 1]
      const state = obs ? candidateLifecycleAsOf(bars, candidate, obs) : null
      const breached = state?.state === 'ANCHOR_BREACHED'
      const selected = selectedId === candidate.candidate_id
      markers.push({
        time: toTime(candidate.anchor_date),
        position: candidate.kind === 'L' ? 'belowBar' : 'aboveBar',
        shape: candidate.kind === 'L' ? 'arrowUp' : 'arrowDown',
        color: breached ? GREY : candidate.kind === 'L' ? BLUE : ORANGE,
        text: selected ? `${candidate.kind} ★` : candidate.kind,
        size: selected ? 2 : 1,
      })
    }
    markers.sort((a,b) => dateFromTime(a.time).localeCompare(dateFromTime(b.time)))
    series.current.setMarkers(markers)
  }, [bars, candidates, selectedId, stage, showH, showL, validationError])

  useEffect(() => {
    const c = chart.current
    if (!c || !bars.length) return
    if (selected && current) {
      const idx = bars.findIndex(b => b.date === current.decision_date)
      if (idx >= 0) {
        c.timeScale().setVisibleLogicalRange({
          from: Math.max(0, idx - 32), to: Math.min(bars.length - 1, idx + 32),
        })
      }
    }
  }, [bars, selectedId, stage]) // selection focus only; mouse zoom is otherwise preserved

  const setVisible = (label: string, size: number | null) => {
    setRange(label)
    const c = chart.current
    if (!c || !bars.length) return
    if (size === null) c.timeScale().fitContent()
    else c.timeScale().setVisibleLogicalRange({
      from: Math.max(0, bars.length - size),
      to: bars.length - 1 + 5,
    })
  }

  if (!bars.length) return <div className={styles.empty}>该股票暂无已发布的K线证据。</div>
  if (validationError) return <div className={styles.warning}>图表已拒绝不完整K线：{validationError}</div>

  return <div className={styles.strategyStockGrid}>
    <div className={styles.strategyStockMain}>
      <div className={styles.strategyChartToolbar}>
        <div className={styles.strategyRangeButtons} aria-label="K线范围">
          {([['1月',22],['3月',66],['6月',132],['1年',250],['全部',null]] as const)
            .map(([label,num]) =>
              <button key={label} type="button" aria-pressed={range===label}
                onClick={()=>setVisible(label,num)}>
                {label}
              </button>)}
        </div>
        <label><input type="checkbox" checked={showL} onChange={e=>setShowL(e.target.checked)} />低点候选 L</label>
        <label><input type="checkbox" checked={showH} onChange={e=>setShowH(e.target.checked)} />高点候选 H</label>
      </div>
      <p className={styles.hint}>
        A股红涨绿跌｜滚轮缩放、鼠标拖动、十字线读日期及OHLC｜
        当前真实快照仅含 {bars.length} 根K线（{bars[0].date}～{bars[bars.length-1].date}）。
        选择“1年”也不会伪造缺失历史。
      </p>
      <div className={styles.strategyTradingChart} ref={ref} role="img"
        aria-label="A股历史日K，红涨绿跌，低点和高点均为早期研究候选" />
      {currentBar && <p className={styles.strategyCrosshairBar}>
        {currentBar.date}　开 {currentBar.open.toFixed(3)}　高 {currentBar.high.toFixed(3)}
       　低 {currentBar.low.toFixed(3)}　收 {currentBar.close.toFixed(3)}
      </p>}
      <p className={styles.hint}>
        L/H 标记固定在<strong>候选锚点日</strong>，模型分数归属于D1/D2/D3<strong>决策日</strong>；
        灰色表示截至相应观测日锚点已被突破。没有权威确认ZigZag数据时，不绘制伪造的确认折线。
      </p>
    </div>
    <aside className={styles.strategyCandidateInspector} aria-label="候选完整阶段研究">
      <h3>候选研究 · D1 / D2 / D3</h3>
      {!selected ? <p className={styles.hint}>点击K线上 L/H 候选标记所在日期，查看同一候选的完整三阶段预测。</p> :
      <>
        <div className={styles.strategyCandidateIdentity}>
          <strong>{selected.kind === 'L' ? '低点早期候选 L' : '高点早期候选 H'}</strong>
          <small>锚点 {selected.anchor_date} · 首次可见 {selected.visible_at}</small>
          <small>锚点价 {fmt(selected.anchor_price)} · 非已确认ZigZag拐点</small>
        </div>
        {observations.map(obs => {
          const state = candidateLifecycleAsOf(bars, selected, obs)
          const active = current?.observation_id === obs.observation_id
          return <button className={styles.strategyStageCard} type="button"
            aria-pressed={active} key={obs.observation_id}
            onClick={()=>onSelect(selected.candidate_id,obs.stage)}>
            <span><b>{obs.stage}</b> · 决策 {obs.decision_date}</span>
            <span>L1 {fmt(obs.l1_score)} / 门槛 {fmt(obs.l1_threshold)}</span>
            <span>L2 {fmt(obs.l2_score)} / 门槛 {fmt(obs.l2_threshold)}</span>
            <span>联合报警：{obs.joint_alert?'是':'否'} · 首次突破：{obs.first_breakout?'是':'否'}</span>
            <span>{lifecycleLabel(state)}</span>
          </button>
        })}
        {lifecycle && <p className={styles.strategyLifecycle}>
          当前阶段：{lifecycleLabel(lifecycle)}
        </p>}
        <p className={styles.hint}>L1/L2为未经校准的分数，不是成功概率。候选锚点失效属于价格事实，不替代冻结模型的L1/L2研究标签。</p>
      </>}
      <p className={styles.warning}>
        本批次未提供真实Train/VAL/Test归属及截至当日的确认ZigZag折线，故不声称样本外表现或已确认拐点。
      </p>
    </aside>
  </div>
}
