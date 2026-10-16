import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '@/store/auth'
import { getDailyResearch, getStockResearch, type DailyRow } from '@/api/strategyCandidates'
import StrategyEvaluationPage from '@/features/strategy-evaluation/StrategyEvaluationPage'
import CandidateScoreChart from './CandidateScoreChart'
import StrategyStockSearch from './StrategyStockSearch'
import ModelManagementView from './ModelManagementView'
import styles from './strategyResearchPage.module.scss'

type Tab = 'stock' | 'evaluation' | 'today' | 'management'
type LinkContext = { releaseId: string; observationId: string; decisionDate: string }
const VALID_TABS: Tab[] = ['stock', 'evaluation', 'today', 'management']
const isSymbol = (value: string) => /^(SH|SZ|BJ):[0-9]{6}$/.test(value)
const fmt = (n: number | null | undefined) => n == null ? '—' : n.toFixed(2)
const errorText = (e: unknown) => {
  const error = e as { response?: { status?: number; data?: { detail?: string } } }
  if (error?.response?.status === 403) return '策略研究权限未激活，无法读取策略数据。'
  if (error?.response?.status === 503) return error?.response?.data?.detail ?? '最新交易日预测尚未发布，页面不使用旧信号冒充今日机会。'
  if (error?.response?.status === 409) return '候选模型发布版本不匹配，已阻止用其他版本的分数替代。'
  if (error?.response?.status === 404) return '尚未发布该股票或交易日的研究数据。'
  return '读取策略数据失败。请检查 API / 任务状态。'
}


function StockView({ urlSymbol, focusedId, stage, releaseId, observationId, decisionDate, onLocation }: {
  urlSymbol: string; focusedId: string | null; stage: string;
  releaseId: string | null; observationId: string | null; decisionDate: string | null;
  onLocation: (symbol: string, id: string | null, stage?: string, context?: LinkContext) => void
}) {
  const query = useQuery({ queryKey:['strategy-research','stock',urlSymbol,releaseId], queryFn:()=>getStockResearch(urlSymbol, releaseId??undefined), enabled:isSymbol(urlSymbol), staleTime:120000 })
  const data = query.data
  const selected = data?.candidates.find(c=>c.candidate_id===focusedId) ?? null
  const candidateObservation = selected?.observations.find(o=>o.stage===stage)
  const linkMismatch = Boolean(focusedId && (
    !selected || !candidateObservation ||
    (observationId && observationId !== candidateObservation.observation_id) ||
    (decisionDate && decisionDate !== candidateObservation.decision_date)
  ))
  const chosenObservation = linkMismatch ? undefined : candidateObservation
  const validFocus = linkMismatch ? null : focusedId
  return <>
    <div className={styles.actions}><h2>个股候选轨迹</h2>
      <StrategyStockSearch symbol={urlSymbol} onChoose={s=>onLocation(s,null)} /></div>
    <p className={styles.hint}>低点 L 和高点 H 是早期候选，不等于交易建议；点击候选查看 D1–D3，并区分历史锚点是否已被突破。</p>
    {query.isLoading && <div className={styles.empty}>读取已发布候选数据…</div>}
    {query.isError && <div className={styles.empty}>{errorText(query.error)}</div>}
    {data && <>
      <p className={styles.meta}>股票 {data.symbol} · 模型发布 {data.release_id} · 候选 {data.candidates.length} 个 · 行情 {data.bars.length} 条</p>
      {linkMismatch && <div className={styles.warning}>深链候选 ID、决策阶段、观测 ID 或决策日与当前发布内容不匹配。已禁止错误定位，请核对模型版本与事件来源。</div>}
      <CandidateScoreChart bars={data.bars} candidates={data.candidates} selectedId={validFocus} stage={stage}
        onSelect={(id,s)=>{
          const candidate=data.candidates.find(c=>c.candidate_id===id)
          const obs=candidate?.observations.find(o=>o.stage===s)
          onLocation(data.symbol,id,s,obs?{releaseId:data.release_id,observationId:obs.observation_id,decisionDate:obs.decision_date}:undefined)
        }} />
      <div className={styles.details}>
        <section><h3>选中候选</h3>{selected && !linkMismatch ? <>
          <p>{selected.candidate_id} · {selected.kind==='L'?'L 买入观察':'H 卖出风险'} · 锚点 {selected.anchor_date}</p>
          <div className={styles.stageButtons}>{selected.observations.map(o=><button type="button" key={o.observation_id} className={chosenObservation?.stage===o.stage?styles.active:''} onClick={()=>onLocation(data.symbol, selected.candidate_id,o.stage, {releaseId:data.release_id, observationId:o.observation_id, decisionDate:o.decision_date})}>{o.stage} · {o.decision_date}</button>)}</div>
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
            <tbody>{data.candidates.map(c=>{const o=c.observations[c.observations.length-1];return <tr key={c.candidate_id}><td>{c.anchor_date}</td><td>{c.kind==='L'?'BUY':'SELL'}</td><td>{c.candidate_id}</td><td>{o?.stage??'—'}</td><td>{fmt(o?.l1_score)}</td><td>{fmt(o?.l2_score)}</td><td><button type="button" onClick={()=>onLocation(data.symbol,c.candidate_id,o?.stage, o?{releaseId:data.release_id, observationId:o.observation_id, decisionDate:o.decision_date}:undefined)}>定位</button></td></tr>})}</tbody></table></div>
        </section>
      </div>
    </>}
  </>
}

function TodayView({ focus }: { focus:(row: DailyRow,releaseId:string,decisionDate:string)=>void }) {
  const [kind,setKind] = useState<'ALL'|'H'|'L'>('ALL')
  const [type,setType] = useState<'ALL'|'ALERT'|'BREAKOUT'>('ALL')
  const [queryText,setQueryText] = useState('')
  const query = useQuery({queryKey:['strategy-research','daily'], queryFn:getDailyResearch, staleTime:120000})
  const data = query.data
  const rows = useMemo(()=>data?.items.filter(r=>(kind==='ALL'||r.kind===kind)&&(type==='ALL'||(type==='ALERT'?r.joint_alert:r.first_breakout))&&r.symbol.includes(queryText.toUpperCase()))??[],[data,kind,type,queryText])
  return <><div className={styles.actions}><h2>今日机会</h2><span className={styles.meta}>{data?`最近完整交易日 ${data.expected_trade_date} · 已发布 ${data.trade_date} · Release ${data.release_id}`:'等待已发布盘后批次'}</span></div>
    <p className={styles.hint}>所有买卖候选、模型报警及已发生的 D1–D3 首次突破都在同一张清单。点击事件将精准定位同一 Candidate ID。</p>
    <div className={styles.filters}><select aria-label="方向" value={kind} onChange={e=>setKind(e.target.value as typeof kind)}><option value="ALL">全部方向</option><option value="L">买入候选</option><option value="H">卖出候选</option></select>
      <select aria-label="信号" value={type} onChange={e=>setType(e.target.value as typeof type)}><option value="ALL">全部候选</option><option value="ALERT">联合报警</option><option value="BREAKOUT">D1–D3首次突破</option></select>
      <input aria-label="搜索股票" placeholder="搜索股票代码" value={queryText} onChange={e=>setQueryText(e.target.value)} /></div>
    {query.isLoading&&<div className={styles.empty}>读取已发布今日机会…</div>}{query.isError&&<div className={styles.empty}>{errorText(query.error)}</div>}
    {data&&<div className={styles.scroll}><table><thead><tr><th>股票</th><th>方向</th><th>阶段</th><th>L1</th><th>L2</th><th>联合报警</th><th>首次突破</th><th>候选身份</th><th>操作</th></tr></thead><tbody>
      {rows.map(row=><tr key={row.observation_id}><td>{row.symbol}</td><td>{row.kind==='L'?'BUY':'SELL'}</td><td>{row.stage}</td><td>{fmt(row.l1_score)}</td><td>{fmt(row.l2_score)}</td><td>{row.joint_alert?'是':'—'}</td><td>{row.first_breakout?'是':'—'}</td><td>{row.candidate_id}</td><td><button onClick={()=>focus(row,data.release_id,data.trade_date)}>定位个股 →</button></td></tr>)}
      {!rows.length&&<tr><td colSpan={9}>该已发布批次无符合条件的事件。</td></tr>}
    </tbody></table></div>}
  </>
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
  const releaseId=params.get('release_id')
  const observationId=params.get('observation_id')
  const decisionDate=params.get('decision_date')
  function navigateTab(t:Tab){const p=new URLSearchParams(params);p.set('tab',t);setParams(p)}
  function locate(stock:string,id:string|null,s?:string,context?:LinkContext){
    const p=new URLSearchParams(params)
    p.set('tab','stock');p.set('symbol',stock)
    if(id)p.set('candidate_id',id);else p.delete('candidate_id')
    if(s)p.set('stage',s);else p.delete('stage')
    if(context){p.set('release_id',context.releaseId);p.set('observation_id',context.observationId);p.set('decision_date',context.decisionDate)}
    else {p.delete('release_id');p.delete('observation_id');p.delete('decision_date')}
    setParams(p)
  }
  return <main className={styles.root}>
    <header className={styles.header}><div><small>盘迹 / STRATEGY RESEARCH</small><h1>策略研究中心</h1><p>同一候选事件贯穿个股研究、历史评价与当日机会</p></div><span className={styles.status}>真实数据模式 · 未发布则显示空态</span></header>
    <nav className={styles.tabs} aria-label="策略子页面">
      {([['stock','个股'],['evaluation','历史评价'],['today','今日机会'],['management','模型管理']] as const).filter(([id])=>id!=='management'||admin).map(([id,label])=><button type="button" key={id} aria-current={tab===id?'page':undefined} className={tab===id?styles.active:''} onClick={()=>navigateTab(id)}>{label}</button>)}
    </nav>
    <div className={styles.content}>
      {tab==='stock'&&<StockView urlSymbol={symbol} focusedId={focusedId} stage={stage} releaseId={releaseId} observationId={observationId} decisionDate={decisionDate} onLocation={locate} />}
      {tab==='evaluation'&&<StrategyEvaluationPage />}
      {tab==='today'&&<TodayView focus={(r,releaseId,day)=>locate(r.symbol,r.candidate_id,r.stage,{releaseId,observationId:r.observation_id,decisionDate:day})} />}
      {tab==='management'&&admin&&<ModelManagementView />}
    </div>
  </main>
}
