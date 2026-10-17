import {useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import {getStrategyJobs,readTrainingReport,retryStrategyJob} from '@/api/strategyManagement'

export default function StrategyJobsView() {
  const q=useQuery({queryKey:['strategy','admin','jobs'],queryFn:getStrategyJobs,retry:false,refetchInterval:5000})
  const [report,setReport]=useState<Awaited<ReturnType<typeof readTrainingReport>>|null>(null)
  const [message,setMessage]=useState(''),[busy,setBusy]=useState(false)
  async function run(action:()=>Promise<void>){if(busy)return;setBusy(true);try{await action()}catch{setMessage('操作失败或运行门禁未满足。')}finally{setBusy(false)}}
  return <section className="panel" style={{marginTop:16}} aria-label="持久策略任务">
    <div className="panel-head"><div><h2>交付验证与盘后任务</h2><div className="hint">仅展示实际队列与工作进度；历史训练报告保留追溯，研究训练入口已迁至 panji_lab。</div></div></div>
    {q.isError?<div className="live-empty">任务服务不可读。</div>:<div className="table-wrap"><table className="data-table dense-table"><thead><tr><th>任务 / 类型</th><th>状态</th><th>真实进度</th><th>结果 / 操作</th></tr></thead><tbody>{q.data?.items.map(j=><tr key={j.job_id}><td className="inline-code">{j.job_id}<br />{j.kind}</td><td>{j.status}</td><td>{j.progress?`${j.progress.stage} ${j.progress.completed}/${j.progress.total}`:'尚无已完成工作量'}</td><td>{j.error??''}{j.stdout_tail&&<details><summary>执行日志</summary><pre style={{whiteSpace:'pre-wrap',maxWidth:420,overflowWrap:'anywhere'}}>{j.stdout_tail}</pre></details>}{j.status==='FAILED'&&j.kind!=='TRAIN'&&<button className="btn tiny" disabled={busy} onClick={()=>run(async()=>{const result=await retryStrategyJob(j.job_id);setMessage(`重试任务已入队 ${result.job_id}`);await q.refetch()})}>重试</button>}{j.kind==='TRAIN'&&j.status==='SUCCEEDED'&&<><button className="btn tiny" disabled={busy} onClick={()=>run(async()=>{setReport(await readTrainingReport(j.job_id));setMessage('已读取真实训练报告。')})}>查看报告</button></>}</td></tr>)}{!q.data?.items.length&&<tr><td colSpan={4}>尚无可读任务，不代表运行链已经就绪。</td></tr>}</tbody></table></div>}
    {report&&<div className="panel-body"><h2>训练报告 · {report.model_count} 个任务</h2><div className="table-wrap"><table className="data-table"><thead><tr><th>任务</th><th>样本范围</th><th>样本</th><th>精准率</th><th>召回率</th><th>AUC</th></tr></thead><tbody>{Object.entries(report.metrics).flatMap(([task,sets])=>Object.entries(sets).map(([split,m])=><tr key={`${task}:${split}`}><td>{task}</td><td>{split}</td><td>{m.n}</td><td>{m.precision?.toFixed(3)??'—'}</td><td>{m.recall?.toFixed(3)??'—'}</td><td>{m.auc?.toFixed(3)??'—'}</td></tr>))}</tbody></table></div></div>}
    {message&&<div className="notice" role="status">{message}</div>}
  </section>
}
