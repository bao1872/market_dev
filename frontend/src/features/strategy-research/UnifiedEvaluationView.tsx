import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { getZigzagEvaluation, type MetricKey, type ModelEvaluation, type ResearchPoint, type StrategySide } from '@/api/strategyResearch'
import OpportunityJointChart from '@/features/strategy-evaluation/OpportunityJointChart'
import DistributionChart from '@/features/strategy-evaluation/DistributionChart'
import { modelVisual, fmtPct, fmtRate } from '@/features/strategy-evaluation/evaluationLogic'
import { buildCandidateDeepLink, type CandidateLinkIdentity } from './candidateLinks'

const DISTRIBUTIONS: Array<{ id: MetricKey; name: string }> = [
  { id: 'endpoint_pct', name: '结构终点变化' },
  { id: 'mfe_pct', name: 'MFE 最大有利空间' },
  { id: 'mae_pct', name: 'MAE 最大不利幅度' },
  { id: 'mdd_pct', name: '路径 MDD' },
]
function errorText(error: unknown): string {
  const e = error as { response?: { status?: number } }
  if (e?.response?.status === 403) return '策略研究权限不足。'
  if (e?.response?.status === 404) return '尚未发布已审计历史评价资产。不会展示原型模拟数据。'
  return `读取真实历史评价失败（HTTP ${e?.response?.status ?? '网络异常'}）。`
}
function sum(models: ModelEvaluation[], field: 'n_signals' | 'n_resolved' | 'n_unresolved' | 'n_path' | 'joint_count'): number {
  return models.reduce((n, model) => n + model[field], 0)
}

/** Pixel/layout reference: reference/panji_strategy_unified_v2.html #view-eval.
 * All statistics come from the audited evaluation API, never from the demo file.
 */
export default function UnifiedEvaluationView({ currentSymbol }: { currentSymbol: string }) {
  const navigate = useNavigate()
  const [side, setSide] = useState<StrategySide>('BUY')
  const [scope, setScope] = useState<'market' | 'stock'>('market')
  const [gain, setGain] = useState(10)
  const [risk, setRisk] = useState(7)
  const [applied, setApplied] = useState({ gain: 10, risk: 7 })
  const [hidden, setHidden] = useState<string[]>([])
  const [metric, setMetric] = useState<MetricKey>('endpoint_pct')
  const [selected, setSelected] = useState<ResearchPoint | null>(null)

  useEffect(() => {
    const id = window.setTimeout(() => setApplied({ gain, risk }), 260)
    return () => window.clearTimeout(id)
  }, [gain, risk])
  const validSymbol = /^(SH|SZ|BJ):\d{6}$/.test(currentSymbol)
  const query = useQuery({
    queryKey: ['strategy-research', 'zigzag', side, scope === 'stock' ? currentSymbol : null, applied.gain, applied.risk],
    queryFn: () => getZigzagEvaluation({ side, symbol: scope === 'stock' ? currentSymbol : undefined, gain_pct: applied.gain, risk_pct: applied.risk }),
    enabled: scope === 'market' || validSymbol,
    staleTime: 120000,
    retry: false,
  })
  const result = query.data
  const models = useMemo(() => result?.models.filter(model => !hidden.includes(model.model_id)) ?? [], [result, hidden])
  const ids = useMemo(() => models.map(model => model.model_id), [models])
  const points = useMemo(() => result?.points.filter(point => ids.includes(point.model_id)) ?? [], [result, ids])
  const rows = useMemo(() => result?.table.filter(row => ids.includes(row.model_id)) ?? [], [result, ids])
  const nSignal = sum(models, 'n_signals')
  const nResolved = sum(models, 'n_resolved')
  const nPending = sum(models, 'n_unresolved')
  const nJoint = sum(models, 'joint_count')
  const nPath = sum(models, 'n_path')
  const nFirst = models.reduce((n, m) => n + (m.touch_order?.TARGET_FIRST ?? 0), 0)
  const nUnknown = models.reduce((n, m) => n + (m.touch_order?.SAME_BAR_UNKNOWN ?? 0), 0)
  const queryPending = applied.gain !== gain || applied.risk !== risk || query.isFetching
  const canLocate = (point: CandidateLinkIdentity) => buildCandidateDeepLink(point) !== null
  function locate(point: CandidateLinkIdentity) {
    const url = buildCandidateDeepLink(point)
    if (url) navigate(url)
  }

  return <section className="view" aria-label="历史评价">
    <div className="toolbar view-toolbar">
      <div className="left"><h2>历史评价 · 多模型同屏</h2><span className="tag purple">每模型独立事件集合</span></div>
      <div className="right">
        <div className="seg" aria-label="评价方向">
          <button type="button" className={side === 'BUY' ? 'active' : ''} aria-pressed={side === 'BUY'} onClick={() => { setSide('BUY'); setHidden([]); setSelected(null) }}>买入</button>
          <button type="button" className={side === 'SELL' ? 'active' : ''} aria-pressed={side === 'SELL'} onClick={() => { setSide('SELL'); setHidden([]); setSelected(null) }}>卖出</button>
        </div>
        <select aria-label="评价范围" className="select" value={scope} onChange={event => { setScope(event.target.value as typeof scope); setSelected(null) }}>
          <option value="market">全市场</option><option value="stock">当前个股</option>
        </select>
        {scope === 'stock' && <span className="tag gray">{currentSymbol}</span>}
      </div>
    </div>
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="panel-head">
        <div><h2>模型选择与安全边际阈值</h2><div className="hint">同一已审计快照、同一评价协议；D1 / D2 / D3 是可并行比较的模型，非互斥交易阶段。</div></div>
        <div className="control-row" aria-label="显示模型">
          {result?.models.map((model, index) => <button type="button" key={model.model_id} className="model-pill" aria-pressed={!hidden.includes(model.model_id)} onClick={() => setHidden(old => old.includes(model.model_id) ? old.filter(id => id !== model.model_id) : [...old, model.model_id])}>
            <span className="dot" style={{ background: modelVisual(model.model_id, index).color }} />{model.model_name} <span className="inline-note">{model.n_signals}行</span>
          </button>)}
          {!result && <span className="smalllabel">等待审计资产</span>}
        </div>
      </div>
      <div className="panel-body"><div className="filters">
        <label className="smalllabel" htmlFor="unified-target-range">目标最大有利空间 MFE ≥</label>
        <input id="unified-target-range" className="range" type="range" min={3} max={24} step={1} value={gain} onChange={e => setGain(Number(e.target.value))} /><b className="mono">{gain}%</b>
        <label className="smalllabel" htmlFor="unified-risk-range">可承受最大不利幅度 |MAE| ≤</label>
        <input id="unified-risk-range" className="range" type="range" min={2} max={20} step={1} value={risk} onChange={e => setRisk(Number(e.target.value))} /><b className="mono">{risk}%</b>
        <span className="tag gray">{queryPending ? '计算中 · 暂不展示旧阈值结果' : '阈值实时对比 · 事后机会包络'}</span>
      </div></div>
    </div>
    {scope === 'stock' && !validSymbol ? <div className="live-empty">请先在“个股”页选择合法股票代码，再切换当前个股评价。</div> :
      query.isPending || queryPending ? <div className="live-empty">正在读取对应阈值的已审计历史评价…</div> :
      query.isError ? <div className="live-empty live-error" role="alert">{errorText(query.error)}</div> :
      !result ? <div className="live-empty">无已发布评价数据。</div> : <>
      <div className="hint" style={{ margin: '4px 0 14px' }}>已审计快照 {result.metadata.version} · 协议 {result.metadata.evaluation_protocol} · Cohort {result.metadata.cohort} · 审计 {result.metadata.audit_ref} · 生成 {result.metadata.generated_at}</div>
      <div className="overview">
        <div className="metric"><div className="k">选中模型信号行</div><div className="v mono">{nSignal.toLocaleString()}</div><div className="d">跨模型不去重；不能冒充独立机会数</div></div>
        <div className="metric"><div className="k">联合达标比例</div><div className="v mono">{fmtRate(nPath ? nJoint / nPath : null)}</div><div className="d">路径可用 {nPath} 行 · MFE 与 MAE 同时达标</div></div>
        <div className="metric"><div className="k">目标先于风险阈值</div><div className="v mono">{fmtRate(nPath ? nFirst / nPath : null)}</div><div className="d">目标先触达 {nFirst} / 路径样本 {nPath}；同日不明 {nUnknown}</div></div>
        <div className="metric"><div className="k">结果未成熟 / 删失</div><div className="v mono">{nPending.toLocaleString()}</div><div className="d">已成熟 {nResolved} 行；未成熟不算失败</div></div>
      </div>
      {models.length === 0 ? <div className="live-empty">当前没有可展示的模型，或已全部关闭。</div> : <>
        <div className="eval-panels">
          <div className="panel">
            <div className="panel-head"><div><h2>有利空间 MFE × 不利幅度 |MAE|</h2><div className="hint">实际事件二维散点与经验密度；模型使用不同颜色和形状。右下区域满足阈值。</div></div><span className="tag gray">{result.metadata.status} · 真实快照</span></div>
            <div className="panel-body eval-chart" style={{ padding: 10 }}><OpportunityJointChart points={points} modelIds={ids} gain={gain} risk={risk} onSelect={setSelected} /><div className="hint">{result.points_sampled ? '散点为确定性抽样；统计表使用全量快照。' : '散点显示后端返回的事件。'} 点击图上的点可查看事件身份。</div></div>
          </div>
          <div className="panel">
            <div className="panel-head"><div><h2>模型安全边际对照</h2><div className="hint">满足 MFE 和 MAE 约束的历史比例</div></div></div>
            <div className="panel-body">
              {models.map((model, index) => <div className="minibar" key={model.model_id}>
                <span style={{ color: modelVisual(model.model_id, index).color }}>{model.model_name}</span>
                <div className="minibar-track"><i style={{ width: `${Math.min(100, Math.max(0, (model.joint_rate ?? 0) * 100))}%`, background: modelVisual(model.model_id, index).color }} /></div>
                <strong className="mono">{fmtRate(model.joint_rate)}</strong>
              </div>)}
              <div className="line-sep" /><h2 style={{ fontSize: 13 }}>收益 / 风险经验分布</h2>
              <div className="control-row" style={{ margin: '13px 0' }}><select className="select" aria-label="收益风险分布指标" value={metric} onChange={e => setMetric(e.target.value as MetricKey)}>{DISTRIBUTIONS.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
              <DistributionChart models={models} metric={metric} />
              <div className="notice warning" style={{ marginTop: 14 }}>MFE 与 MAE 同时达标，不等于可执行收益。同日 OHLC 触达顺序未知，不得推断先盈利。</div>
            </div>
          </div>
        </div>
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="panel-head"><div><h2>模型比较统计表</h2><div className="hint">信号行与独立事件不可混算；未校准模型分数不能当成功概率。</div></div></div>
          <div className="table-wrap"><table className="data-table dense-table"><thead><tr><th>模型</th><th>信号行 / 成熟</th><th>成功率</th><th>终点 P50</th><th>MFE P75</th><th>MAE P80</th><th>MDD P90</th><th>联合达标</th><th>样本质量</th></tr></thead><tbody>{models.map((model, index) => <tr key={model.model_id}>
            <td style={{ color: modelVisual(model.model_id, index).color }}>{model.model_name}</td><td>{model.n_signals} / {model.n_resolved}</td>
            <td>{fmtRate(model.success_rate)}</td><td>{fmtPct(model.endpoint_p50, true)}</td><td>{fmtPct(model.mfe_p75)}</td><td>{fmtPct(model.mae_p80)}</td><td>{fmtPct(model.mdd_p90)}</td><td>{fmtRate(model.joint_rate)}</td><td>{model.n_unresolved ? `未成熟 ${model.n_unresolved}` : '已成熟'}</td>
          </tr>)}</tbody></table></div>
        </div>
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="panel-head"><div><h2>历史事件明细</h2><div className="hint">已发布事件样本；完整身份字段齐备时才能精准跳转对应个股候选。</div></div></div>
          <div className="table-wrap"><table className="data-table dense-table"><thead><tr><th>日期</th><th>股票</th><th>模型</th><th>结果</th><th>MFE</th><th>MAE</th><th>MDD</th><th>终点</th><th>操作</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.model_id}:${row.event_id}:${index}`}>
            <td>{row.signal_time}</td><td>{row.symbol}</td><td>{row.model_id}</td><td>{row.outcome === 'SUCCESS' ? '成功' : row.outcome === 'FAIL' ? '失败' : '未成熟'}</td>
            <td>{fmtPct(row.mfe_pct, true)}</td><td>{fmtPct(row.mae_pct)}</td><td>{fmtPct(row.mdd_pct)}</td><td>{fmtPct(row.endpoint_pct, true)}</td>
            <td><button type="button" className="btn tiny" disabled={!canLocate(row)} onClick={() => locate(row)}>{canLocate(row) ? '定位候选 →' : '无完整身份'}</button></td>
          </tr>)}{!rows.length && <tr><td colSpan={9}>没有已发布的事件明细</td></tr>}</tbody></table></div>
        </div>
      </>}
      <p className="research-footer">历史评价展示经验机会包络而非可交易收益；同事件多 D 阶段相关，需按独立事件与时间可见性审计。模型、数据和协议均来自服务端已发布审计快照。</p>
    </>}
    {selected && <div role="presentation" style={{ position: 'fixed', zIndex: 9999, inset: 0, display: 'grid', placeItems: 'center', background: '#030c16dd' }} onClick={() => setSelected(null)}>
      <div role="dialog" aria-modal="true" aria-label="历史事件详情" className="panel" style={{ width: 'min(490px,94vw)', padding: 22 }} onClick={event => event.stopPropagation()}>
        <h2>历史事件 · {selected.model_id}</h2><p>{selected.symbol} · {selected.signal_time}</p>
        <p className="mono">MFE {fmtPct(selected.mfe_pct, true)} · MAE {fmtPct(selected.mae_pct)} · {selected.outcome}</p><p className="inline-note">{selected.event_id}</p>
        <div className="control-row"><button className="btn tiny" onClick={() => setSelected(null)}>关闭</button><button className="btn primary tiny" disabled={!canLocate(selected)} onClick={() => locate(selected)}>定位对应候选 →</button></div>
      </div>
    </div>}
  </section>
}
