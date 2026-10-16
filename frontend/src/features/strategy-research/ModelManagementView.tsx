import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  listModelReleases, getTrainingWindows, previewTraining, queueTraining,
  getStrategyJob, requestPrediction, activateModelRelease,
  type TrainConfig,
} from '@/api/strategyManagement'
import styles from './strategyResearchPage.module.scss'

const splitLabels = [
  ['train','训练 24个月'], ['val','验证 6个月'], ['test','测试 3个月'],
] as const

export default function ModelManagementView() {
  const windows = useQuery({
    queryKey:['strategy','admin','train-windows'],
    queryFn:getTrainingWindows, staleTime:15*60*1000, retry:false,
  })
  const [trees,setTrees] = useState(300)
  const [message,setMessage] = useState('')
  const [running,setRunning] = useState(false)
  const [jobId,setJobId] = useState('')
  const [evaluationSha,setEvaluationSha] = useState('')
  const [tradeDate,setTradeDate] = useState('')
  const releases=useQuery({queryKey:['strategy','admin','releases'],queryFn:listModelReleases,staleTime:60000,retry:false})
  const job=useQuery({
    queryKey:['strategy','admin','job',jobId],
    queryFn:()=>getStrategyJob(jobId),
    enabled:!!jobId,refetchInterval:q=>{const state=q.state.data?.status;return state==='SUCCEEDED'||state==='FAILED'?false:3000},retry:false,
  })
  const window=windows.data
  const makeConfig = ():TrainConfig|null => window ? {
    as_of:window.as_of, train:window.train,val:window.val,test:window.test,trees,
  }:null
  async function preview() {
    const cfg=makeConfig();if (!cfg)return
    setRunning(true)
    try {
      const info=await previewTraining(cfg)
      setMessage(`${info.status}：${info.message}`)
    } catch {setMessage('训练配置预检失败。')}
    finally {setRunning(false)}
  }
  async function submit() {
    const cfg=makeConfig();if (!cfg || !window?.train_enqueue_enabled)return
    setRunning(true)
    try {
      const info=await queueTraining(cfg)
      setJobId(info.job_id)
      setMessage(`训练任务已入队 ${info.job_id}；训练完成后仍需独立审核发布。`)
    } catch {setMessage('训练未入队：部署或隔离训练运行时尚未通过门禁。')}
    finally {setRunning(false)}
  }
  async function rerun() {
    if (!tradeDate)return
    setRunning(true)
    try {const info=await requestPrediction(tradeDate);setJobId(info.job_id);setMessage(`预测补跑任务已入队 ${info.job_id}`)}
    catch {setMessage('预测补跑未创建：请检查权限、运行时门禁和交易日。')}
    finally {setRunning(false)}
  }
  async function activate(releaseId:string) {
    if (!/^[0-9a-f]{64}$/.test(evaluationSha)) {setMessage('需要完整64位独立评价报告SHA256。');return}
    setRunning(true)
    try {const result=await activateModelRelease(releaseId,evaluationSha);setMessage(`模型版本状态：${result.status}`);await releases.refetch()}
    catch {setMessage('模型激活/回滚失败：独立评价审核未通过。')}
    finally {setRunning(false)}
  }
  return <section className={styles.card}>
    <h2>模型管理</h2>
    <p className={styles.hint}>训练、审核发布、盘后预测是三项独立操作，均需要管理员权限与部署运行时门禁。</p>
    <h3>滚动训练窗口 · 24 / 6 / 3个月</h3>
    {windows.isLoading&&<p className={styles.hint}>读取后端权威训练窗口…</p>}
    {windows.isError&&<p className={styles.warning}>后端训练窗口不可用，已禁止创建训练任务。</p>}
    {window&&<>
      <p className={styles.meta}>后端运行日 {window.as_of} · 上一个完整自然月为截断月 · 每个月自动滚动</p>
      <div className={styles.strategyRollingCards}>
        {splitLabels.map(([name,label])=><div className={styles.strategyRollingCard} key={name}>
          <small>{label}</small><strong>{window[name].start} ～ {window[name].end}</strong>
        </div>)}
      </div>
      <div className={styles.actions}>
        <label>全市场模型树数 <input aria-label="训练树数量" type="number" min={50} max={2000} value={trees}
          onChange={e=>setTrees(Number(e.target.value))}/></label>
        <button type="button" onClick={preview} disabled={running||trees<50||trees>2000}>预检训练配置</button>
        <button type="button" onClick={submit} disabled={!window.train_enqueue_enabled||running||trees<50||trees>2000}>重新训练模型</button>
      </div>
      {!window.train_enqueue_enabled&&<p className={styles.warning}>
        生产隔离训练运行时或管理员写入开关尚未审核开启；当前仅能查看训练区间，不接受训练任务。
      </p>}
    </>}
    {jobId&&<div className={styles.card}>
      <h3>任务状态</h3><p>任务ID：{jobId}</p>
      <p>状态：{job.data?.status??(job.isError?'读取失败':'查询中')}</p>
      {job.data?.error&&<p className={styles.warning}>{job.data.error}</p>}
    </div>}
    <h3>现役模型与版本</h3>
    <label>独立评价报告SHA256 <input aria-label="评价报告SHA256" value={evaluationSha} onChange={e=>setEvaluationSha(e.target.value.trim().toLowerCase())}/></label>
    {releases.isError&&<p className={styles.warning}>版本列表不可读取</p>}
    {releases.data&&<><p>现役Release：{releases.data.active_release?.release_id??'未激活'}</p>
      <div className={styles.scroll}><table><thead><tr><th>Release</th><th>权重摘要</th><th>标签合同</th><th>状态</th><th>操作</th></tr></thead><tbody>
        {releases.data.releases.map(r=><tr key={r.release_id}><td>{r.release_id}</td><td>{r.bundle_sha256?.slice(0,20)}</td><td>{r.label_contract}</td><td>{r.status}</td>
          <td><button type="button" disabled={running||r.status!=='AUDITED'||!r.release_id||!/^[0-9a-f]{64}$/.test(evaluationSha)}
            onClick={()=>r.release_id&&activate(r.release_id)}>审核激活/回滚</button></td></tr>)}
      </tbody></table></div>
    </>}
    <h3>每日预测补跑（不重训）</h3>
    <div className={styles.actions}><input aria-label="预测交易日" type="date" value={tradeDate} onChange={e=>setTradeDate(e.target.value)}/>
      <button type="button" disabled={!tradeDate||running} onClick={rerun}>创建预测补跑</button></div>
    {message&&<p role="status" className={styles.hint}>{message}</p>}
    <p className={styles.hint}>训练窗口为权威后端计算结果，不再允许前端提交与滚动合同不一致的任意日期。</p>
  </section>
}
