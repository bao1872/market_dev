import {useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import {getZigzagEvaluation, type StrategySide} from '@/api/strategyResearch'
import DistributionChart from '@/features/strategy-evaluation/DistributionChart'
import {fmtRate,fmtPct} from '@/features/strategy-evaluation/evaluationLogic'
import {buildCandidateDeepLink} from './candidateLinks'
import {Link} from 'react-router-dom'

export default function BreakoutEvaluationView({symbol,releaseId,gain,risk,period='ALL'}:{symbol?:string;releaseId?:string;gain:number;risk:number;period?:string}) {
  const [side,setSide]=useState<StrategySide>('BUY'), [trend,setTrend]=useState('ALL')
  const q=useQuery({queryKey:['strategy-breakouts',symbol,releaseId,period,side,trend,gain,risk],
    queryFn:()=>getZigzagEvaluation({symbol,release_id:releaseId,period,side,family:'BREAKOUT',trend:trend==='ALL'?undefined:trend,gain_pct:gain,risk_pct:risk}),retry:false,staleTime:120000})
  return <section className="panel breakout-evaluation" aria-label="已发生突破专项评价">
    <div className="panel-head"><div><h2>03 / D1–D3 已发生突破 · 后续表现</h2><div className="hint">首次突破独立收录，不要求 L1/L2 超阈值；按突破时可知的走势归组，D2/D3 跟踪不重复计数。</div></div></div>
    <div className="panel-body"><div className="control-row"><select className="select" aria-label="突破方向" value={side} onChange={e=>setSide(e.target.value as StrategySide)}><option value="BUY">上破前高</option><option value="SELL">下破前低</option></select>
      <select className="select" aria-label="突破时趋势" value={trend} onChange={e=>setTrend(e.target.value)}><option value="ALL">全部走势</option><option value="向上">上升趋势</option><option value="向下">下降趋势</option><option value="待确认">方向待确认</option></select></div>
      {q.isPending?<div className="live-empty">读取同版本突破事件…</div>:q.isError?<div className="live-empty live-error">突破专项评价不可用或版本不一致。</div>:!q.data?.models.length?<div className="live-empty">尚无已发布突破专项样本；不使用模型联合告警替代。</div>:<>
        <p className="hint">独立事件 {q.data.n_unique_events ?? '—'} · 目标 MFE ≥ {gain}% / 风险 |MAE| ≤ {risk}% · 从突破可知后的下一交易日开盘观察 20 个交易日。</p>
        <div className="table-wrap"><table className="data-table dense-table"><thead><tr><th>当时走势 / 突破</th><th>事件 / 成熟 / 未成熟</th><th>目标先触达</th><th>风险先触达 / 同日不明</th><th>终点收益 P50</th><th>回撤 P90</th></tr></thead><tbody>{q.data.models.map(m=><tr key={m.model_id}><td>{m.model_name}</td><td>{m.n_signals} / {m.n_resolved} / {m.n_unresolved}</td><td>{fmtRate(m.n_resolved?m.touch_order.TARGET_FIRST/m.n_resolved:null)}</td><td>{m.touch_order.RISK_FIRST} / {m.touch_order.SAME_BAR_UNKNOWN}</td><td>{fmtPct(m.endpoint_p50,true)}</td><td>{fmtPct(m.mdd_p90)}</td></tr>)}</tbody></table></div>
        <div className="joint-distributions"><div><h3>突破后收益分布</h3><DistributionChart models={q.data.models} metric="endpoint_pct" /></div><div><h3>突破后回撤分布</h3><DistributionChart models={q.data.models} metric="mdd_pct" /></div></div>
        <details><summary>事件明细与图上定位</summary><div className="table-wrap"><table className="data-table"><thead><tr><th>证券</th><th>首次突破日</th><th>阶段</th><th>状态</th><th>定位</th></tr></thead><tbody>{q.data.table.map(row=>{const url=buildCandidateDeepLink(row);return <tr key={`${row.model_id}:${row.event_id}`}><td>{row.symbol}</td><td>{row.decision_date}</td><td>{row.stage}</td><td>{row.outcome==='UNRESOLVED'?'未成熟':'路径已成熟'}</td><td>{url?<Link to={url}>定位首次突破 →</Link>:'缺少完整身份'}</td></tr>})}</tbody></table></div></details>
      </>}
      <div className="notice warning">突破事实没有预测 AUC；这里评价突破后的收益/回撤潜能。目标先触达率保留同日顺序不明与均未触达分母，不等同可执行交易胜率。</div>
    </div>
  </section>
}
