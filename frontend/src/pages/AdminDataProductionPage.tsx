// 数据生产中心（受保护路由，admin only）
// [管理后台优化 PRD §8.2] 统一查看盘迹所有业务数据产品的生产状态、质量门禁、正式发布与恢复动作。
//
// 结构：
// - 盘后编排：复用原「盘后流水线」页面
// - 总览：从后端 summary.production_chain 渲染 6 个产品节点（行情/第一金字塔/板块/复盘/竞价/发布）
// - 各业务 Tab（第一金字塔/板块/复盘/竞价/发布）：展示同一聚合读模型的筛选视图（该产品节点详情），
//   不重复建设复杂详情（PRD §8.2：其他业务 Tab 先展示同一个聚合读模型的筛选视图）
// URL 状态：tab 进入 URL query（/admin/data-production?tab=after-close），刷新保持，可分享定位。
import { useSearchParams } from 'react-router-dom'
import AdminAfterClosePipelinePage from './AdminAfterClosePipelinePage'
import AdminReadinessWorkbench from '@/features/product-readiness/AdminReadinessWorkbench'
import { useAdminSystemOverview } from '@/hooks/useApi'

export type DataProductionTab =
  | 'overview'
  | 'after-close'
  | 'readiness'
  | 'first-pyramid'
  | 'board'
  | 'review'
  | 'auction'
  | 'publish'

const TAB_ITEMS: { key: DataProductionTab; label: string }[] = [
  { key: 'overview', label: '总览' },
  { key: 'after-close', label: '盘后编排' },
  { key: 'readiness', label: '盘后就绪' },
  { key: 'first-pyramid', label: '第一金字塔' },
  { key: 'board', label: '板块' },
  { key: 'review', label: '复盘' },
  { key: 'auction', label: '竞价' },
  { key: 'publish', label: '发布' },
]

// 业务产品 Tab → production_chain 节点 key 映射（筛选视图）
const TAB_TO_CHAIN_KEY: Partial<Record<DataProductionTab, string>> = {
  'first-pyramid': 'first_pyramid',
  board: 'board',
  review: 'review',
  auction: 'auction',
  publish: 'publish',
}

const CHAIN_KEY_TO_LABEL: Record<string, string> = {
  bars: '行情',
  first_pyramid: '第一金字塔',
  board: '板块分析',
  review: '复盘',
  auction: '竞价准备',
  publish: '正式发布',
}

// 节点状态 → pill 颜色映射（与全局 status-pill 一致）
function chainStatusPill(status: string): string {
  if (status === 'ok') return 'ok'
  if (status === 'failed') return 'off'
  return 'warn'
}

function chainStatusText(status: string): string {
  const map: Record<string, string> = {
    ok: '正常',
    pending: '待处理',
    running: '进行中',
    failed: '失败',
    stale: '落后',
    attention: '需关注',
    not_applicable: '不适用',
  }
  return map[status] ?? status
}

export default function AdminDataProductionPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTabRaw = searchParams.get('tab')
  // 默认进入"总览"（展示 6 节点生产链），而非盘后编排详情
  const activeTab: DataProductionTab = (TAB_ITEMS.some((t) => t.key === activeTabRaw)
    ? (activeTabRaw as DataProductionTab)
    : 'overview') as DataProductionTab

  const overviewQuery = useAdminSystemOverview(true)
  const overview = overviewQuery.data
  const chain = overview?.summary?.production_chain ?? []

  const handleTab = (tab: DataProductionTab) => {
    const params = new URLSearchParams(searchParams)
    if (tab === 'overview') {
      // 默认 Tab 不加 tab 参数，保持 URL 简洁
      params.delete('tab')
    } else {
      params.set('tab', tab)
    }
    setSearchParams(params, { replace: false })
  }

  // 业务产品 Tab 的筛选视图：展示该产品节点（若总览未命中则给空态）
  const businessChainKey = activeTab === 'overview' ? null : TAB_TO_CHAIN_KEY[activeTab] ?? null
  const businessNode = businessChainKey
    ? chain.find((n) => n.key === businessChainKey) ?? null
    : null

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">数据生产中心</h1>
          <div className="page-desc">
            统一查看各数据产品的生产状态、质量门禁、正式发布与恢复动作
          </div>
        </div>
      </div>

      <div className="jobs-tabs" role="tablist">
        {TAB_ITEMS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={activeTab === item.key}
            className={`jobs-tab ${activeTab === item.key ? 'active' : ''}`}
            onClick={() => handleTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {/* 盘后编排：并入原盘后流水线页面 */}
      {activeTab === 'after-close' && <AdminAfterClosePipelinePage />}

      {/* 盘后就绪：九节点就绪状态 + 治理报告（Commit G/H） */}
      {activeTab === 'readiness' && <AdminReadinessWorkbench />}

      {/* 总览：从后端 summary.production_chain 渲染 6 个产品节点 */}
      {activeTab === 'overview' && (
        <section className="card section-gap">
          <div className="card-head">
            <div>
              <div className="card-title">各数据产品生产状态</div>
              <div className="card-sub">行情 / 第一金字塔 / 板块分析 / 复盘 / 竞价准备 / 正式发布</div>
            </div>
          </div>
          <div className="card-body">
            {overviewQuery.isLoading ? (
              <div className="notice">加载中…</div>
            ) : overviewQuery.isError ? (
              <div className="notice warn">生产状态查询失败，请稍后重试</div>
            ) : chain.length ? (
              chain.map((node) => (
                <div key={node.key} className="toggle-row">
                  <span>{node.label}</span>
                  <b className="num" style={{ maxWidth: '60%', textAlign: 'right' }}>
                    <span className={`status-pill ${chainStatusPill(node.status)}`}>
                      {chainStatusText(node.status)}
                    </span>
                    <span className="muted" style={{ display: 'block', fontSize: '0.85em' }}>
                      {node.detail}
                    </span>
                    {node.blocking_reason && (
                      <span className="muted" style={{ display: 'block', fontSize: '0.8em' }}>
                        阻塞原因：{node.blocking_reason}
                      </span>
                    )}
                    {node.recommended_action && (
                      <span className="muted" style={{ display: 'block', fontSize: '0.8em' }}>
                        建议：{node.recommended_action}
                      </span>
                    )}
                  </b>
                </div>
              ))
            ) : (
              <div className="notice">暂无数据</div>
            )}
          </div>
        </section>
      )}

      {/* 业务产品 Tab：聚合读模型筛选视图（PRD §8.2），展示该产品节点状态，不再显示"P1 后续提供"占位 */}
      {activeTab !== 'after-close' && activeTab !== 'overview' && activeTab !== 'readiness' && (
        <section className="card section-gap">
          <div className="card-head">
            <div>
              <div className="card-title">
                {businessNode ? businessNode.label : (CHAIN_KEY_TO_LABEL[businessChainKey ?? ''] ?? '数据产品')}
              </div>
              <div className="card-sub">生产状态 / 质量门禁 / 正式发布</div>
            </div>
          </div>
          <div className="card-body">
            {overviewQuery.isLoading ? (
              <div className="notice">加载中…</div>
            ) : overviewQuery.isError ? (
              <div className="notice warn">生产状态查询失败，请稍后重试</div>
            ) : businessNode ? (
              <>
                <div className="toggle-row">
                  <span>状态</span>
                  <b>
                    <span className={`status-pill ${chainStatusPill(businessNode.status)}`}>
                      {chainStatusText(businessNode.status)}
                    </span>
                  </b>
                </div>
                <div className="toggle-row">
                  <span>交易日</span>
                  <b className="num">{businessNode.trade_date ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>run_id</span>
                  <b className="num" style={{ fontSize: '0.8em' }}>{businessNode.run_id ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>质量门禁</span>
                  <b className="num">
                    {businessNode.quality_gate === 'passed'
                      ? '已通过'
                      : businessNode.quality_gate === 'failed'
                        ? '未通过'
                        : businessNode.quality_gate === 'pending'
                          ? '待通过'
                          : '不适用'}
                  </b>
                </div>
                <div className="toggle-row">
                  <span>正式发布</span>
                  <b className="num">
                    {businessNode.publication_status === 'published'
                      ? '已发布'
                      : businessNode.publication_status === 'failed'
                        ? '发布失败'
                        : businessNode.publication_status === 'pending'
                          ? '待发布'
                          : '不适用'}
                  </b>
                </div>
                {businessNode.detail && (
                  <div className="toggle-row">
                    <span>详情</span>
                    <b className="num">{businessNode.detail}</b>
                  </div>
                )}
                {businessNode.blocking_reason && (
                  <div className="toggle-row">
                    <span>阻塞原因</span>
                    <b className="num">{businessNode.blocking_reason}</b>
                  </div>
                )}
                {businessNode.recommended_action && (
                  <div className="toggle-row">
                    <span>建议动作</span>
                    <b className="num">{businessNode.recommended_action}</b>
                  </div>
                )}
              </>
            ) : (
              <div className="notice">该产品暂无生产记录</div>
            )}
          </div>
        </section>
      )}
    </>
  )
}
