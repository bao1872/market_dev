import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { listModelReleases, previewTraining, queueTraining, requestPrediction, activateModelRelease, type TrainConfig } from '@/api/strategyManagement'
import styles from './strategyResearchPage.module.scss'

const defaultCfg: TrainConfig = {
  train:{start:'2023-01-01',end:'2024-12-31'},
  val:{start:'2025-01-01',end:'2025-12-31'},
  test:{start:'2026-01-01',end:'2026-09-30'},trees:300,
}
const fields = [
  ['train','Train'],['val','Validation'],['test','Test'],
] as const

export default function ModelManagementView() {
  const [cfg,setCfg]=useState(defaultCfg)
  const [message,setMessage]=useState('')
  const [running,setRunning]=useState(false)
  const [evaluationSha,setEvaluationSha]=useState('')
  const [tradeDate,setTradeDate]=useState(new Date().toISOString().slice(0,10))
  const releases=useQuery({queryKey:['strategy','admin','releases'],queryFn:listModelReleases,staleTime:60000,retry:false})
  const ordered=cfg.train.start<=cfg.train.end&&cfg.train.end<cfg.val.start&&cfg.val.start<=cfg.val.end&&
    cfg.val.end<cfg.test.start&&cfg.test.start<=cfg.test.end
  const mutateDate=(section:'train'|'val'|'test',key:'start'|'end',value:string)=>
    setCfg(prev=>({...prev,[section]:{...prev[section],[key]:value}}))
  async function preview() {
    setRunning(true)
    try {const info=await previewTraining(cfg);setMessage(`${info.status}：${info.message}`)}
    catch {setMessage('训练区间预检失败；请检查权限与参数。')}
    finally {setRunning(false)}
  }
  async function submit() {
    if (!ordered) return
    setRunning(true)
    try {const info=await queueTraining(cfg);setMessage(`训练任务已入队 ${info.job_id}（不自动发布模型）`)}
    catch {setMessage('训练任务未创建：部署开关可能尚未开启。')}
    finally {setRunning(false)}
  }
  async function rerun() {
    setRunning(true)
    try {const info=await requestPrediction(tradeDate);setMessage(`预测补跑已入队 ${info.job_id}（不是重训）`)}
    catch {setMessage('预测补跑未创建：请检查权限/部署开关/交易日。')}
    finally {setRunning(false)}
  }
  async function activate(releaseId:string) {
    if (!/^[0-9a-f]{64}$/.test(evaluationSha)) {setMessage('须填完整64位评价报告 SHA256');return}
    setRunning(true)
    try {const result=await activateModelRelease(releaseId,evaluationSha);setMessage(`模型版本状态：${result.status}`);await releases.refetch()}
    catch {setMessage('激活/回滚失败，服务端审核门禁未通过。')}
    finally {setRunning(false)}
  }
  return <section className={styles.card}>
    <h2>模型管理</h2>
    <p className={styles.hint}>策略模型版本与盘后预测相互独立。训练完成不会自动替换现役模型；所有写操作必须通过管理员权限和后端部署开关。</p>
    <h3>重新训练模型 · Train / Validation / Test</h3>
    <div className={styles.dategrid}>
      {fields.flatMap(([section,label])=>(['start','end'] as const).map(key=>
        <label key={`${section}-${key}`}>{label} {key==='start'?'起':'止'}
          <input type="date" value={cfg[section][key]} onChange={e=>mutateDate(section,key,e.target.value)}/>
        </label>))}
    </div>
    <p className={ordered?styles.good:styles.warning}>{ordered?'日期顺序合法（样本成熟及事件隔离在训练任务内再次校验）':'日期区间重叠或倒置'}</p>
    <div className={styles.actions}>
      <button type="button" onClick={preview} disabled={!ordered||running}>预检训练配置</button>
      <button type="button" onClick={submit} disabled={!ordered||running}>重新训练模型</button>
    </div>
    <h3>现役模型与版本</h3>
    <label>已核验评价报告 SHA256 <input aria-label="评价报告SHA256" value={evaluationSha} onChange={e=>setEvaluationSha(e.target.value.trim().toLowerCase())} placeholder="64位哈希，独立发布凭据" /></label>
    {releases.isError&&<p>版本列表未配置或暂不可读取。</p>}
    {releases.data&&<><p>现役 Release：{releases.data.active_release?.release_id??'未激活'}</p>
      <div className={styles.scroll}><table><thead><tr><th>Release</th><th>模型权重摘要</th><th>标签合同</th><th>审核状态</th><th>受控操作</th></tr></thead><tbody>
        {releases.data.releases.map(r=><tr key={r.release_id}><td>{r.release_id}</td><td>{r.bundle_sha256?.slice(0,20)}</td><td>{r.label_contract}</td><td>{r.status}</td><td><button type="button" disabled={running||r.status!=='AUDITED'||!r.release_id||!/^[0-9a-f]{64}$/.test(evaluationSha)} onClick={()=>r.release_id&&activate(r.release_id)}>受控激活 / 回滚</button></td></tr>)}
      </tbody></table></div></>}
    <h3>每日预测补跑（不重训）</h3>
    <div className={styles.actions}><input type="date" aria-label="预测交易日" value={tradeDate} onChange={e=>setTradeDate(e.target.value)}/>
      <button type="button" disabled={running} onClick={rerun}>创建预测补跑</button></div>
    {message&&<p className={styles.hint} role="status">{message}</p>}
    <p className={styles.hint}>模型激活与回滚必须单独核验审计报告 SHA；当前页面不提供一键无审核发布。</p>
  </section>
}
