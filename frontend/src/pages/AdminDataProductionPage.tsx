// 数据生产中心（受保护路由，admin only）
// [管理后台优化 PRD §8.2] 统一查看盘迹所有业务数据产品的生产状态、质量门禁、正式发布与恢复动作。
//
// P0：将原「盘后流水线」并入本中心，作为「盘后编排」Tab 的主内容；
//     提供 Tab 结构（总览/盘后编排/第一金字塔/板块/复盘/竞价/发布），后续 P1 阶段填充各业务 Tab。
// URL 状态：tab 进入 URL query（/admin/data-production?tab=after-close），刷新保持，可分享定位。
import { useSearchParams } from 'react-router-dom'
import AdminAfterClosePipelinePage from './AdminAfterClosePipelinePage'

export type DataProductionTab =
  | 'overview'
  | 'after-close'
  | 'first-pyramid'
  | 'board'
  | 'review'
  | 'auction'
  | 'publish'

const TAB_ITEMS: { key: DataProductionTab; label: string }[] = [
  { key: 'overview', label: '总览' },
  { key: 'after-close', label: '盘后编排' },
  { key: 'first-pyramid', label: '第一金字塔' },
  { key: 'board', label: '板块' },
  { key: 'review', label: '复盘' },
  { key: 'auction', label: '竞价' },
  { key: 'publish', label: '发布' },
]

export default function AdminDataProductionPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTabRaw = searchParams.get('tab')
  const activeTab: DataProductionTab = (TAB_ITEMS.some((t) => t.key === activeTabRaw)
    ? (activeTabRaw as DataProductionTab)
    : 'after-close') as DataProductionTab

  const handleTab = (tab: DataProductionTab) => {
    const params = new URLSearchParams(searchParams)
    if (tab === 'after-close') {
      // 默认 Tab 不加 tab 参数，保持 URL 简洁
      params.delete('tab')
    } else {
      params.set('tab', tab)
    }
    setSearchParams(params, { replace: false })
  }

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

      {/* 业务产品 Tab：P1 阶段填充聚合状态；当前给出空态占位，避免"暂无数据"误导 */}
      {activeTab !== 'after-close' && (
        <div className="card section-gap">
          <div className="card-body">
            <div className="empty-state">
              该业务产品聚合视图将在统一数据生产与发布状态阶段（P1）提供
            </div>
            <div className="hint">
              当前阶段先完成信息架构收口，后续接入后端聚合读模型（AdminRunSummary）。
            </div>
          </div>
        </div>
      )}
    </>
  )
}
