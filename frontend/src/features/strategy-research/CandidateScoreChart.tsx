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
import type { Bar, Candidate, StrategyStructure, TVZigZagOverlay } from '@/api/strategyCandidates'
import { ZIGZAG_FIXED } from '@/components/zigzag'
import {
  allStageObservations, assertUniqueSortedBars,
  candidateLifecycleAsOf, lifecycleLabel, stageObservation,
} from './candidateTimeline'
import styles from './strategyResearchPage.module.scss'
import { chartWindow, MIN_VISIBLE_BARS } from './chartWindow'
import {SCORE_FAMILIES,scorePoints,scoreValue} from './scorePoints'
import {scorePrimitive} from './scorePrimitive'

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
  bars, candidates, selectedId, stage, onSelect, structure, tvOverlay,
}: {
  bars: Bar[]
  structure?: StrategyStructure
  tvOverlay?: TVZigZagOverlay
  candidates: Candidate[]
  selectedId: string | null
  stage: string
  onSelect: (candidateId: string, stage: string) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const scoreHost = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const viewport = useRef<{from:number;to:number}|null>(null)
  const series = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const callbacks = useRef({ candidates, onSelect })
  callbacks.current = { candidates, onSelect }
  const [hoverDate, setHoverDate] = useState<string | null>(null)
  const [range, setRange] = useState('90根')
  const [visibleBars,setVisibleBars]=useState(0)
  const [rawScores,setRawScores]=useState(false)
  const [hiddenFamilies,setHiddenFamilies]=useState<string[]>([])
  const [scoreStage,setScoreStage]=useState('ALL')
  const [pickedDate,setPickedDate]=useState<string|null>(null)
  const [selectedScoreId,setSelectedScoreId]=useState<string|null>(null)
  const points=useMemo(()=>scorePoints(candidates).filter(p=>!hiddenFamilies.includes(p.family)&&(scoreStage==='ALL'||p.stage===scoreStage)),[candidates,hiddenFamilies,scoreStage])
  const [showH, setShowH] = useState(true)
  const [showL, setShowL] = useState(true)

  const selected = candidates.find(c => c.candidate_id === selectedId)
  const observations = useMemo(
    () => selected ? allStageObservations(selected) : [],
    [selected],
  )
  const current = selected ? stageObservation(selected, stage) : undefined
  const lifecycle = selected && current
    ? candidateLifecycleAsOf(bars, selected, current)
    : undefined
  const selectedPoint=points.find(p=>p.id===selectedScoreId&&p.candidateId===selectedId&&p.stage===stage)
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
      width: Math.max(host.clientWidth, 1),
      height: 490,
      layout: { background: { type: ColorType.Solid, color: '#141d29' }, textColor: '#b1becf' },
      grid: { vertLines: { color: '#242f3c' }, horzLines: { color: '#293643' } },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { borderColor: '#435269', rightOffset: 6, barSpacing: 11, timeVisible: false },
      rightPriceScale: { borderColor: '#435269', minimumWidth: 65 },
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
    if (tvOverlay) {
      const offset = tvOverlay.barCount-bars.length
      const legs = [...tvOverlay.segments.map(leg => ({leg,dashed:false})),
        ...(tvOverlay.projected ? [{leg:tvOverlay.projected,dashed:true}] : [])]
      for (const {leg,dashed} of legs) {
        if (leg.to.index < offset || leg.from.index === leg.to.index) continue
        const fromIndex = Math.max(offset,leg.from.index)
        const startPrice = leg.from.price+(leg.to.price-leg.from.price)*(fromIndex-leg.from.index)/(leg.to.index-leg.from.index)
        const line = c.addLineSeries({color:ZIGZAG_FIXED.lineColor,lineWidth:2,lineStyle:dashed?2:0,
          lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,autoscaleInfoProvider:()=>null})
        // Clip geometry at the displayed window; this point is not a new pivot.
        line.setData([{time:toTime(bars[fromIndex-offset].date),value:startPrice},
          {time:toTime(leg.to.date),value:leg.to.price}])
      }
    } else if (structure && structure.source_bar_hash && structure.confirmed_pivots.length >= 2) {
      // Query-as-of historical structure, NOT decision-day advance-knowledge.
      const line = c.addLineSeries({
        color:'#e3be71',lineWidth:2,lastValueVisible:false,
        priceLineVisible:false,crosshairMarkerVisible:false,
      })
      line.setData(structure.confirmed_pivots.map(p => ({
        time:toTime(p.anchor_date),value:p.price,
      })))
    }
    const laneCharts:IChartApi[]=[]
    if(scoreHost.current&&candidates.length){
      const sc=createChart(scoreHost.current,{width:Math.max(host.clientWidth,1),height:210,
        layout:{background:{type:ColorType.Solid,color:'#141d29'},textColor:'#b1becf'},
        grid:{vertLines:{color:'#242f3c'},horzLines:{color:'#293643'}},
        rightPriceScale:{minimumWidth:65},timeScale:{rightOffset:6,barSpacing:11},crosshair:{mode:CrosshairMode.Normal}})
      const axis=sc.addLineSeries({lineVisible:false,pointMarkersVisible:false,lastValueVisible:false,priceLineVisible:false,
        autoscaleInfoProvider:()=>({priceRange:rawScores?{minValue:0,maxValue:1}:{minValue:-1,maxValue:1}})})
      axis.setData(bars.map(b=>({time:toTime(b.date),value:rawScores?.5:0})))
      if(!rawScores)axis.createPriceLine({price:0,color:'#a8b4c6',lineWidth:1,lineStyle:0,axisLabelVisible:true,title:'阈值基准'})
      axis.attachPrimitive(scorePrimitive(points,rawScores))
      let crossSync=false
      const syncCross=(time:Time|undefined,fromScore:boolean)=>{
        if(crossSync)return
        crossSync=true
        try{
          setHoverDate(time?dateFromTime(time):null)
          const bar=time?bars.find(b=>b.date===dateFromTime(time)):undefined
          if(!time||!bar){c.clearCrosshairPosition();sc.clearCrosshairPosition();return}
          if(fromScore)c.setCrosshairPosition(bar.close,time,candle)
          else sc.setCrosshairPosition(scoreValue(points.find(p=>p.date===bar.date)??{score:0,threshold:0},rawScores),time,axis)
        }finally{crossSync=false}
      }
      sc.subscribeCrosshairMove(p=>syncCross(p.time,true))
      c.subscribeCrosshairMove(p=>syncCross(p.time,false))
      sc.subscribeClick(p=>{
        if(!p.time||!p.point)return
        const date=dateFromTime(p.time)
        const hits=points.filter(v=>v.date===date).filter(v=>{
          const y=axis.priceToCoordinate(scoreValue(v,rawScores));return y!=null&&Math.abs(y-p.point!.y)<=9
        })
        setPickedDate(date)
        if(hits.length===1){setSelectedScoreId(hits[0].id);callbacks.current.onSelect(hits[0].candidateId,hits[0].stage)}
        // Overlapping glyphs stay selectable in the same-day observation list.
      })
      laneCharts.push(sc)
    }
    let syncing = false
    const allCharts = [c, ...laneCharts]
    for (const origin of allCharts) origin.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (!range || syncing) return
      setVisibleBars(Math.max(0,Math.round(Math.min(bars.length-1,range.to)-Math.max(0,range.from)+1)))
      syncing = true
      try { for (const target of allCharts) if (target !== origin) {
        const previous = target.timeScale().getVisibleLogicalRange()
        if (!previous || Math.abs(previous.from - range.from) > .001 || Math.abs(previous.to - range.to) > .001)
          target.timeScale().setVisibleLogicalRange(range)
      } }
      finally { syncing = false }
    })
    const initial = viewport.current ?? chartWindow(bars.length)
    if (initial) c.timeScale().setVisibleLogicalRange(initial)
    chart.current = c
    series.current = candle

    const resize = new ResizeObserver(([entry]) => {
      const logical=c.timeScale().getVisibleLogicalRange()
      for (const target of allCharts) target.applyOptions({ width: Math.max(Math.floor(entry.contentRect.width), 1) })
      if(logical)for(const target of allCharts)target.timeScale().setVisibleLogicalRange(logical)
    })
    resize.observe(host)

    c.subscribeCrosshairMove(param => setHoverDate(param.time ? dateFromTime(param.time) : null))
    c.subscribeClick(param => {
      if (!param.time) return
      const date = dateFromTime(param.time)
      const matches = callbacks.current.candidates.filter(x => x.anchor_date === date)
      setPickedDate(date)
      if (matches.length === 1) {
        const item = matches[0]
        callbacks.current.onSelect(item.candidate_id, item.observations[0]?.stage ?? 'D1')
      }
    })
    return () => {
      resize.disconnect()
      for (const lane of laneCharts) lane.remove()
      viewport.current=c.timeScale().getVisibleLogicalRange()
      c.remove()
      chart.current = null
      series.current = null
    }
  }, [bars, candidates, validationError, structure, tvOverlay, points, rawScores])

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
    if (structure) {
      for (const node of structure.early_candidates) {
        if ((node.kind === 'H' && !showH) || (node.kind === 'L' && !showL)) continue
        markers.push({time:toTime(node.anchor_date),
          position:node.kind==='L'?'belowBar':'aboveBar',
          shape:node.kind==='L'?'circle':'square',
          color:'#93a1b4',text:node.kind==='L'?'L?':'H?',size:1})
      }
    }
    markers.sort((a,b) => dateFromTime(a.time).localeCompare(dateFromTime(b.time)))
    series.current.setMarkers(markers)
  }, [bars, candidates, selectedId, stage, showH, showL, validationError, structure])

  useEffect(() => {
    const c = chart.current
    if (!c || !bars.length) return
    if (selected && current) {
      const idx = bars.findIndex(b => b.date === current.decision_date)
      if (idx >= 0) {
        c.timeScale().setVisibleLogicalRange({
          ...chartWindow(bars.length, idx)!,
        })
      }
    }
  }, [bars, selected, current]) // selection focus only; mouse zoom is otherwise preserved

  const setVisible = (label: string, size: number | null) => {
    setRange(label)
    const c = chart.current
    if (!c || !bars.length) return
    if (size === null) c.timeScale().fitContent()
    else { const next = chartWindow(bars.length, undefined, size); if (next) c.timeScale().setVisibleLogicalRange(next) }
  }

  if (!bars.length) return <div className={styles.empty}>该股票暂无已发布的K线证据。</div>
  if (validationError) return <div className={styles.warning}>图表已拒绝不完整K线：{validationError}</div>

  return <div className="strategy-full-chart">
    <div className={styles.strategyStockMain}>
      <div className={styles.strategyChartToolbar}>
        <div className={styles.strategyRangeButtons} aria-label="K线范围">
          {([['90根',90],['180根',180],['1年',250],['全部',null]] as const)
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
        <span aria-label="实际可见K线数量">当前视窗 {visibleBars} 根</span>｜当前实际数据含 {bars.length} 根K线（{bars[0].date}～{bars[bars.length-1].date}）。
        {bars.length < MIN_VISIBLE_BARS ? `当前版本不足90根，仅展示真实的${bars.length}根；需发布更完整行情区间。` : '初始视窗与事件定位均至少保留90根真实日线；可拖动查看更早数据。'}
      </p>
      <div className={styles.strategyTradingChart} ref={ref} role="img"
        aria-label="A股历史日K，红涨绿跌，低点和高点均为早期研究候选" />
      {candidates.length>0&&<section className="model-score-tracks" aria-label="统一模型评分副图">
        <div className="control-row" style={{padding:'8px 0'}}>
          <strong>模型评分</strong><button type="button" className="btn tiny" aria-pressed={rawScores} onClick={()=>setRawScores(v=>!v)}>{rawScores?'原始分数 0–1':'阈值差 · 零线为门限'}</button>
          <select className="select" aria-label="评分阶段" value={scoreStage} onChange={e=>setScoreStage(e.target.value)}><option value="ALL">全部阶段</option>{['D0','D1','D2','D3'].map(v=><option key={v}>{v}</option>)}</select>
          <select className="select" aria-label="评分观测日期" value={pickedDate??''} onChange={e=>setPickedDate(e.target.value||null)}><option value="">跟随光标</option>{[...new Set(points.map(p=>p.date))].sort().map(day=><option key={day}>{day}</option>)}</select>
          {SCORE_FAMILIES.map(f=><button key={f.id} type="button" className="btn tiny" style={{color:f.color,opacity:hiddenFamilies.includes(f.id)?.45:1}} aria-pressed={!hiddenFamilies.includes(f.id)} onClick={()=>setHiddenFamilies(v=>v.includes(f.id)?v.filter(x=>x!==f.id):[...v,f.id])}>{f.label}</button>)}
        </div>
        <p className={styles.hint}>■ D0　● D1　◆ D2　▲ D3。阈值差＝原始分数－对应阈值，不是跨模型可比较的成功概率。重合点点击后在下方精确选择。</p>
        <div ref={scoreHost} role="img" aria-label="统一模型评分，六类颜色与阶段形状，时间轴与主K线联动" />
      </section>}
      {currentBar && <p className={styles.strategyCrosshairBar}>
        {currentBar.date}　开 {currentBar.open.toFixed(3)}　高 {currentBar.high.toFixed(3)}
       　低 {currentBar.low.toFixed(3)}　收 {currentBar.close.toFixed(3)}
      </p>}
      {(pickedDate||hoverDate) && candidates.length > 0 && <div className="score-date-detail" aria-live="polite">
        <strong>{pickedDate||hoverDate} · 同日模型观测</strong>{pickedDate&&<button type='button' className='btn tiny' onClick={()=>setPickedDate(null)}>跟随光标</button>}
        {pickedDate&&<div className="control-row">{candidates.filter(c=>c.anchor_date===pickedDate).flatMap(c=>c.observations.map(o=><button type="button" className="btn tiny" key={o.observation_id} onClick={()=>onSelect(c.candidate_id,o.stage)}>锚点 {c.anchor_date} {c.kind} → 评分 {o.decision_date} {o.stage}</button>))}</div>}
        <div className="control-row">{points.filter(p=>p.date===(pickedDate||hoverDate)).map(p=><button type="button" className="btn tiny" style={{color:p.color}} key={p.id} onClick={()=>{setSelectedScoreId(p.id);onSelect(p.candidateId,p.stage)}}>
          {p.family} {p.stage} · 锚点 {p.anchorDate} · 评分 {p.date} · 原值 {fmt(p.score)} / 阈值 {fmt(p.threshold)} / 差 {fmt(scoreValue(p,false))}
        </button>)}</div>
      </div>}
      <p className={styles.hint}>
        {structure ? <>
          灰色 H?/L? 是未经模型评分的早期候选，标记位于锚点日；首次可见日期见候选表。未确认极值不绘制。
        </> : <>
          L/H 标记位于候选锚点日，D1/D2/D3 分数归属于决策日；灰色 L/H 表示在对应模型观测日锚点已被突破。
        </>}
      </p>
    </div>
    <section className="score-observation-strip" aria-label="选中模型观测">
      {!selected || !current ? <p className={styles.hint}>移动十字线查看同日全部模型输出，点击主图或副图定位观测；完整 K 线始终保持在上方。</p> : <>
        <div className="control-row"><strong>{selected.kind === 'L' ? 'L 低点候选' : 'H 高点候选'} · {current.decision_date}</strong>
          {observations.map(o => <button type="button" key={o.observation_id} className={`btn tiny ${o.stage === current.stage ? 'primary' : ''}`}
            aria-pressed={o.stage === current.stage} onClick={() => onSelect(selected.candidate_id,o.stage)}>{o.stage}</button>)}</div>
        <p>{current.stage === 'D0' ? `D0 极值倾向 ${fmt(current.d0_score)} / 阈值 ${fmt(current.d0_threshold)}` :
          `L1 ${fmt(current.l1_score)} / 阈值 ${fmt(current.l1_threshold)} · L2 条件分数 ${fmt(current.l2_score)} / 阈值 ${fmt(current.l2_threshold)}`}
          {selectedPoint && ` · 选中 ${selectedPoint.family} · 阈值差 ${scoreValue(selectedPoint,false).toFixed(3)}`}
          {current.first_breakout ? ' · 本阶段首次突破参考点' : ''}</p>
        <div className="hint">{current.decision_date === bars[bars.length-1]?.date && current.scoring_status !== 'CANDIDATE_INACTIVE' ? '当日评分' : '历史观测，不沿用为当日评分'} · 锚点 {selected.anchor_date} · 评分 {current.decision_date} · 当前状态 {lifecycleLabel(candidateLifecycleAsOf(bars,selected,{...current,decision_date:bars[bars.length-1].date}))} · 当时状态 {lifecycle ? lifecycleLabel(lifecycle) : '状态待确认'} · 观测 {current.observation_id}；L2 条件资格未实时验证。</div>
      </>}
      {tvOverlay ? <p className={styles.hint}>蓝线直接复用 TV ZigZag 代码；虚线存在时它就是最后一段，否则取最后实线。实线末端仍可能变化，不等同完全确认；方向不由回归或当前 K 线涨跌决定。</p> : structure && <p className={styles.hint}>黄线为截至 {structure.as_of} 已确认研究结构；缺少 TV 末段时不以此替代当前方向。</p>}
    </section>
  </div>
}
