import { Link } from 'react-router-dom'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  listModelReleases, receiveLabDelivery,
  getStrategyJob, requestPrediction, activateModelRelease,
  getStrategyRuntime, auditModelRelease,
} from '@/api/strategyManagement'
import StrategyJobsView from './StrategyJobsView'

export default function UnifiedManagementView() {
  const [deliveryFile, setDeliveryFile] = useState<File | null>(null)
  const [manifestSha, setManifestSha] = useState('')
  const [reportSha, setReportSha] = useState('')
  const [predictionDate, setPredictionDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [jobId, setJobId] = useState('')
  const [message, setMessage] = useState('')
  const runtime = useQuery({queryKey:['strategy','admin','runtime'],queryFn:getStrategyRuntime,retry:false,refetchInterval:15000})
  const releases = useQuery({ queryKey: ['strategy','admin','releases'], queryFn: listModelReleases, retry: false, staleTime: 60000 })
  const job = useQuery({ queryKey: ['strategy','admin','job',jobId], queryFn: () => getStrategyJob(jobId), enabled: !!jobId, retry: false,
    refetchInterval: q => { const status = q.state.data?.status; return status === 'SUCCEEDED' || status === 'FAILED' ? false : 3000 } })
  const validSha = /^[0-9a-f]{64}$/.test(reportSha)
  async function run(action: () => Promise<string>) {
    if (busy) return
    setBusy(true)
    try { setMessage(await action()) } catch (error) {
      const detail=(error as {response?:{data?:{detail?:unknown}}}).response?.data?.detail
      setMessage(typeof detail==='string' ? `操作未完成：${detail.slice(0,1000)}` : '操作被服务器拒绝或运行时门禁未通过。')
    }
    finally { setBusy(false) }
  }
  return <section className="view" aria-label="模型管理">
    <div className="toolbar view-toolbar"><div className="left"><h2>模型管理 · 权重与发布生命周期</h2><span className="tag purple">管理员专属</span></div>
      <span className="tag gray">全市场统一 14 任务</span>
    </div>
    <div className="notice" style={{ marginBottom: 16 }}>panji_lab 负责研究、训练与实验验证；本系统负责接收、技术验证、人工激活和日常运行。同一时刻仅运行一套批准的全市场模型。</div>
    <div className="notice">策略运行时：{runtime.data?.status ?? (runtime.isError ? '读取失败' : '读取中')} · 交付验证{runtime.data?.can_validate_delivery ? '可用' : '不可用'} · 现役模型推理{runtime.data?.can_predict ? '可用' : '不可用'}</div>
    <div className="notice">盘后调度：{runtime.data?.after_close?.status ?? '未返回调度记录'} {runtime.data?.after_close?.checked_at ?? ''} {runtime.data?.after_close?.error ?? runtime.data?.after_close?.reason ?? ''}{runtime.data?.after_close?.backlog_exceeds_31_sessions && ' · 漏跑超过自动补偿边界，需指定日期处理'}</div>
    <div className="panel" style={{marginBottom:16}}><div className="panel-head"><h2>接收 panji_lab 交付</h2></div><div className="panel-body">
      <p className="hint">ZIP 包须包含完整十四模型、批准身份、训练/验证/测试报告及黄金样例。接收后执行特征和分数对账，再生成指定日期的全市场候选评价；不会自动激活。</p>
      <div className="control-row"><input className="field" type="file" accept=".zip" aria-label="Lab 交付包" onChange={e=>setDeliveryFile(e.target.files?.[0] ?? null)} />
      <input className="field" aria-label="交付 manifest SHA256" placeholder="lab 提供的 manifest SHA256" value={manifestSha} onChange={e=>setManifestSha(e.target.value.trim().toLowerCase())} />
      <input className="field" type="date" aria-label="交付评价交易日" value={predictionDate} onChange={e=>setPredictionDate(e.target.value)} />
      <button className="btn primary" disabled={busy || !deliveryFile || !/^[0-9a-f]{64}$/.test(manifestSha) || !predictionDate || !runtime.data?.can_validate_delivery} onClick={()=>run(async()=>{const result=await receiveLabDelivery(deliveryFile!,manifestSha,predictionDate);setJobId(result.job_id);return `交付已接收，验证任务 ${result.job_id}；未激活。`})}>接收并验证</button></div>
    </div></div>
    {releases.data?.installed_models?.map(model=><div className="notice" key={model.manifest_sha256 ?? model.status}>已安装模型：{model.version ?? '身份不可用'} · {model.model_count ?? 0} 任务 · {model.status}<div className="inline-code">{model.manifest_sha256}</div><div>输入合同：{model.price_input_contract ?? '未验证'}；{model.blockers.length ? `待处理：${model.blockers.join('、')}` : '文件完整性已核对，仍需交付验证与评价'}</div></div>)}
    <div className="admin-grid">
      <div className="panel"><div className="panel-head"><div><h2>当前正式模型</h2><div className="hint">保留唯一现役身份；待验收及回滚记录在下方受控流程中查看</div></div><span className="tag green">人工审核</span></div>
        <div className="panel-body">
          {releases.isPending && <div className="hint">正在读取服务器发布版本…</div>}
          {releases.isError && <div className="notice warning">发布版本接口不可用，禁止模型激活。</div>}
          {releases.data && <>
            <div className="inspect-row"><span>现役 Release</span><b className="mono">{releases.data.active_release?.release_id ?? '未激活'}</b></div>
            <details><summary>交付审核与必要历史记录</summary><div className="line-sep" /><div className="control-row"><label className="smalllabel" htmlFor="model-report-sha">评价资产 SHA256（64 位）</label>
              <input id="model-report-sha" className="field" style={{ flex: '1 1 230px' }} value={reportSha} onChange={e => setReportSha(e.target.value.trim().toLowerCase())} placeholder="核对评价资产后填写完整 SHA256" /></div>
            <div className="table-wrap"><table className="data-table"><thead><tr><th>版本</th><th>摘要</th><th>标签合同</th><th>状态</th><th>操作</th></tr></thead><tbody>
              {releases.data.releases.map(release => <tr key={release.release_id ?? release.bundle_sha256}>
                <td className="inline-code">{release.release_id ?? '—'}</td><td className="inline-code">{release.bundle_sha256?.slice(0, 14) ?? '—'}</td>
                <td>{release.label_contract ?? '—'}</td><td><span className="tag gray">{release.status ?? '未知'}</span>{!release.production_eligible && <span className="hint">历史记录，仅供追溯</span>}<div className="inline-code">评价：{release.evaluation_sha256 ?? '未生成'}</div></td>
                <td>{release.batch_id && <Link className="btn tiny" to={`/strategy?tab=stock&preview_batch=${release.batch_id}`}>预览本批次</Link>}{release.status === 'VALIDATED_STAGING_NOT_DEPLOYED' && <button className="btn tiny" type="button" disabled={busy || !release.production_eligible || !validSha || reportSha !== release.evaluation_sha256 || !release.release_id} onClick={() =>
                  run(async () => { const result = await auditModelRelease(release.release_id!, reportSha); await releases.refetch(); return `人工审核结果：${result.status}` })}>确认评价并审核</button>}
                  <button className="btn tiny" type="button" disabled={busy || !release.production_eligible || !validSha || release.status !== 'AUDITED' || !release.release_id} onClick={() =>
                  run(async () => { const result = await activateModelRelease(release.release_id!, reportSha); await releases.refetch(); await runtime.refetch(); return `后端返回模型发布状态：${result.status}` })}>激活 / 回滚</button></td>
              </tr>)}
            </tbody></table></div>
            <div className="hint">仅 AUDITED 版本可提交；最终能否激活、回滚或变更现役 Release，必须由权威后端校验，页面不绕过安全门。</div>
            </details>
          </>}
        </div>
      </div>
      <div className="panel"><div className="panel-head"><div><h2>盘后运行与指定日期补跑</h2><div className="hint">任务由现有持久队列执行，模型与结果身份分别核验</div></div></div>
        <div className="panel-body">
          <div className="section-caption">最近操作任务 <span className="tag gray">{job.data?.status ?? (jobId ? '读取中' : '暂无')}</span></div>
          {jobId ? <div className="inspect-row"><span>任务 ID</span><b className="inline-code">{jobId}</b></div> : <div className="hint">本页尚未发起任务；不能据此推断服务器无历史任务。</div>}
          {job.data?.error && <div className="notice warning">{job.data.error}</div>}
          <div className="line-sep" />
          <div className="section-caption">每日预测补跑 <span className="tag gray">不重训模型</span></div>
          <div className="control-row"><input type="date" className="field" aria-label="补跑交易日" value={predictionDate} onChange={e => setPredictionDate(e.target.value)} />
            <button type="button" className="btn" disabled={busy || !predictionDate || !runtime.data?.can_predict} onClick={() => run(async () => { const x = await requestPrediction(predictionDate); setJobId(x.job_id); return `预测补跑任务已登记 ${x.job_id}` })}>申请预测补跑</button></div>
        </div>
      </div>
    </div>
    <StrategyJobsView />
    {message && <div className="notice" role="status" style={{ marginTop: 15 }}>{message}</div>}
    <p className="research-footer">本管理页接入真实管理员接口，不使用静态 HTML 的模拟作业、假发布状态或假 SHA。交付验证、补跑和人工发布均须通过后端门禁。历史训练任务仅作为过渡证据保留。</p>
  </section>
}
