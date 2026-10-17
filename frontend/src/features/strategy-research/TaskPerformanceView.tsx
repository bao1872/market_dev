import { strategyPreviewKey } from '@/api/strategyPreview'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getTaskPerformance } from '@/api/strategyResearch'
import { fmtRate } from '@/features/strategy-evaluation/evaluationLogic'

export default function TaskPerformanceView({ symbol, releaseId }: {symbol?: string; releaseId?: string}) {
  const [period, setPeriod] = useState('TEST')
  const [family, setFamily] = useState('L1')
  const [kind, setKind] = useState('L')
  const [direction, setDirection] = useState('向上')
  const [stage, setStage] = useState('ALL')
  const query = useQuery({queryKey: [strategyPreviewKey(), 'strategy-task-performance', symbol, releaseId, period],
    queryFn: () => getTaskPerformance({symbol, release_id: releaseId, period}), retry: false, staleTime: 120000})
  const rows = (family === 'TREND' ? query.data?.trend.tasks : query.data?.tasks)?.filter(t => (family === 'TREND' ? t.direction === direction : t.layer === family && t.kind === kind) && (stage === 'ALL' || t.stage === stage)) ?? []
  return <section className="panel task-performance" aria-label={symbol ? '个股分任务性能评价' : '全市场分任务性能评价'}>
    <div className="panel-head"><div><h2>01 / 分任务识别能力{symbol ? ` · ${symbol}` : ' · 全市场'}</h2><div className="hint">完整适用候选池 · 固定模型阈值 · 按任务和阶段独立评价</div></div>
      <label>样本范围 <select className="select" aria-label="任务评价样本范围" value={period} onChange={e => setPeriod(e.target.value)}>
        <option value="TEST">独立测试 TEST</option><option value="POST_TEST">测试后观察 POST_TEST</option><option value="VAL">验证 VAL</option><option value="UNASSIGNED">未标注切割</option><option value="ALL">全部已发布样本</option></select></label></div>
    <div className="panel-body"><div className="control-row">
      {([['L1','高低点确认 L1'],['L2','突破确认 L2'],['D0','当日极值 D0'],['TREND','趋势确认']] as const).map(([id,label]) =>
        <button type="button" key={id} className={`btn ${family === id ? 'primary' : ''}`} aria-pressed={family === id} onClick={() => {setFamily(id);setStage('ALL')}}>{label}</button>)}
    </div>
    <>
      <div className="control-row" style={{marginTop: 14}}>{family === 'TREND' ? <div className="seg" aria-label="TV末段方向">{['向上','向下'].map(d => <button type="button" key={d} aria-pressed={direction === d} className={direction === d ? 'active' : ''} onClick={() => setDirection(d)}>{d === '向上' ? '上升末段' : '下降末段'}</button>)}</div> : <div className="seg" aria-label="任务评价端点">
        <button type="button" aria-pressed={kind === 'L'} className={kind === 'L' ? 'active' : ''} onClick={() => setKind('L')}>低点 L</button>
        <button type="button" aria-pressed={kind === 'H'} className={kind === 'H' ? 'active' : ''} onClick={() => setKind('H')}>高点 H</button></div>}
        {family !== 'D0' && <select className="select" aria-label="任务评价阶段" value={stage} onChange={e => setStage(e.target.value)}><option value="ALL">逐阶段展示</option>{(family === 'TREND' ? ['D0','D1','D2','D3'] : ['D1','D2','D3']).map(s => <option key={s}>{s}</option>)}</select>}</div>
      {family === 'TREND' && <><p className="hint">按决策日 TV 最后一段分组（含虚线和实线），只评价与该末段端点匹配的 D0 / L1 观测。沿用原端点确认标签和各模型概率，各阶段独立计算。</p><p className="hint">匹配观测 {query.data?.trend.coverage.matched ?? '—'} · 端点不匹配 {query.data?.trend.coverage.endpoint_mismatch ?? '—'} · 缺少末段证据 {query.data?.trend.coverage.missing_context ?? '—'}。无适用分数不沿用旧分数；计数为模型观测数。</p></>}
      <p className="hint">{family === 'TREND' ? '正例：当前末段端点对应候选最终成为同方向确认端点；实线不自动视为正例。' : family === 'L1' ? '正例：有效候选最终成为同方向确认 ZigZag 端点。' : family === 'L2' ? '正例：真实低点后的下一高点突破前高 HH，或真实高点后的下一低点跌破前低 LL。研究条件子群单独评价。' : '正例：当日局部极值最终成为同方向确认端点；高分不表示趋势继续延伸。'}</p>
      {family === 'L2' && <div className="notice warning">L2 适用域：真实锚点与有效目标条件子群；资格并非实时全部可知。其性能不能代替完整候选链路表现。</div>}
      {query.isPending ? <div className="live-empty">读取对应版本任务评价…</div> : query.isError ? <div className="live-empty live-error">任务评价不可用，或与候选版本不一致；不展示替代版本。</div> : query.data?.status === 'UNAVAILABLE' ? <div className="live-empty">{query.data.reason}</div> : !rows.length ? <div className="live-empty">此任务 / 阶段 / 样本范围没有已发布评价；不等于性能为零。</div> :
        <div className="table-wrap"><table className="data-table dense-table"><thead><tr><th>任务</th><th>精准率</th><th>召回率</th><th>ROC-AUC</th><th>TP / FP / FN / TN</th><th>成熟 / 未成熟</th><th>样本质量</th></tr></thead><tbody>{rows.map(t => <tr key={t.task}>
          <td>{t.task}</td><td>{fmtRate(t.precision)}</td><td>{fmtRate(t.recall)}</td><td title={t.auc_unavailable_reason ?? ''}>{t.auc?.toFixed(3) ?? '—'}</td><td>{t.tp} / {t.fp} / {t.fn} / {t.tn}</td><td>{t.n_matured} / {t.n_unresolved}</td><td>{t.sample_quality === 'SMALL_SAMPLE' ? '小样本，谨慎解读' : '可用'} · 排除 {t.n_excluded}</td></tr>)}</tbody></table></div>}
    </>
    <details><summary>指标与分母口径</summary><p className="hint">精准率 TP/(TP+FP)，召回率 TP/(TP+FN)。AUC 使用全部成熟适用样本的连续分数，不只使用超阈值告警；缺少正负双类别时不可计算。未成熟不当作失败，多阶段不混算为独立事件。</p></details></div>
  </section>
}
