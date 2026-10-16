import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getStockResearch, getStrategyHistory, type Candidate, type Observation } from '@/api/strategyCandidates'
import CandidateScoreChart from './CandidateScoreChart'
import StrategyStockSearch from './StrategyStockSearch'

type LinkContext = { releaseId: string; observationId: string; decisionDate: string }
export type StockLocation = (symbol: string, candidateId: string | null, stage?: string, context?: LinkContext) => void
const isSymbol = (v: string) => /^(SH|SZ|BJ):\d{6}$/.test(v)
const fmt = (n: number | null | undefined) => n == null || !Number.isFinite(n) ? '—' : n.toFixed(3)
function apiError(e: unknown) {
  const error = e as { response?: { status?: number; data?: { detail?: string } } }
  if (error.response?.status === 403) return '策略权限不足。'
  if (error.response?.status === 404) return '该股票尚无已发布模型候选，行情与结构可能仍可查看。'
  if (error.response?.status === 409) return '模型发布版本与观测身份不一致，已阻止错误关联。'
  return error.response?.data?.detail ?? '数据服务暂不可用。'
}
function ScoreLine({ candidate }: { candidate: Candidate }) {
  const data = candidate.observations
  const w = 340, h = 95, padding = 24
  const x = (i: number) => padding + i * (w - padding * 2) / Math.max(1, data.length - 1)
  const y = (v: number) => h - padding - Math.min(1, Math.max(0, v)) * (h - padding * 1.6)
  const path = (get: (o: Observation) => number | null) => data.map((o, i) => {
    const value = get(o); return value == null || !Number.isFinite(value) ? '' : `${i === 0 ? 'M' : 'L'}${x(i)},${y(value)}`
  }).filter(Boolean).join(' ')
  return <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="候选 L1 与 L2 模型分数轨迹" style={{ display: 'block', width: '100%', marginTop: 8 }}>
    {[0, .5, 1].map(k => <g key={k}><line x1="20" y1={y(k)} x2={w - 8} y2={y(k)} stroke="#2b4760" strokeDasharray="3 3" /><text x="0" y={y(k) + 3} fill="#95a9bd" fontSize="9">{k}</text></g>)}
    <path d={path(o => o.l1_score)} stroke="#43b5ef" strokeWidth="2" fill="none" />
    <path d={path(o => o.l2_score)} stroke="#ffb56a" strokeWidth="2" fill="none" />
    {data.map((o, i) => <text key={o.observation_id} x={x(i)} y="94" textAnchor="middle" fontSize="9" fill="#a0b4ca">{o.stage}</text>)}
  </svg>
}

/** Unified layout, existing identity/lineage safeguards and MDAS snapshot separation. */
export default function UnifiedStockView({ urlSymbol, focusedId, stage, releaseId, observationId, decisionDate, onLocation }: {
  urlSymbol: string; focusedId: string | null; stage: string; releaseId: string | null;
  observationId: string | null; decisionDate: string | null; onLocation: StockLocation
}) {
  const stock = useQuery({ queryKey: ['strategy-research','stock',urlSymbol,releaseId], queryFn: () => getStockResearch(urlSymbol, releaseId ?? undefined), enabled: isSymbol(urlSymbol), staleTime: 120000, retry: false })
  const history = useQuery({ queryKey: ['strategy-research','bars',urlSymbol,500], queryFn: () => getStrategyHistory(urlSymbol,500), enabled: isSymbol(urlSymbol), staleTime: 120000, retry: false })
  const [priceMode, setPriceMode] = useState<'mdas' | 'snapshot'>(focusedId ? 'snapshot' : 'mdas')
  const data = stock.data
  const selected = data?.candidates.find(c => c.candidate_id === focusedId) ?? null
  const obs = selected?.observations.find(o => o.stage === stage)
  const mismatch = !!focusedId && (!selected || !obs || (observationId && observationId !== obs.observation_id) ||
    (decisionDate && decisionDate !== obs.decision_date))
  const focused = !mismatch ? selected : null
  const focusedObs = !mismatch ? obs : undefined
  function select(c: Candidate, o?: Observation) {
    setPriceMode('snapshot')
    onLocation(urlSymbol, c.candidate_id, o?.stage, o ? {
      releaseId: data!.release_id, observationId: o.observation_id, decisionDate: o.decision_date,
    } : undefined)
  }
  const showHistory = priceMode === 'mdas' && !!history.data || !data && !!history.data
  const showSnapshot = !!data && !showHistory
  return <section className="view" aria-label="个股候选轨迹">
    <div className="toolbar view-toolbar">
      <div className="left"><h2>个股 · 统一候选轨迹</h2><span className="tag purple">买卖同图 · D1–D3</span></div>
      <div className="right"><StrategyStockSearch symbol={urlSymbol} onChoose={symbol => onLocation(symbol, null)} /></div>
    </div>
    <div className="hint" style={{ marginBottom: 14 }}>蓝色 L 表示早期低点候选，橙色 H 表示早期高点候选；均非已执行交易或自动买卖建议。MDAS 与模型历史快照来自不同价格版本，严格分开显示。</div>
    {mismatch && <div className="live-empty live-error">候选 ID、D 阶段、观测 ID 或决策日与当前版本不符，已阻止错误定位。</div>}
    {!isSymbol(urlSymbol) && <div className="live-empty live-error">股票代码格式不正确，请使用 SH:600000 等有效代码。</div>}
    {history.isError && <div className="notice warning">权威 MDAS 历史行情不可用：{apiError(history.error)}</div>}
    {stock.isError && <div className="notice warning">模型候选快照不可用：{apiError(stock.error)} 不会用别的版本覆盖。</div>}
    {stock.isPending && history.isPending && isSymbol(urlSymbol) && <div className="live-empty">正在读取权威日线与历史模型快照…</div>}
    {(history.data || data) && <div className="head-meta">
      {history.data && <span className="meta">权威行情 <b>{history.data.actual_start} ~ {history.data.actual_end}</b> · QFQ · {history.data.count}根</span>}
      {data && <span className="meta">模型发布 <b>{data.release_id}</b> · {data.candidates.length}候选</span>}
      <span className="meta">价格与候选 <b>独立图层，不混用</b></span>
    </div>}
    <div className="stock-grid">
      <div className="panel chart-card">
        <div className="panel-head"><div><h2>候选点与模型输出 · 日线 K</h2><div className="hint">最新行情与历史模型快照严格分视图。模型分数未经过概率校准。</div></div></div>
        <div className="panel-body"><div className="control-row">
          <div className="seg" aria-label="价格及候选数据来源">
            <button type="button" className={showHistory ? 'active' : ''} disabled={!history.data} aria-pressed={showHistory} onClick={() => setPriceMode('mdas')}>完整权威行情</button>
            <button type="button" className={showSnapshot ? 'active' : ''} disabled={!data} aria-pressed={showSnapshot} onClick={() => setPriceMode('snapshot')}>历史模型快照</button>
          </div>
          <span className="smalllabel">{showHistory ? `MDAS DB_ONLY · as_of ${history.data?.as_of ?? '—'}` : `模型独立快照 · ${data?.release_id ?? '—'}`}</span>
        </div></div>
        <div className="chart-stage">
          {showHistory && history.data ? <CandidateScoreChart bars={history.data.bars} structure={history.data.structure} candidates={[]} selectedId={null} stage={stage} onSelect={() => {}} /> :
            showSnapshot && data ? <CandidateScoreChart bars={data.bars} candidates={data.candidates} selectedId={mismatch ? null : focusedId} stage={stage} onSelect={(id, newStage) => {
              const candidate = data.candidates.find(c => c.candidate_id === id)
              const observation = candidate?.observations.find(o => o.stage === newStage)
              if (candidate) select(candidate, observation)
            }} /> : <div className="live-empty">当前尚无可展示的行情或已发布候选图层。</div>}
        </div>
        <div className="chart-foot"><span><i className="legend-dot" style={{ background: '#43b5ef' }} />L · 买入观察</span><span><i className="legend-dot" style={{ background: '#ffb56a' }} />H · 卖出风险</span><span>候选非交易建议；MDAS 枢轴不冒充 L1/L2 历史模型输出</span></div>
      </div>
      <div className="panel">
        <div className="panel-head"><div><h2>选中候选 · 双层模型观察</h2><div className="hint">同一候选 ID，逐日展开 L1 / L2 / 联合门控</div></div></div>
        <div className="panel-body">
          {!focused ? <div className="live-empty">点击模型快照图上的候选标记，或从下方候选表中定位。</div> : <>
            <div className="inspect-row"><span>候选方向</span><b className={focused.kind === 'L' ? 'buy' : 'sell'}>{focused.kind === 'L' ? 'L · BUY 买入观察' : 'H · SELL 卖出风险'}</b></div>
            <div className="inspect-row"><span>结构锚点</span><b className="mono">{focused.anchor_date}</b></div>
            <div className="inspect-row"><span>候选身份</span><b className="inline-code">{focused.candidate_id}</b></div>
            <div className="line-sep" />
            <div className="control-row">{focused.observations.map(o => <button className={`btn tiny ${focusedObs?.observation_id === o.observation_id ? 'primary' : ''}`} key={o.observation_id} onClick={() => select(focused, o)}>{o.stage} · {o.decision_date}</button>)}</div>
            {focusedObs && <>
              <div className="overview" style={{ gridTemplateColumns: 'repeat(2,minmax(0,1fr))' }}>
                <div className="metric"><div className="k">L1 · 第一层分数</div><div className="v buy">{fmt(focusedObs.l1_score)}</div><div className="d">门槛 {fmt(focusedObs.l1_threshold)}</div></div>
                <div className="metric"><div className="k">L2 · 条件分数</div><div className="v sell">{fmt(focusedObs.l2_score)}</div><div className="d">门槛 {fmt(focusedObs.l2_threshold)}</div></div>
              </div>
              <div className="inspect-row"><span>联合判断</span><b>{focusedObs.joint_alert ? '已报警' : '仅观察'}</b></div>
              <div className="inspect-row"><span>首次突破</span><b>{focusedObs.first_breakout ? '已观察到' : '未观察到'}</b></div>
              <div className="inspect-row"><span>决策日 / 输入截止</span><b className="inline-code">{focusedObs.decision_date} / {focusedObs.data_cutoff}</b></div>
              <div className="inspect-row"><span>已确认参考价</span><b>{focusedObs.reference_price ?? '—'} · {focusedObs.reference_confirmed_at ?? '未确认'}</b></div>
            </>}
            <div className="line-sep" /><h2 style={{ fontSize: 13 }}>两层模型分数轨迹</h2>
            <div className="hint"><span className="buy">● L1 蓝</span> · <span className="sell">● L2 橙</span> · 观察日 D1→D3</div>
            <ScoreLine candidate={focused} />
            <div className="notice warning">历史模型分数不是校准成功概率；结构突破事实与机会结果不可混称。</div>
          </>}
        </div>
      </div>
    </div>
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="panel-head"><div><h2>该股全部候选事件</h2><div className="hint">H/L 同一时间线；定位使用不可变 release、candidate、observation 和决策日身份。</div></div></div>
      <div className="table-wrap"><table className="data-table dense-table"><thead><tr><th>候选日期</th><th>方向</th><th>候选 ID</th><th>锚点参考价</th><th>最后观测</th><th>L1 分数</th><th>L2 分数</th><th>联合结果</th><th>首次突破</th><th>操作</th></tr></thead><tbody>
        {data?.candidates.map(candidate => {
          const o = candidate.observations[candidate.observations.length - 1]
          return <tr key={candidate.candidate_id}><td>{candidate.anchor_date}</td><td className={candidate.kind === 'L' ? 'buy' : 'sell'}>{candidate.kind === 'L' ? 'L · 买入观察' : 'H · 卖出风险'}</td>
            <td className="inline-code">{candidate.candidate_id}</td><td>{candidate.anchor_price.toFixed(2)}</td><td>{o?.stage ?? '—'} · {o?.decision_date ?? '—'}</td>
            <td>{fmt(o?.l1_score)}</td><td>{fmt(o?.l2_score)}</td><td>{o?.joint_alert ? '报警' : '仅观察'}</td><td>{o?.first_breakout ? '首次突破' : '—'}</td>
            <td><button className="btn tiny" type="button" onClick={() => select(candidate,o)}>定位 →</button></td></tr>
        })}
        {!data?.candidates.length && <tr><td colSpan={10}>暂无已发布模型候选，不等于该股没有价格结构。</td></tr>}
      </tbody></table></div>
    </div>
    <p className="research-footer">图上展示的是已发布历史模型数据和独立权威行情。两者复权及来源未对齐时禁止叠加，不做静默口径转换，不把模型的历史候选当作实时交易信号。</p>
  </section>
}
