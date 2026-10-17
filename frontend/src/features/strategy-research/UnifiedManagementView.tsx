import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  listModelReleases, getTrainingWindows, previewTraining, queueTraining,
  getStrategyJob, requestPrediction, activateModelRelease, type TrainConfig,
  getStrategyRuntime, auditModelRelease,
} from '@/api/strategyManagement'
import StrategyJobsView from './StrategyJobsView'

const windows = [['train', '训练 24 个月'], ['val', '验证 6 个月'], ['test', '测试 3 个月']] as const
export default function UnifiedManagementView() {
  const [trees, setTrees] = useState(300)
  const [reportSha, setReportSha] = useState('')
  const [predictionDate, setPredictionDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [jobId, setJobId] = useState('')
  const [message, setMessage] = useState('')
  const [showTrain, setShowTrain] = useState(false)
  const runtime = useQuery({queryKey:['strategy','admin','runtime'],queryFn:getStrategyRuntime,retry:false,refetchInterval:15000})
  const releases = useQuery({ queryKey: ['strategy','admin','releases'], queryFn: listModelReleases, retry: false, staleTime: 60000 })
  const training = useQuery({ queryKey: ['strategy','admin','train-windows'], queryFn: getTrainingWindows, retry: false, staleTime: 900000 })
  const job = useQuery({ queryKey: ['strategy','admin','job',jobId], queryFn: () => getStrategyJob(jobId), enabled: !!jobId, retry: false,
    refetchInterval: q => { const status = q.state.data?.status; return status === 'SUCCEEDED' || status === 'FAILED' ? false : 3000 } })
  const config: TrainConfig | null = training.data ? {
    as_of: training.data.as_of, model_family: 'fourteen', train: training.data.train, val: training.data.val, test: training.data.test, trees,
  } : null
  const validTrees = Number.isInteger(trees) && trees >= 50 && trees <= 2000
  const validSha = /^[0-9a-f]{64}$/.test(reportSha)
  async function run(action: () => Promise<string>) {
    if (busy) return
    setBusy(true)
    try { setMessage(await action()) } catch { setMessage('操作被服务器拒绝或运行时门禁未通过，不会假设操作成功。') }
    finally { setBusy(false) }
  }
  return <section className="view" aria-label="模型管理">
    <div className="toolbar view-toolbar"><div className="left"><h2>模型管理 · 权重与发布生命周期</h2><span className="tag purple">管理员专属</span></div>
      <div className="right"><button type="button" className="btn primary" onClick={() => setShowTrain(v => !v)}>＋ 手动训练模型</button></div>
    </div>
    <div className="notice" style={{ marginBottom: 16 }}>五类互补模型共 14 个任务，全市场统一权重。训练、验证与测试的时间范围以<strong>事件决策日期</strong>为准，由权威后端滚动窗口决定。日常盘后只执行已发布模型推理；训练完成并不自动发布新版本。</div>
    <div className="notice">策略运行时：{runtime.data?.status ?? (runtime.isError ? '读取失败' : '读取中')} · 训练{runtime.data?.can_train ? '可用' : '不可用'} · 现役模型推理{runtime.data?.can_predict ? '可用' : '不可用'}</div>
    <div className="admin-grid">
      <div className="panel"><div className="panel-head"><div><h2>模型版本与发布</h2><div className="hint">模型包、阈值、校准器、特征 SHA 作为不可变 release 审核发布</div></div><span className="tag green">人工审核</span></div>
        <div className="panel-body">
          {releases.isPending && <div className="hint">正在读取服务器发布版本…</div>}
          {releases.isError && <div className="notice warning">发布版本接口不可用，禁止模型激活。</div>}
          {releases.data && <>
            <div className="inspect-row"><span>现役 Release</span><b className="mono">{releases.data.active_release?.release_id ?? '未激活'}</b></div>
            <div className="line-sep" /><div className="control-row"><label className="smalllabel" htmlFor="model-report-sha">评价资产 SHA256（64 位）</label>
              <input id="model-report-sha" className="field" style={{ flex: '1 1 230px' }} value={reportSha} onChange={e => setReportSha(e.target.value.trim().toLowerCase())} placeholder="核对评价资产后填写完整 SHA256" /></div>
            <div className="table-wrap"><table className="data-table"><thead><tr><th>版本</th><th>摘要</th><th>标签合同</th><th>状态</th><th>操作</th></tr></thead><tbody>
              {releases.data.releases.map(release => <tr key={release.release_id ?? release.bundle_sha256}>
                <td className="inline-code">{release.release_id ?? '—'}</td><td className="inline-code">{release.bundle_sha256?.slice(0, 14) ?? '—'}</td>
                <td>{release.label_contract ?? '—'}</td><td><span className="tag gray">{release.status ?? '未知'}</span><div className="inline-code">评价：{release.evaluation_sha256 ?? '未生成'}</div></td>
                <td>{release.status === 'VALIDATED_STAGING_NOT_DEPLOYED' && <button className="btn tiny" type="button" disabled={busy || !validSha || reportSha !== release.evaluation_sha256 || !release.release_id} onClick={() =>
                  run(async () => { const result = await auditModelRelease(release.release_id!, reportSha); await releases.refetch(); return `人工审核结果：${result.status}` })}>确认评价并审核</button>}
                  <button className="btn tiny" type="button" disabled={busy || !validSha || release.status !== 'AUDITED' || !release.release_id} onClick={() =>
                  run(async () => { const result = await activateModelRelease(release.release_id!, reportSha); await releases.refetch(); await runtime.refetch(); return `后端返回模型发布状态：${result.status}` })}>激活 / 回滚</button></td>
              </tr>)}
            </tbody></table></div>
            <div className="hint">仅 AUDITED 版本可提交；最终能否激活、回滚或变更现役 Release，必须由权威后端校验，页面不绕过安全门。</div>
          </>}
        </div>
      </div>
      <div className="panel"><div className="panel-head"><div><h2>训练任务 / 预测任务</h2><div className="hint">训练与每日盘后预测具有不同任务 ID 和生命周期</div></div></div>
        <div className="panel-body">
          <div className="section-caption">最近操作任务 <span className="tag gray">{job.data?.status ?? (jobId ? '读取中' : '暂无')}</span></div>
          {jobId ? <div className="inspect-row"><span>任务 ID</span><b className="inline-code">{jobId}</b></div> : <div className="hint">本页尚未发起任务；不能据此推断服务器无历史任务。</div>}
          {job.data?.error && <div className="notice warning">{job.data.error}</div>}
          <div className="line-sep" />
          <div className="section-caption">每日预测补跑 <span className="tag gray">不重训模型</span></div>
          <div className="control-row"><input type="date" className="field" aria-label="补跑交易日" value={predictionDate} onChange={e => setPredictionDate(e.target.value)} />
            <button type="button" className="btn" disabled={busy || !predictionDate || !runtime.data?.can_predict} onClick={() => run(async () => { const x = await requestPrediction(predictionDate); setJobId(x.job_id); return `预测补跑任务已登记 ${x.job_id}` })}>申请预测补跑</button></div>
          <div className="line-sep" />
          <div className="section-caption">训练窗口 <span className="tag purple">24 / 6 / 3 个月</span></div>
          {training.isError ? <div className="notice warning">权威训练窗口不可读取，禁止预检或入队。</div> : training.isPending ? <div className="hint">读取权威后端窗口中…</div> : training.data && <>
            <div className="hint">运行日 {training.data.as_of} · 每个窗口以服务端返回日期为准</div>
            {windows.map(([key, label]) => <div className="inspect-row" key={key}><span>{label}</span><b className="inline-code">{training.data![key].start} ~ {training.data![key].end}</b></div>)}
            {!training.data.train_enqueue_enabled && <div className="notice warning">隔离训练运行时或管理员写入门禁未打开；训练提交不可用。</div>}
          </>}
        </div>
      </div>
    </div>
    <StrategyJobsView tradeDate={predictionDate} />
    {showTrain && <div role="dialog" aria-modal="true" aria-label="手动训练全市场十四任务模型" style={{ position: 'fixed', zIndex: 9998, inset: 0, background: '#030c16dd', display: 'grid', placeItems: 'center' }} onClick={() => setShowTrain(false)}>
      <div className="panel" style={{ width: 'min(650px,95vw)', maxHeight: '90vh', overflowY: 'auto' }} onClick={e => e.stopPropagation()}>
        <div className="panel-head"><div><div className="eyebrow">ADMIN / MANUAL TRAINING</div><h2>手动训练全市场十四任务模型</h2><div className="hint">仅当后端正式允许运行时才能创建训练作业。</div></div><button type="button" className="btn tiny" onClick={() => setShowTrain(false)}>关闭</button></div>
        <div className="panel-body"><div className="section-caption">01 / 数据与时间合同</div>
          {training.data ? windows.map(([key, label]) => <div className="inspect-row" key={key}><span>{label}</span><b>{training.data![key].start} ~ {training.data![key].end}</b></div>) : <div className="notice warning">后端训练窗口不可用</div>}
          <div className="line-sep" /><div className="section-caption">02 / 不可省略的执行门禁</div>
          <div className="hint">候选决策日切分、PIT、标签成熟、purge/embargo、特征与模型 SHA、独立审计均由服务端执行，页面不提供绕过开关。</div>
          <div className="control-row" style={{ marginTop: 15 }}><label>树数量 <input className="field" aria-label="训练树数量" type="number" min={50} max={2000} value={trees} onChange={e => setTrees(Number(e.target.value))} /></label></div>
          <div className="control-row" style={{ marginTop: 14 }}>
            <button type="button" className="btn" disabled={busy || !config || !validTrees} onClick={() => run(async () => { const info = await previewTraining(config!); return `${info.status}：${info.message}` })}>只读预检训练配置</button>
            <button type="button" className="btn primary" disabled={busy || !config || !validTrees || !training.data?.train_enqueue_enabled} onClick={() => run(async () => { const info = await queueTraining(config!); setJobId(info.job_id); setShowTrain(false); return `训练任务已入队 ${info.job_id}；训练完成仍需独立审核。` })}>创建训练任务</button>
          </div>
        </div>
      </div>
    </div>}
    {message && <div className="notice" role="status" style={{ marginTop: 15 }}>{message}</div>}
    <p className="research-footer">本管理页接入真实管理员接口，不使用静态 HTML 的模拟作业、假发布状态或假 SHA。训练、预测和发布仅在显式点击且后端门禁通过时执行。</p>
  </section>
}
