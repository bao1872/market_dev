import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '@/store/auth'
import { getDailyResearch, getStockResearch, type Candidate, type DailyRow } from '@/api/strategyCandidates'
import StrategyEvaluationPage from '@/features/strategy-evaluation/StrategyEvaluationPage'
import styles from './strategyResearchPage.module.scss'

type Tab = 'stock' | 'evaluation' | 'today' | 'management'
const VALID_TABS: Tab[] = ['stock', 'evaluation', 'today', 'management']
const isSymbol = (value: string) => /^(SH|SZ|BJ):[0-9]{6}$/.test(value)
const fmt = (n: number | null | undefined) => n == null ? '—' : n.toFixed(2)
const errorText = (e: unknown) => {
  const error = e as { response?: { status?: number } }
  if (error?.response?.status === 403) return '策略研究权限未激活，无法读取策略数据。'
  if (error?.response?.status === 503) return '还没有经过审计并发布的研究数据。页面不会使用模拟数据替代。'
  if (error?.response?.status === 404) return '尚未发布该股票或交易日的研究数据。'
  return '读取策略数据失败。请检查 API / 任务状态。'
}

function keyActivate(e: KeyboardEvent<SVGGElement>, fn: () => void) {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn() }
}

/** Pure geometry: marker coordinates refer to frozen event anchor bars. */
function StockChart({ bars, candidates, selectedId, stage, onSelect }: {
  bars: import('@/api/strategyCandidates').Bar[]; candidates: Candidate[]; selectedId: string | null;
  stage: string; onSelect: (id: string, s: string) => void
}) {
  const chosen = candidates.find(c => c.candidate_id === selectedId)
  const focusDate = chosen?.anchor_date
  const focus = bars.findIndex(b => b.date === focusDate)
  const start = focus >= 0 ? Math.max(0, focus - 22) : Math.max(0, bars.length - 66)
  const visible = bars.slice(start, start + 66)
  const width = Math.max(860, visible.length * 15 + 110)
  const height = 405
  const priceMin = Math.min(...visible.map(b => b.low), 0) === 0 ? Math.min(...visible.map(b => b.low)) : Math.min(...visible.map(b => b.low))
  const priceMax = Math.max(...visible.map(b => b.high))
  const spread = Math.max(.001, priceMax - priceMin)
  const y = (p: number) => 310 - 225 * (p - priceMin) / spread
  const xy = new Map(visible.map((b, index) => [b.date, 55 + index * 15]))
  const markers = candidates.filter(c => xy.has(c.anchor_date))
  if (!visible.length) return <div className={styles.empty}>该股票暂无已发布 K 线证据。</div>
  return <div className={styles.chartScroll}>
    <svg className={styles.chart} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="股票日线与同时显示的 H/L 候选及 L1、L2 分数">
      {[0, 1, 2, 3, 4].map(t => {
        const v = priceMin + spread * t / 4
        return <g key={t}><line x1="32" x2={width - 25} y1={y(v)} y2={y(v)} stroke="rgba(151,163,181,.18)" /><text x="3" y={y(v)+4} fontSize="10" fill="#9aa6b8">{v.toFixed(1)}</text></g>
      })}
      {visible.map((bar) => {
        const x = xy.get(bar.date)!
        const rising = bar.close >= bar.open
        return <g key={bar.date}><line x1={x} x2={x} y1={y(bar.high)} y2={y(bar.low)} stroke={rising ? '#39a995' : '#df806d'} strokeWidth="1.6" />
          <rect x={x-4} y={Math.min(y(bar.close),y(bar.open))} width="8" height={Math.max(1,Math.abs(y(bar.close)-y(bar.open)))} fill={rising ? '#39a995' : '#df806d'} />
          {((x-55)/15)%8===0 && <text x={x-18} y="363" fill="#8994a5" fontSize="10">{bar.date.slice(5)}</text>}</g>
      })}
      {markers.map((cand, index) => {
        const x = xy.get(cand.anchor_date)!
        const obs = cand.observations.find(o => o.stage === stage) ?? cand.observations[cand.observations.length - 1]
        const picked = cand.candidate_id === selectedId
        const hi = cand.kind === 'H'
        const base = y(cand.anchor_price)
        const ty = hi ? Math.max(45,base-38-(index%3)*12) : Math.min(332,base+46+(index%3)*12)
        return <g key={cand.candidate_id} role="button" tabIndex={0}
          onClick={() => onSelect(cand.candidate_id, obs?.stage ?? 'D1')}
          onKeyDown={e => keyActivate(e, () => onSelect(cand.candidate_id, obs?.stage ?? 'D1'))}
          aria-label={`${cand.kind==='L'?'买入':'卖出'}候选 ${cand.anchor_date} 第一层 ${fmt(obs?.l1_score)} 第二层 ${fmt(obs?.l2_score)}`}
          style={{ cursor:'pointer' }}>
          <path d={hi ? `M${x} ${base-12} l-7 -11 h14 Z` : `M${x} ${base+12} l-7 11 h14 Z`}
            fill={hi ? '#eaa477' : '#6aaefa'} stroke={picked?'white':'none'} strokeWidth="1.5" />
          <line x1={x} y1={base+(hi?-12:12)} x2={x} y2={ty+(hi?5:-5)} stroke={picked?'white':'#6d7685'} strokeDasharray="2 3" />
          <rect x={x-44} y={ty-17} width="88" height="37" rx="5" fill={picked?'#2b405d':'#19222d'} stroke={picked?'#b8d5fe':'#566273'} strokeWidth={picked?1.7:.7} />
          <text x={x} y={ty-6} textAnchor="middle" fill={hi?'#f3b68d':'#8abefa'} fontSize="9.5" fontWeight="bold">{hi?'▼ SELL':'▲ BUY'} · {obs?.stage ?? '—'}</text>
          <text x={x} y={ty+5} textAnchor="middle" fill="white" fontSize="9.6">L1 {fmt(obs?.l1_score)} · L2 {fmt(obs?.l2_score)}</text>
          {obs?.joint_alert && <text x={x} y={ty+15} textAnchor="middle" fill="#9cd6c1" fontSize="8">联合报警</text>}
        </g>
      })}
    </svg>
  </div>
}

function StockView({ urlSymbol, focusedId, stage, onLocation }: {
  urlSymbol: string; focusedId: string | null; stage: string;
  onLocation: (symbol: string, id: string | null, stage?: string) => void
}) {
  const [input, setInput] = useState(urlSymbol)
  useEffect(() => setInput(urlSymbol), [urlSymbol])
  const query = useQuery({ queryKey:['strategy-research','stock',urlSymbol], queryFn:()=>getStockResearch(urlSymbol), enabled:isSymbol(urlSymbol), staleTime:120000 })
  const data = query.data
  const selected = data?.candidates.find(c=>c.candidate_id===focusedId) ?? null
  const chosenObservation = selected?.observations.find(o=>o.stage===stage) ?? selected?.observations[selected.observations.length - 1]
  return <>
    <div className={styles.actions}><h2>个股候选轨迹</h2>
      <form onSubmit={e=>{e.preventDefault(); const s=input.trim().toUpperCase(); if(isSymbol(s)) onLocation(s,null)}}>
        <input aria-label="股票代码" value={input} onChange={e=>setInput(e.target.value)} placeholder="SH:600000" />
        <button type="submit">查看股票</button>
      </form></div>
    <p className={styles.hint}>买入（L）与卖出（H）候选同图；每个标记上直接保留第一层和第二层模型输出。均为未经校准的模型分数，非真实概率。</p>
    {query.isLoading && <div className={styles.empty}>读取已发布候选数据…</div>}
    {query.isError && <div className={styles.empty}>{errorText(query.error)}</div>}
    {data && <>
      <p className={styles.meta}>股票 {data.symbol} · 模型发布 {data.release_id} · 候选 {data.candidates.length} 个 · 行情 {data.bars.length} 条</p>
      {focusedId && !selected && <div className={styles.warning}>请求的候选事件在此版本不存在：{focusedId}。不会自动替换为其他事件。</div>}
      <StockChart bars={data.bars} candidates={data.candidates} selectedId={focusedId} stage={stage}
        onSelect={(id,s)=>onLocation(data.symbol,id,s)} />
      <div className={styles.details}>
        <section><h3>选中候选</h3>{selected ? <>
          <p>{selected.candidate_id} · {selected.kind==='L'?'L 买入观察':'H 卖出风险'} · 锚点 {selected.anchor_date}</p>
          <div className={styles.stageButtons}>{selected.observations.map(o=><button type="button" key={o.observation_id} className={chosenObservation?.stage===o.stage?styles.active:''} onClick={()=>onLocation(data.symbol, selected.candidate_id,o.stage)}>{o.stage} · {o.decision_date}</button>)}</div>
          {chosenObservation && <dl className={styles.scoregrid}>
            <div><dt>L1 模型分数</dt><dd>{fmt(chosenObservation.l1_score)} <small>门槛 {fmt(chosenObservation.l1_threshold)}</small></dd></div>
            <div><dt>L2 条件分数</dt><dd>{fmt(chosenObservation.l2_score)} <small>门槛 {fmt(chosenObservation.l2_threshold)}</small></dd></div>
            <div><dt>联合报警</dt><dd>{chosenObservation.joint_alert?'是':'否'}</dd></div>
            <div><dt>D1–D3 首次突破</dt><dd>{chosenObservation.first_breakout?'是':'否'}</dd></div>
            <div><dt>观测日期 / 输入截止</dt><dd>{chosenObservation.decision_date} / {chosenObservation.data_cutoff}</dd></div>
            <div><dt>已确认参考价</dt><dd>{chosenObservation.reference_price??'—'} <small>{chosenObservation.reference_confirmed_at??''}</small></dd></div>
          </dl>}
        </>:<p>点击图上候选点或下方表格中的一行。</p>}</section>
        <section><h3>所有候选 · H/L 同表</h3>
          <div className={styles.scroll}><table><thead><tr><th>锚点日</th><th>方向</th><th>候选 ID</th><th>最近 D</th><th>L1</th><th>L2</th><th>操作</th></tr></thead>
            <tbody>{data.candidates.map(c=>{const o=c.observations[c.observations.length-1];return <tr key={c.candidate_id}><td>{c.anchor_date}</td><td>{c.kind==='L'?'BUY':'SELL'}</td><td>{c.candidate_id}</td><td>{o?.stage??'—'}</td><td>{fmt(o?.l1_score)}</td><td>{fmt(o?.l2_score)}</td><td><button type="button" onClick={()=>onLocation(data.symbol,c.candidate_id,o?.stage)}>定位</button></td></tr>})}</tbody></table></div>
        </section>
      </div>
    </>}
  </>
}

function TodayView({ focus }: { focus:(row: DailyRow)=>void }) {
  const [kind,setKind] = useState<'ALL'|'H'|'L'>('ALL')
  const [type,setType] = useState<'ALL'|'ALERT'|'BREAKOUT'>('ALL')
  const [queryText,setQueryText] = useState('')
  const query = useQuery({queryKey:['strategy-research','daily'], queryFn:getDailyResearch, staleTime:120000})
  const data = query.data
  const rows = useMemo(()=>data?.items.filter(r=>(kind==='ALL'||r.kind===kind)&&(type==='ALL'||(type==='ALERT'?r.joint_alert:r.first_breakout))&&r.symbol.includes(queryText.toUpperCase()))??[],[data,kind,type,queryText])
  return <><div className={styles.actions}><h2>今日机会</h2><span className={styles.meta}>{data?`数据日 ${data.trade_date} · Release ${data.release_id}`:'等待已发布盘后批次'}</span></div>
    <p className={styles.hint}>所有买卖候选、模型报警及已发生的 D1–D3 首次突破都在同一张清单。点击事件将精准定位同一 Candidate ID。</p>
    <div className={styles.filters}><select aria-label="方向" value={kind} onChange={e=>setKind(e.target.value as typeof kind)}><option value="ALL">全部方向</option><option value="L">买入候选</option><option value="H">卖出候选</option></select>
      <select aria-label="信号" value={type} onChange={e=>setType(e.target.value as typeof type)}><option value="ALL">全部候选</option><option value="ALERT">联合报警</option><option value="BREAKOUT">D1–D3首次突破</option></select>
      <input aria-label="搜索股票" placeholder="搜索股票代码" value={queryText} onChange={e=>setQueryText(e.target.value)} /></div>
    {query.isLoading&&<div className={styles.empty}>读取已发布今日机会…</div>}{query.isError&&<div className={styles.empty}>{errorText(query.error)}</div>}
    {data&&<div className={styles.scroll}><table><thead><tr><th>股票</th><th>方向</th><th>阶段</th><th>L1</th><th>L2</th><th>联合报警</th><th>首次突破</th><th>候选身份</th><th>操作</th></tr></thead><tbody>
      {rows.map(row=><tr key={row.observation_id}><td>{row.symbol}</td><td>{row.kind==='L'?'BUY':'SELL'}</td><td>{row.stage}</td><td>{fmt(row.l1_score)}</td><td>{fmt(row.l2_score)}</td><td>{row.joint_alert?'是':'—'}</td><td>{row.first_breakout?'是':'—'}</td><td>{row.candidate_id}</td><td><button onClick={()=>focus(row)}>定位个股 →</button></td></tr>)}
      {!rows.length&&<tr><td colSpan={9}>该已发布批次无符合条件的事件。</td></tr>}
    </tbody></table></div>}
  </>
}

function AdminView() {
  const [train,setTrain] = useState({train0:'2023-01-01',train1:'2024-12-31',val0:'2025-01-01',val1:'2025-12-31',test0:'2026-01-01',test1:'2026-09-30'})
  const valid=train.train0<=train.train1&&train.train1<train.val0&&train.val0<=train.val1&&train.val1<train.test0&&train.test0<=train.test1
  return <><h2>模型管理</h2><p className={styles.hint}>管理员专属。手动重训与每日自动推理分离。当前交付只提供安全的预配置界面；算法制品和训练任务服务未通过审计前，训练/发布/回滚操作保持锁定。</p>
    <section className={styles.card}><h3>重新训练模型 · 事件决策日范围</h3><div className={styles.dategrid}>
      {([['train0','Train 起'],['train1','Train 止'],['val0','Validation 起'],['val1','Validation 止'],['test0','Test 起'],['test1','Test 止']] as const).map(([key,label])=><label key={key}>{label}<input type="date" value={train[key]} onChange={e=>setTrain(p=>({...p,[key]:e.target.value}))} /></label>)}
    </div><p className={valid?styles.good:styles.warning}>{valid?'区间日期顺序合法（尚未做样本成熟/PIT预检）':'日期区间重叠或倒置，不符合独立 Train/VAL/TEST 合同'}</p>
      <button type="button" disabled title="需先核验因果引擎、模型权重与后验标签，接入后台训练服务">重新训练（等待模型制品审计）</button></section>
    <section className={styles.card}><h3>任务与版本</h3><p>每日盘后预测、候选模型发布、回滚均需要后端任务与不可变模型发布指针；本阶段未连接这些管理 API。不能用前端状态模拟生产发布。</p></section></>
}

export default function StrategyResearchPage() {
  const [params,setParams] = useSearchParams()
  const user = useAuthStore(s=>s.user)
  const admin=!!user?.is_admin
  const desired=params.get('tab')
  const tab:Tab = VALID_TABS.includes(desired as Tab)&& (desired!=='management'||admin) ? desired as Tab : 'stock'
  const symbol=params.get('symbol') ?? 'SH:600000'
  const focusedId=params.get('candidate_id')
  const stage=params.get('stage') ?? 'D1'
  function navigateTab(t:Tab){const p=new URLSearchParams(params);p.set('tab',t);setParams(p)}
  function locate(stock:string,id:string|null,s?:string){const p=new URLSearchParams(params);p.set('tab','stock');p.set('symbol',stock);if(id)p.set('candidate_id',id);else p.delete('candidate_id');if(s)p.set('stage',s);else p.delete('stage');setParams(p)}
  return <main className={styles.root}>
    <header className={styles.header}><div><small>盘迹 / STRATEGY RESEARCH</small><h1>策略研究中心</h1><p>同一候选事件贯穿个股研究、历史评价与当日机会</p></div><span className={styles.status}>真实数据模式 · 未发布则显示空态</span></header>
    <nav className={styles.tabs} aria-label="策略子页面">
      {([['stock','个股'],['evaluation','历史评价'],['today','今日机会'],['management','模型管理']] as const).filter(([id])=>id!=='management'||admin).map(([id,label])=><button type="button" key={id} aria-current={tab===id?'page':undefined} className={tab===id?styles.active:''} onClick={()=>navigateTab(id)}>{label}</button>)}
    </nav>
    <div className={styles.content}>
      {tab==='stock'&&<StockView urlSymbol={symbol} focusedId={focusedId} stage={stage} onLocation={locate} />}
      {tab==='evaluation'&&<StrategyEvaluationPage />}
      {tab==='today'&&<TodayView focus={r=>locate(r.symbol,r.candidate_id,r.stage)} />}
      {tab==='management'&&admin&&<AdminView />}
    </div>
  </main>
}
