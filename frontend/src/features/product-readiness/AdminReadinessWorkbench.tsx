// Admin 盘后工作台 — ProductReadiness 九节点就绪状态 + 治理报告（Commit H / Commit G）
//
// 消费 GET /v1/admin/readiness/{trade_date} 的正式发布读模型：
// - 九节点状态（daily_facts/board_facts/stock_core/dsa_projection/chip/state_events/
//   auction_anchor/board_aggregation/review）
// - 闭包状态 + freshness 标志
// - 治理报告（pointer lineage / stale / unmatched active / degraded reasons）
//
// 状态视图：loading / empty / degraded / failed / stale，均有明确空态与错误提示。
// 用户侧页面只消费正式 publication（本页为 admin 诊断，不触发任何写操作）。
import { useState } from 'react'
import { useAdminProductReadiness } from '@/hooks/useApi'

// 闭包状态 → pill 颜色映射（与全局 status-pill 一致）
function closurePill(closure: string): string {
  if (closure === 'fully_ready') return 'ok'
  if (closure === 'core_ready' || closure === 'degraded_ready') return 'warn'
  if (closure === 'blocked') return 'off'
  return 'muted'
}

function closureText(closure: string): string {
  const map: Record<string, string> = {
    pending: '待处理',
    blocked: '阻塞',
    core_ready: '核心就绪',
    degraded_ready: '降级就绪',
    fully_ready: '完全就绪',
  }
  return map[closure] ?? closure
}

// readiness → pill 颜色映射
function readinessPill(readiness: string): string {
  if (readiness === 'ready' || readiness === 'ready_reused') return 'ok'
  if (readiness === 'degraded') return 'warn'
  if (readiness === 'unavailable' || readiness === 'blocked') return 'off'
  return 'muted'
}

function readinessText(readiness: string): string {
  const map: Record<string, string> = {
    pending: '待处理',
    ready: '就绪',
    ready_reused: '复用就绪',
    degraded: '降级',
    unavailable: '不可用',
    blocked: '阻塞',
  }
  return map[readiness] ?? readiness
}

// 产品中文名（九节点）
const PRODUCT_LABELS: Record<string, string> = {
  daily_facts: '日线刷新',
  board_facts: '板块事实',
  stock_core: '个股核心',
  dsa_projection: 'DSA 投影',
  chip: '筹码共识',
  state_events: '状态事件',
  auction_anchor: '竞价锚点',
  board_aggregation: '板块聚合',
  review: '复盘',
}

// dataSource → 中文标签
const SOURCE_LABELS: Record<string, string> = {
  publication_pointer: '发布指针',
  run_status: 'run 状态',
  derived_from_stock_core: '派生自核心',
  review_publication: '复盘发布',
}

// lineage 关键字段 → 中文标签（用于诊断明细展示）
const LINEAGE_KEY_LABELS: Record<string, string> = {
  source_type: '来源类型',
  publication_id: '发布 ID',
  pointer_data_run_id: '指针数据 run',
  run_id: 'run ID',
  review_run_id: '复盘 run',
  source_core_run_id: '核心 run',
  derived_from: '派生自',
  algorithm_version: '算法版本',
  coverage: '覆盖率',
  reason_code: '原因码',
  readiness: '就绪',
  freshness: '新鲜度',
}

// 根据 reason_code / readiness 推导推荐恢复动作（admin 诊断提示，不触发写）
function recommendedAction(lineage: Record<string, unknown> | undefined, readiness: string, freshness: string): string {
  if (!lineage) return '—'
  const rc = lineage.reason_code as string | undefined
  if (rc === 'REUSED_PREVIOUS_RUN') return '重新触发板块事实计算以刷新指针'
  if (rc === 'CHIP_PARTIAL') return '补齐筹码口径后重算 chip 共识'
  if (rc === 'REVIEW_PUBLISHED') return '已发布，无需动作'
  if (rc === 'UPGRADED_FROM_PARENT') return '已由核心升级派生，无需动作'
  if (rc === 'PARENT_NOT_CONSUMABLE') return '等待核心就绪后派生'
  if (rc === 'NO_PUBLICATION' || rc === 'NO_REVIEW_RUN' || rc === 'NO_CHIP_RUN' || rc === 'NO_AUCTION_RUN' || rc === 'NO_RUN')
    return '运行对应任务或检查是否缺失'
  if (readiness === 'unavailable') return '检查 run 终态与失败原因'
  if (readiness === 'degraded') return '补齐依赖后重试'
  if (freshness === 'stale') return '检查是否落后核心，必要时刷新'
  return '无需动作'
}

export default function AdminReadinessWorkbench() {
  // 默认查询最近交易日（写死为当前 A 股交易日；正式场景由盘后编排推进后呈现）
  const today = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
  }).format(new Date())
  const [tradeDate, setTradeDate] = useState<string>(today)

  const readinessQuery = useAdminProductReadiness(tradeDate)
  const data = readinessQuery.data

  const stale = data
    ? // stale：闭包非终态但查询已返回（页面展示治理报告的 stale/unmatched 信号）
      data.governance.staleChildren.length > 0
    : false

  return (
    <div className="card section-gap">
      <div className="card-head">
        <div>
          <div className="card-title">盘后就绪工作台</div>
          <div className="card-sub">
            九节点就绪状态 / 闭包评估 / 治理报告（daily_refresh · board_facts · stock_core ·
            dsa_projection · chip · state_events · auction · board_aggregation · review）
          </div>
        </div>
      </div>

      <div className="card-body">
        {/* 交易日选择 */}
        <div className="toggle-row">
          <span>交易日</span>
          <b>
            <input
              type="date"
              value={tradeDate}
              onChange={(e) => setTradeDate(e.target.value)}
              aria-label="选择交易日"
            />
          </b>
        </div>

        {readinessQuery.isLoading ? (
          <div className="notice">加载中…</div>
        ) : readinessQuery.isError ? (
          <div className="notice warn">就绪状态查询失败，请稍后重试</div>
        ) : data && data.products && data.products.length ? (
          <>
            {/* 闭包状态 + freshness 标志 */}
            <div className="toggle-row">
              <span>闭包状态</span>
              <b>
                <span className={`status-pill ${closurePill(data.closure)}`}>
                  {closureText(data.closure)}
                </span>
              </b>
            </div>
            <div className="toggle-row">
              <span>核心链就绪</span>
              <b className="num">
                {data.mandatoryProductsReady ? '是' : '否'}{' '}
                <span className="muted">（完全新鲜：{data.mandatoryProductsFullyFresh ? '是' : '否'}）</span>
              </b>
            </div>
            <div className="toggle-row">
              <span>增强任务已终态</span>
              <b className="num">{data.enhancementJobsTerminal ? '是' : '否'}</b>
            </div>

            {/* 九节点明细 */}
            <div className="card-sub" style={{ marginTop: '1em' }}>
              九节点状态
            </div>
            {data.products.map((p) => (
              <div key={p.product} className="toggle-row" style={{ alignItems: 'flex-start' }}>
                <span>
                  {PRODUCT_LABELS[p.product] ?? p.product}
                  <span className="muted" style={{ fontSize: '0.8em' }}>
                    {' '}({p.product})
                  </span>
                </span>
                <b style={{ maxWidth: '70%', textAlign: 'right' }}>
                  <span className={`status-pill ${readinessPill(p.readiness)}`}>
                    {readinessText(p.readiness)}
                  </span>
                  <span className="muted" style={{ display: 'block', fontSize: '0.8em' }}>
                    新鲜度：{p.freshness} · 终态：{p.isTerminal ? '是' : '否'} · 可消费：
                    {p.isConsumable ? '是' : '否'} · 来源：{SOURCE_LABELS[p.dataSource] ?? p.dataSource}
                  </span>
                  {/* [H 修正] 展示真实 lineage 血缘字段 */}
                  {p.lineage && Object.keys(p.lineage).length > 0 && (
                    <span className="muted" style={{ display: 'block', fontSize: '0.78em', marginTop: '0.3em' }}>
                      {Object.entries(p.lineage)
                        .filter(([k]) => k !== 'source_type' && k !== 'readiness' && k !== 'freshness')
                        .map(([k, v]) => `${LINEAGE_KEY_LABELS[k] ?? k}=${String(v)}`)
                        .join(' · ')}
                    </span>
                  )}
                  <span
                    className="muted"
                    style={{
                      display: 'block',
                      fontSize: '0.78em',
                      marginTop: '0.3em',
                      color: '#b45309',
                    }}
                  >
                    动作：{recommendedAction(p.lineage, p.readiness, p.freshness)}
                  </span>
                </b>
              </div>
            ))}

            {/* 治理报告 */}
            <div className="card-sub" style={{ marginTop: '1em' }}>
              治理报告
            </div>
            {/* [H 修正] 真实 lineage 血缘：run_id / publication_id / pointer / coverage / reason_code */}
            <div className="toggle-row" style={{ alignItems: 'flex-start' }}>
              <span>数据血缘（lineage）</span>
              <b className="num" style={{ maxWidth: '72%', textAlign: 'right', fontSize: '0.82em' }}>
                {Object.entries(data.governance.pointerLineage).map(([k, v]) => (
                  <div key={k} style={{ marginBottom: '0.2em' }}>
                    <b>{PRODUCT_LABELS[k] ?? k}</b>
                    {': '}
                    {Object.entries(v as Record<string, unknown>)
                      .filter(([fk]) => fk !== 'source_type' && fk !== 'readiness' && fk !== 'freshness')
                      .map(([fk, fv]) => `${LINEAGE_KEY_LABELS[fk] ?? fk}=${String(fv)}`)
                      .join(' · ')}
                  </div>
                ))}
              </b>
            </div>
            <div className="toggle-row">
              <span>陈旧子产品（stale）</span>
              <b className="num">
                {data.governance.staleChildren.length
                  ? data.governance.staleChildren.map((s) => PRODUCT_LABELS[s] ?? s).join('、')
                  : '无'}
              </b>
            </div>
            <div className="toggle-row">
              <span>未匹配运行中增强（unmatched）</span>
              <b className="num">
                {data.governance.unmatchedActiveChildren.length
                  ? data.governance.unmatchedActiveChildren
                      .map((s) => PRODUCT_LABELS[s] ?? s)
                      .join('、')
                  : '无'}
              </b>
            </div>
            <div className="toggle-row">
              <span>就绪产品</span>
              <b className="num">
                {data.governance.readyProducts.length
                  ? data.governance.readyProducts.map((s) => PRODUCT_LABELS[s] ?? s).join('、')
                  : '无'}
              </b>
            </div>
            <div className="toggle-row">
              <span>待处理产品</span>
              <b className="num">
                {data.governance.pendingProducts.length
                  ? data.governance.pendingProducts.map((s) => PRODUCT_LABELS[s] ?? s).join('、')
                  : '无'}
              </b>
            </div>
            <div className="toggle-row">
              <span>不可用产品</span>
              <b className="num">
                {data.governance.unavailableProducts.length
                  ? data.governance.unavailableProducts.map((s) => PRODUCT_LABELS[s] ?? s).join('、')
                  : '无'}
              </b>
            </div>

            {/* degraded reasons */}
            {data.governance.degradedReasons.length > 0 && (
              <div className="toggle-row">
                <span>降级原因</span>
                <b className="num" style={{ maxWidth: '70%', textAlign: 'right', fontSize: '0.85em' }}>
                  {data.governance.degradedReasons
                    .map((r) => `${PRODUCT_LABELS[r.product] ?? r.product}[${r.code}]`)
                    .join('；')}
                </b>
              </div>
            )}

            {stale && (
              <div className="notice warn" style={{ marginTop: '0.5em' }}>
                检测到陈旧产品（stale），可能存在复用/落后数据，请结合修复步骤核对。
              </div>
            )}
          </>
        ) : (
          <div className="notice">该交易日暂无就绪数据</div>
        )}
      </div>
    </div>
  )
}