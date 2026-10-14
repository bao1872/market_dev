import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { getZigzagEvaluation, type MetricKey, type ResearchPoint, type StrategySide } from '@/api/strategyResearch'

import { fmtPct, fmtRate, modelVisual } from './evaluationLogic'
import OpportunityJointChart from './OpportunityJointChart'
import DistributionChart from './DistributionChart'
import { buildCandidateDeepLink, type CandidateLinkIdentity } from '@/features/strategy-research/candidateLinks'
import styles from './strategyEvaluation.module.scss'

const DISTRIBUTIONS: { id: MetricKey; name: string }[] = [
  { id: 'endpoint_pct', name: '终点变化' },
  { id: 'mfe_pct', name: 'MFE 有利空间' },
  { id: 'mae_pct', name: 'MAE 不利偏离' },
  { id: 'mdd_pct', name: 'MDD 路径回撤' },
]

function errorKind(err: unknown) {
  const e = err as { response?: { status?: number }; message?: string }
  if (e?.response?.status === 404) return '没有已发布的研究快照。请先完成研究数据审计与导入；本页面不会使用模拟数据。'
  if (e?.response?.status === 403) return '当前账户没有策略研究权限，无法访问历史评价。'
  return `研究数据读取失败（${e?.response?.status ?? '网络'}）。请检查快照完整性和服务状态。`
}

export default function StrategyEvaluationPage() {
  const navigate = useNavigate()
  const [side, setSide] = useState<StrategySide>('BUY')
  const [scope, setScope] = useState<'all' | 'stock'>('all')
  const [input, setInput] = useState('')
  const [symbol, setSymbol] = useState<string | undefined>()
  const [gain, setGain] = useState(5)
  const [risk, setRisk] = useState(5)
  const [disabled, setDisabled] = useState<string[]>([])
  const [metric, setMetric] = useState<MetricKey>('endpoint_pct')
  const [selectedPoint, setSelectedPoint] = useState<ResearchPoint | null>(null)
  const query = useQuery({
    queryKey: ['strategy-research', 'zigzag', side, scope === 'stock' ? symbol : null, gain, risk],
    queryFn: () => getZigzagEvaluation({ side, symbol: scope === 'stock' ? symbol : undefined, gain_pct: gain, risk_pct: risk }),
    enabled: scope === 'all' || !!symbol,
    staleTime: 120_000,
  })
  const response = query.data
  const models = useMemo(() => response?.models.filter(m => !disabled.includes(m.model_id)) ?? [], [response, disabled])
  const ids = useMemo(() => models.map(m => m.model_id), [models])
  const points = useMemo(() => response?.points.filter(p => ids.includes(p.model_id)) ?? [], [response, ids])
  const rows = useMemo(() => response?.table.filter(p => ids.includes(p.model_id)) ?? [], [response, ids])
  const signalRows = models.reduce((n, m) => n + m.n_signals, 0)
  const resolvedRows = models.reduce((n, m) => n + m.n_resolved, 0)
  const successRows = models.reduce((n, m) => n + m.n_success, 0)
  const jointEvents = models.reduce((n, m) => n + m.joint_count, 0)
  const jointN = models.reduce((n, m) => n + m.n_path, 0)
  function toggle(id: string) { setDisabled(s => s.includes(id) ? s.filter(v => v !== id) : [...s, id]) }
  function switchSide(v: StrategySide) { setSide(v); setDisabled([]); setSelectedPoint(null) }
  function canLocate(p: CandidateLinkIdentity): boolean { return buildCandidateDeepLink(p) !== null }
  function stockLink(p: CandidateLinkIdentity) {
    const link = buildCandidateDeepLink(p)
    if (link) navigate(link)
  }
  return <div className={styles.page}>
    <div className={styles.head}>
      <div><div className={styles.eyebrow}>盘迹 / STRATEGY RESEARCH</div><h1>策略评价工作台</h1><p>研究信号的机会空间、安全边际与历史可靠性 · 非自动交易回测</p></div>
      <div className={styles.version}>只读研究 · {response?.metadata.version ?? '尚无快照'}<br /><small>历史诊断 / 非实时荐股</small></div>
    </div>
    <section className={styles.toolbar} aria-label="策略评价筛选器">
      <label className={styles.selectLabel}>策略 <strong>两层 ZigZag 结构机会</strong></label>
      <div className={styles.segment} aria-label="买卖方向"><button className={side === 'BUY' ? styles.active : ''} onClick={() => switchSide('BUY')}>买入机会</button><button className={side === 'SELL' ? styles.active : ''} onClick={() => switchSide('SELL')}>卖出机会</button></div>
      <div className={styles.segment} aria-label="评价范围"><button className={scope === 'all' ? styles.active : ''} onClick={() => { setScope('all'); setSelectedPoint(null) }}>全市场</button><button className={scope === 'stock' ? styles.active : ''} onClick={() => { setScope('stock'); setSelectedPoint(null) }}>个股</button></div>
      {scope === 'stock' && <form className={styles.stockInput} onSubmit={e => { e.preventDefault(); const v = input.trim().toUpperCase(); if (/^[A-Z0-9:_]{3,32}$/.test(v)) setSymbol(v) }}><input aria-label="输入股票代码" value={input} onChange={e => setInput(e.target.value)} placeholder="SH:600000" /><button type="submit">分析</button></form>}
    </section>
    {scope === 'stock' && !symbol ? <section className={styles.empty}>输入股票标识（例如 SH:600000），查看该股票的模型评价。</section> : query.isLoading ? <section className={styles.empty}>正在读取已发布研究快照……</section> : query.isError ? <section className={styles.empty}>{errorKind(query.error)}</section> : !response ? <section className={styles.empty}>暂无评价数据</section> : <>
      <div className={styles.provenance}>数据版本：{response.metadata.version}　·　评价协议：{response.metadata.evaluation_protocol}　·　全候选报警口径：{response.metadata.cohort}　·　审计标识：{response.metadata.audit_ref}　·　生成：{response.metadata.generated_at}</div>
      <section className={styles.kpis}>
        <div><small>选中模型信号行（跨模型不去重）</small><strong>{signalRows.toLocaleString()}</strong><span>含未成熟事件</span></div>
        <div><small>已成熟信号行</small><strong>{resolvedRows.toLocaleString()}</strong><span>未成熟 {signalRows - resolvedRows} 行</span></div>
        <div><small>已成熟事件成功占比</small><strong>{fmtRate(resolvedRows ? successRows / resolvedRows : null)}</strong><span>分母：已成熟信号行</span></div>
        <div><small>路径联合达标率</small><strong>{fmtRate(jointN ? jointEvents / jointN : null)}</strong><span>目标 ≥{gain}% · MAE ≤{risk}%</span></div>
      </section>
      <section className={styles.modelPanel}>
        <div className={styles.sectionTitle}><h2>模型叠加</h2><span>同一方向多个模型同时比较，关闭模型后所有图表及表格同步隐藏</span></div>
        <div className={styles.modelItems}>{response.models.map((m, index) => <button type="button" key={m.model_id} className={`${styles.modelChip} ${disabled.includes(m.model_id) ? styles.off : ''}`} onClick={() => toggle(m.model_id)} aria-pressed={!disabled.includes(m.model_id)}><i style={{ background: modelVisual(m.model_id, index).color }} />{m.model_name}<small>{m.n_signals.toLocaleString()} 行</small></button>)}</div>
      </section>
      {models.length === 0 ? <section className={styles.empty}>当前方向没有可展示的模型，或所有模型已被隐藏。</section> : <>
        <div className={styles.grid}>
          <section className={styles.card}>
            <div className={styles.sectionTitle}><h2>收益–风险联合分布与散点</h2><span>经验二维分箱着色 + 实际事件散点 · 非高斯拟合</span></div>
            <OpportunityJointChart points={points} modelIds={ids} gain={gain} risk={risk} onSelect={setSelectedPoint} />
            <div className={styles.hint}>每个点是一条模型信号；不同模型可能共享同一机会。{response.points_sampled ? '图上散点为确定性抽样，统计表使用完整快照。' : '图上展示全部可用散点。'} 点选查看事件。</div>
          </section>
          <section className={styles.card}>
            <div className={styles.sectionTitle}><h2>安全边际</h2><span>阈值用于描述历史机会，不构成交易建议</span></div>
            <div className={styles.rangeRow}><label>最低有利空间 <strong>+{gain.toFixed(1)}%</strong></label><input type="range" min="1" max="30" step=".5" value={gain} onChange={e => setGain(Number(e.target.value))} /></div>
            <div className={styles.rangeRow}><label>最大不利偏离 <strong>{risk.toFixed(1)}%</strong></label><input type="range" min="1" max="30" step=".5" value={risk} onChange={e => setRisk(Number(e.target.value))} /></div>
            <div className={styles.riskTable}><div className={styles.riskHead}><span>模型</span><span>联合达标</span><span>目标先触达</span><span>同日不明</span></div>{models.map((m, i) => <div key={m.model_id} className={styles.riskLine}><span><i style={{ background: modelVisual(m.model_id, i).color }} />{m.model_name}</span><b>{fmtRate(m.joint_rate)}</b><b>{m.touch_order.TARGET_FIRST}/{m.n_resolved}</b><b>{m.touch_order.SAME_BAR_UNKNOWN}</b></div>)}</div>
            <div className={styles.hint}>联合达标：MFE≥目标且 MAE≤阈值；目标先触达另按有序日线高低价计算。无路径时标为 UNAVAILABLE，同日先后未知时不作猜测。</div>
          </section>
        </div>
        <section className={styles.card}>
          <div className={styles.sectionTitle}><h2>历史收益与风险分布</h2><span>经验相对频率，同尺度叠加比较</span></div>
          <div className={styles.tabs}>{DISTRIBUTIONS.map(d => <button key={d.id} onClick={() => setMetric(d.id)} className={metric === d.id ? styles.tabActive : ''}>{d.name}</button>)}</div>
          <DistributionChart models={models} metric={metric} />
          <div className={styles.hint}>MFE=最大有利偏离；MAE=最大不利偏离幅度；MDD=事件观察期路径最大回撤。均非实际成交收益。</div>
        </section>
        <section className={styles.card}>
          <div className={styles.sectionTitle}><h2>多模型统计对照</h2><span>{scope === 'all' ? '全市场' : symbol} · 已成熟口径 · 原始样本规模</span></div>
          <div className={styles.scroll}><table><thead><tr><th>模型</th><th>信号行</th><th>成熟 / 未成熟</th><th>成功率</th><th>终点 P50</th><th>MFE P75</th><th>MAE P80</th><th>MDD P90</th><th>联合达标</th></tr></thead><tbody>{models.map((m,i) => <tr key={m.model_id}><td><span className={styles.legend}><i style={{ background: modelVisual(m.model_id, i).color }} />{m.model_name}</span></td><td>{m.n_signals}</td><td>{m.n_resolved} / {m.n_unresolved}</td><td>{fmtRate(m.success_rate)}</td><td>{fmtPct(m.endpoint_p50,true)}</td><td>{fmtPct(m.mfe_p75)}</td><td>{fmtPct(m.mae_p80)}</td><td>{fmtPct(m.mdd_p90)}</td><td>{fmtRate(m.joint_rate)} ({m.joint_count}/{m.n_path})</td></tr>)}</tbody></table></div>
          <div className={styles.hint}>成功率以冻结的端到端事件 outcome 为准；未成熟不计入成功率分母。跨模型统计不能直接相加解释为独立市场机会数量。</div>
        </section>
        <section className={styles.card}>
          <div className={styles.sectionTitle}><h2>历史事件明细</h2><span>最近事件，每模型最多 80 行 · 仅用于抽样回溯</span></div>
          <div className={styles.scroll}><table><thead><tr><th>日期</th><th>股票</th><th>模型</th><th>结果</th><th>MFE</th><th>MAE</th><th>MDD</th><th>终点</th><th>操作</th></tr></thead><tbody>{rows.map((r,i) => <tr key={`${r.model_id}|${r.event_id}|${i}`}><td>{r.signal_time}</td><td>{r.symbol}</td><td>{r.model_id}</td><td>{r.outcome === 'SUCCESS' ? '成功' : r.outcome === 'FAIL' ? '失败' : '未成熟'}</td><td>{fmtPct(r.mfe_pct,true)}</td><td>{fmtPct(r.mae_pct)}</td><td>{fmtPct(r.mdd_pct)}</td><td>{fmtPct(r.endpoint_pct,true)}</td><td><button className={styles.textButton} disabled={!canLocate(r)} title={!canLocate(r)?'该历史事件缺少完整 candidate_id / observation_id / D阶段 / 决策日 / 发布版本，禁止错误回链':undefined} onClick={() => stockLink(r)}>{canLocate(r)?'定位该候选 →':'不可精准定位'}</button></td></tr>)}</tbody></table></div>
        </section>
      </>}
      {selectedPoint && <div className={styles.modalBackdrop} onClick={() => setSelectedPoint(null)}><div className={styles.modal} onClick={e => e.stopPropagation()}><h3>历史机会事件</h3><p>{selectedPoint.symbol} · {selectedPoint.signal_time} · {selectedPoint.model_id}</p><p>MFE {fmtPct(selectedPoint.mfe_pct, true)}　 MAE {fmtPct(selectedPoint.mae_pct)}</p><p>事件：{selectedPoint.event_id}</p><div className={styles.actions}><button onClick={() => setSelectedPoint(null)}>关闭</button><button disabled={!canLocate(selectedPoint)} title={!canLocate(selectedPoint)?'历史快照尚未提供完整事件映射':undefined} onClick={() => stockLink(selectedPoint)}>{canLocate(selectedPoint)?'定位该候选 →':'缺少候选映射'}</button></div></div></div>}
    </>}
  </div>
}
