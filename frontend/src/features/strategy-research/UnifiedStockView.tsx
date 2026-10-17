import { useQuery } from '@tanstack/react-query'
import { getStockResearch, getStrategyHistory } from '@/api/strategyCandidates'
import CandidateScoreChart from './CandidateScoreChart'
import StrategyStockSearch from './StrategyStockSearch'
import UnifiedEvaluationView from './UnifiedEvaluationView'

type LinkContext = { releaseId: string; observationId: string; decisionDate: string }
export type StockLocation = (symbol: string, candidateId: string | null, stage?: string, context?: LinkContext) => void
const isSymbol = (v: string) => /^(SH|SZ|BJ):\d{6}$/.test(v)

export default function UnifiedStockView({urlSymbol, focusedId, stage, releaseId, observationId, decisionDate, onLocation}: {
  urlSymbol: string; focusedId: string | null; stage: string; releaseId: string | null;
  observationId: string | null; decisionDate: string | null; onLocation: StockLocation
}) {
  const stock = useQuery({queryKey: ['strategy-research','stock',urlSymbol,releaseId],
    queryFn: () => getStockResearch(urlSymbol, releaseId ?? undefined), enabled: isSymbol(urlSymbol), staleTime: 120000, retry: false})
  const history = useQuery({queryKey: ['strategy-research','bars',urlSymbol,500],
    queryFn: () => getStrategyHistory(urlSymbol,500), enabled: isSymbol(urlSymbol), staleTime: 120000, retry: false})
  const data = stock.data
  const selected = data?.candidates.find(c => c.candidate_id === focusedId)
  const obs = selected?.observations.find(o => o.stage === stage)
  const mismatch = !!focusedId && (!selected || !obs || (observationId && observationId !== obs.observation_id) || (decisionDate && decisionDate !== obs.decision_date))
  return <section className="view" aria-label="个股候选轨迹">
    <div className="toolbar view-toolbar"><div className="left"><h2>个股 · 完整 K 线与五类模型</h2></div>
      <StrategyStockSearch symbol={urlSymbol} onChoose={symbol => onLocation(symbol,null)} /></div>
    <div className="head-meta"><span className="meta">全市场统一模型 <b>{data?.metadata?.model_version ?? '当前已发布版本（尚未标注十四任务版本）'}</b></span>
      {data && <span className="meta">Release <b>{data.release_id}</b></span>}</div>
    {mismatch && <div className="live-empty live-error">候选、阶段、观测或决策日与发布版本不一致，已禁止错误定位。</div>}
    {stock.isError && <div className="notice warning">模型候选快照不可用；仅可查看独立权威行情，不将其他价格版本与历史模型分数混合。</div>}
    <div className="panel"><div className="panel-head"><div><h2>日线 K 与模型评分轨道</h2><div className="hint">{data ? '模型快照的完整已发布行情区间；五类输出按决策日与主图对齐。' : '权威行情；尚无可叠加的已发布模型快照。'}</div></div></div><div className="panel-body">
      {data ? <CandidateScoreChart bars={data.bars} candidates={data.candidates} tvOverlay={data.tv_zigzag} selectedId={mismatch ? null : focusedId} stage={stage} onSelect={(id,s) => {
        const c = data.candidates.find(c => c.candidate_id === id), o = c?.observations.find(o => o.stage === s)
        if (c && o) onLocation(urlSymbol,id,s,{releaseId:data.release_id,observationId:o.observation_id,decisionDate:o.decision_date})
      }} /> : history.data ? <CandidateScoreChart bars={history.data.bars} structure={history.data.structure} tvOverlay={history.data.tv_zigzag} candidates={[]} selectedId={null} stage={stage} onSelect={() => {}} /> :
        <div className="live-empty">{stock.isPending || history.isPending ? '正在读取行情与模型快照…' : '权威行情与模型快照均不可用。'}</div>}
    </div></div>
    <p className="research-footer">评分详情位于图下，不遮挡主图。D0 是极值倾向；L1 是高低点确认；L2 是条件性突破评分，分数均非校准概率。</p>
    {data && <UnifiedEvaluationView key={`${urlSymbol}:${data.release_id}`} currentSymbol={urlSymbol} embedded releaseId={data.release_id} />}
  </section>
}
