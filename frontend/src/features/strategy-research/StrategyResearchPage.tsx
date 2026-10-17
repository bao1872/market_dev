/** PANJI_V2_UI_UNIFIED_002: unified_v2 visual system with live read-only research APIs.
 * Original reference: reference/panji_strategy_unified_v2.html
 * Route and backend capability guards remain owned by the existing app.
 */
import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '@/store/auth'
import UnifiedStockView from './UnifiedStockView'
import UnifiedEvaluationView from './UnifiedEvaluationView'
import UnifiedTodayView from './UnifiedTodayView'
import UnifiedManagementView from './UnifiedManagementView'
import './unifiedV2.scss'

type Tab = 'stock' | 'evaluation' | 'today' | 'management'
type LinkContext = { releaseId: string; observationId: string; decisionDate: string }
const TABS: Tab[] = ['stock', 'evaluation', 'today', 'management']
export default function StrategyResearchPage() {
  const [params, setParams] = useSearchParams()
  const user = useAuthStore(s => s.user)
  const admin = !!user?.is_admin
  const desired = params.get('tab')
  const tab: Tab = TABS.includes(desired as Tab) && (desired !== 'management' || admin) ? desired as Tab : 'stock'
  const symbol = params.get('symbol') ?? 'SH:600000'
  const focusedId = params.get('candidate_id')
  const stage = params.get('stage') ?? 'D1'
  const releaseId = params.get('release_id')
  const observationId = params.get('observation_id')
  const decisionDate = params.get('decision_date')
  function navigateTab(next: Tab) {
    if (next === 'management' && !admin) return
    const p = new URLSearchParams(params)
    p.set('tab', next)
    setParams(p)
  }
  function locate(stock: string, id: string | null, selectedStage?: string, context?: LinkContext) {
    const p = new URLSearchParams(params)
    p.set('tab', 'stock')
    p.set('symbol', stock)
    if (id) p.set('candidate_id', id)
    else p.delete('candidate_id')
    if (selectedStage) p.set('stage', selectedStage)
    else p.delete('stage')
    if (context) {
      p.set('release_id', context.releaseId)
      p.set('observation_id', context.observationId)
      p.set('decision_date', context.decisionDate)
    } else {
      p.delete('release_id')
      p.delete('observation_id')
      p.delete('decision_date')
    }
    setParams(p)
  }
  return <main id="panji-research-v2" className="shell">
    <header className="headline">
      <div>
        <div className="eyebrow">PANJI / STRATEGY RESEARCH</div>
        <h1>策略中心 <span className="tag gray">五类互补 · 全市场统一模型</span></h1>
        <div className="subtitle">一只股票、一张K线，同时看到买入与卖出候选；每个点可追溯 D0 / L1 / L2 评分、决策时点与分任务性能。</div>
      </div>
      <div className="header-actions">
        <span className="tag green">仅展示已发布版本</span>
        {admin && <button type="button" className="btn primary" onClick={() => navigateTab('management')}>模型与运行管理</button>}
      </div>
    </header>
    <div className="head-meta">
      <span className="meta">策略 <b>ZigZag · 两层机会识别</b></span>
      <span className="meta">当前个股 <b>{symbol}</b></span>
      <span className="meta">数据来源 <b>权威接口 · 未发布则空态</b></span>
      <span className="meta">模型输出 <b>D0 / L1 / L2 分数，非校准概率</b></span>
    </div>
    <nav className="tabs" aria-label="策略子页面">
      {([['stock', '个股', '01'], ['evaluation', '历史评价', '02'], ['today', '当日候选', '03'], ['management', '模型管理', '04']] as const)
        .filter(([id]) => id !== 'management' || admin)
        .map(([id, label, number]) => <button type="button" key={id}
          aria-current={tab === id ? 'page' : undefined}
          className={`tab ${tab === id ? 'active' : ''}`} onClick={() => navigateTab(id)}>
            {label} <span className="n">{number}</span>
          </button>)}
    </nav>
    {tab === 'stock' && <UnifiedStockView urlSymbol={symbol} focusedId={focusedId} stage={stage}
      releaseId={releaseId} observationId={observationId} decisionDate={decisionDate} onLocation={locate} />}
    {tab === 'evaluation' && <UnifiedEvaluationView currentSymbol={symbol} />}
    {tab === 'today' && <UnifiedTodayView focus={(row, release, date) =>
      locate(row.symbol, row.candidate_id, row.stage, {
        releaseId: release, observationId: row.observation_id, decisionDate: date,
      })} />}
    {tab === 'management' && admin && <UnifiedManagementView />}
  </main>
}
