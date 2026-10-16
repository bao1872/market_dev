import { useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { getDailyResearch, getStrategyHistory, type DailyRow } from '@/api/strategyCandidates'
import { currentSegmentAsOf } from './currentSegment'

const PER_PAGE = 12
function signed(n: number): string { return `${n > 0 ? '+' : ''}${n.toFixed(2)}%` }
function score(n: number | null): string { return n == null || !Number.isFinite(n) ? '—' : n.toFixed(3) }
function errorText(error: unknown): string {
  const e = error as { response?: { status?: number; data?: { detail?: string } } }
  if (e?.response?.status === 403) return '策略研究权限未激活。'
  if (e?.response?.status === 404) return '尚无已发布盘后机会批次，不展示模拟信号。'
  if (e?.response?.status === 503) return e.response.data?.detail || '当前交易日批次尚未发布，禁止使用历史数据冒充今日机会。'
  return `读取机会批次失败（${e?.response?.status ?? '网络'}）。`
}

/** Reference: unified_v2 #view-today. No fake pipeline completion or model inference. */
export default function UnifiedTodayView({ focus }: {
  focus: (row: DailyRow, releaseId: string, tradeDate: string) => void
}) {
  const [kind, setKind] = useState<'ALL' | 'L' | 'H'>('ALL')
  const [type, setType] = useState<'ALL' | 'ALERT' | 'BREAKOUT' | 'WATCH'>('ALL')
  const [filter, setFilter] = useState('')
  const [page, setPage] = useState(0)
  const query = useQuery({ queryKey: ['strategy-research', 'daily'], queryFn: getDailyResearch, retry: false, staleTime: 120000 })
  const batch = query.data
  const filtered = useMemo(() => batch?.items.filter(row =>
    (kind === 'ALL' || row.kind === kind) &&
    (type === 'ALL' || (type === 'ALERT' && row.joint_alert) || (type === 'BREAKOUT' && row.first_breakout) ||
      (type === 'WATCH' && !row.joint_alert && !row.first_breakout)) &&
    (row.symbol.toUpperCase().includes(filter.trim().toUpperCase()) || row.candidate_id.toUpperCase().includes(filter.trim().toUpperCase()))
  ) ?? [], [batch, kind, type, filter])
  const totalPages = Math.max(1, Math.ceil(filtered.length / PER_PAGE))
  const safePage = Math.min(page, totalPages - 1)
  const shown = useMemo(() => filtered.slice(safePage * PER_PAGE, (safePage + 1) * PER_PAGE), [filtered, safePage])
  const visibleSymbols = useMemo(() => [...new Set(shown.map(row => row.symbol))], [shown])
  // Bounded per-page read-only MDAS data access; never N queries for the entire market.
  // React Query caches by symbol and avoids requests for hidden rows.
  const historyQueries = useQueries({ queries: visibleSymbols.map(symbol => ({
    queryKey: ['strategy-research', 'segment-mdas', symbol, batch?.trade_date],
    queryFn: () => getStrategyHistory(symbol, 240),
    enabled: batch?.status === 'PUBLISHED', staleTime: 120000, retry: false,
  })) })
  const segmentBySymbol = new Map(visibleSymbols.map((symbol, i) => [symbol,
    historyQueries[i]?.data && batch ? currentSegmentAsOf(historyQueries[i].data, batch.trade_date) : null]))
  function changeKind(value: typeof kind) { setKind(value); setPage(0) }
  function changeType(value: typeof type) { setType(value); setPage(0) }
  return <section className="view" aria-label="今日机会">
    <div className="toolbar view-toolbar"><div className="left"><h2>今日机会 · 当日预测与已发生突破</h2><span className={`tag ${batch?.freshness === 'CURRENT' ? 'green' : 'orange'}`}>
      {batch ? `${batch.trade_date} · ${batch.freshness === 'CURRENT' ? '最新已发布批次' : '历史已发布批次（非今日）'}` : '等待已发布批次'}
    </span></div><div className="right"><span className="smalllabel">点击股票 → 个股 → 精准定位同一候选 ID</span></div></div>
    {query.isPending ? <div className="live-empty">正在读取真实已发布机会…</div> : query.isError ? <div role="alert" className="live-empty live-error">{errorText(query.error)}</div> : !batch ? <div className="live-empty">暂无机会批次</div> : <>
      {batch.freshness !== 'CURRENT' && <div className="notice warning" style={{ marginBottom: 13 }}>最新完整交易日预期 {batch.expected_trade_date}，已发布数据仍为 {batch.trade_date}；不能将旧批次宣传为今日信号。</div>}
      <div className="overview" style={{ marginTop: 0 }}>
        <div className="metric"><div className="k">当日买入候选</div><div className="v buy mono">{batch.items.filter(r => r.kind === 'L').length}</div><div className="d">低点 L / 模型买入观察（非当前上行）</div></div>
        <div className="metric"><div className="k">当日卖出候选</div><div className="v sell mono">{batch.items.filter(r => r.kind === 'H').length}</div><div className="d">高点 H / 模型卖出风险（非当前下行）</div></div>
        <div className="metric"><div className="k">D1–D3 首次突破</div><div className="v mono">{batch.items.filter(r => r.first_breakout).length}</div><div className="d">已发生的突破事实，与预测严格区分</div></div>
        <div className="metric"><div className="k">模型数据日期</div><div className="v mono" style={{ fontSize: 19 }}>{batch.trade_date.slice(5)}</div><div className="d">{batch.freshness === 'CURRENT' ? '已发布当前批次' : '历史批次，缺更不可替代'} · Release 已绑定</div></div>
      </div>
      <div className="panel">
        <div className="panel-head">
          <div><h2>同一张机会清单 · 预测与首次突破</h2><div className="hint">“候选方向”和“当前走势”分列；方向/斜率来自同日权威 QFQ 行情的已确认结构，不用 H/L 猜测当下走势。</div></div>
          <div className="filters">
            <div className="seg" aria-label="候选方向">
              {([['ALL','全部'],['L','买入'],['H','卖出']] as const).map(([value,label]) => <button key={value} type="button" className={kind === value ? 'active' : ''} aria-pressed={kind === value} onClick={() => changeKind(value)}>{label}</button>)}
            </div>
            <select className="select" aria-label="机会状态" value={type} onChange={e => changeType(e.target.value as typeof type)}><option value="ALL">全部机会</option><option value="ALERT">模型联合报警</option><option value="BREAKOUT">D1–D3已突破</option><option value="WATCH">仅观察</option></select>
            <input className="field" aria-label="筛选股票" placeholder="代码 / 候选ID" value={filter} style={{ width: 158 }} onChange={e => { setFilter(e.target.value); setPage(0) }} />
          </div>
        </div>
        <div className="table-wrap"><table className="data-table dense-table"><thead><tr>
          <th>股票</th><th>模型候选</th><th>当前走势方向</th><th>本段累计收益</th><th>本段收益率斜率</th><th>当前阶段</th><th>L1 / L2 模型分数</th><th>模型门控</th><th>突破事实</th><th>候选 ID</th><th>图上定位</th>
        </tr></thead><tbody>{shown.map(row => {
          const seg = segmentBySymbol.get(row.symbol)
          const loading = historyQueries[visibleSymbols.indexOf(row.symbol)]?.isPending
          const trendClass = seg ? (seg.direction === '向上' ? 'trend-up' : 'trend-down') : 'trend-neutral'
          return <tr key={row.observation_id}>
            <td><b>{row.symbol}</b></td><td><span className={`tag ${row.kind === 'L' ? 'gray' : 'orange'}`}>{row.kind === 'L' ? 'L · 买入候选' : 'H · 卖出候选'}</span></td>
            <td className={trendClass} title={seg ? `已确认结构锚点 ${seg.anchorDate} → ${seg.lastDate}，${seg.sessions}交易间隔；当前方向由本段回归斜率决定，不代表模型预测` : '数据未达同日、复权、结构和来源一致性门禁，禁止推断'}>
              {seg ? `${seg.direction}${seg.contrarySlope ? ' · 结构背离' : ''}` : loading ? '读取结构…' : '方向待确认'}
            </td>
            <td className="mono">{seg ? signed(seg.totalReturnPct) : '—'}</td>
            <td className="mono" title="按最近已确认结构锚点至当前日的 QFQ 收盘价，对 log(price) ~ 交易日序号做 OLS，斜率=100×(exp(beta)-1)，单位 %/交易日。非可交易收益。">{seg ? `${signed(seg.slopePctPerSession)} / 交易日` : '—'}</td>
            <td>{row.stage}</td><td className="mono">{score(row.l1_score)} / {score(row.l2_score)}</td>
            <td><span className={`tag ${row.joint_alert ? 'green' : 'gray'}`}>{row.joint_alert ? '联合报警' : '仅观察'}</span></td>
            <td><span className={`tag ${row.first_breakout ? 'orange' : 'gray'}`}>{row.first_breakout ? '首次突破' : '未突破'}</span></td>
            <td><span className="inline-code" title={row.observation_id}>{row.candidate_id}</span></td>
            <td><button type="button" className="btn tiny" onClick={() => focus(row, batch.release_id, batch.trade_date)}>定位 →</button></td>
          </tr>
        })}{!shown.length && <tr><td colSpan={11}>当前筛选下无符合条件的已发布候选；不代表没有行情机会。</td></tr>}</tbody></table></div>
        <div className="table-footer"><span className="list-status">{filtered.length} 行 · {safePage + 1}/{totalPages} 页 · 每页最多 {PER_PAGE} 行（仅请求可见股票行情）</span><div className="control-row"><button type="button" disabled={safePage === 0} onClick={() => setPage(p => Math.max(0, p - 1))}>上一页</button><button type="button" disabled={safePage >= totalPages - 1} onClick={() => setPage(p => p + 1)}>下一页</button></div></div>
        <div className="panel-body" style={{ borderTop: '1px solid #263c51', padding: '10px 19px' }}><div className="hint">
          当前走势：由最新已确认结构枢轴至批次日期间的价格回归斜率符号判断向上/向下；与枢轴 L/H 隐含结构方向不一致时显示“结构背离”。
          累计收益按枢轴价到收盘价计算；斜率按段内复权收盘价的对数线性回归估计，每交易日百分比变化。两者均为已发生价格描述，不是 L1/L2 预测、也不是可执行策略收益。无同日确认结构时显示待确认。
        </div></div>
      </div>
      <div className="panel" style={{ marginTop: 14 }}>
        <div className="panel-head"><div><h2>每日盘后任务 · 运行状态</h2><div className="hint">保留原型四阶段流程布局；只有后端已提供的批次发布状态被确认为完成，其他阶段不虚构执行证据。</div></div><span className="tag green">{batch.status}</span></div>
        <div className="panel-body"><div className="pipe">
          {['01 / 日线数据就绪', '02 / 因果候选 H + L', '03 / L1 + L2 推理', '04 / 批次原子发布'].map((label, i) => <div className="node" key={label}><b>{label}</b><small>{i === 3 ? `${batch.trade_date} · ${batch.status}` : '该阶段独立证据未由当前接口提供'}</small></div>)}
        </div><div className="hint mono">trade_date: {batch.trade_date} · expected: {batch.expected_trade_date} · release: {batch.release_id} · status: {batch.status} · freshness: {batch.freshness}</div></div>
      </div>
    </>}
  </section>
}
