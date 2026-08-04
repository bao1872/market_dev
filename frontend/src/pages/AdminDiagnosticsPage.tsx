// 诊断工具（受保护路由，admin only）
// [管理后台优化 PRD §8.5] 排查个股、字段、数据质量、版本和访问问题。
//
// P0：将原「个股调试」（AdminStockDebugPage）与「访问统计」（AdminVisitorsPage）并入本中心，
//     提供 Tab 结构（个股调试/数据质量/运行版本/权限诊断/访问统计），后续 P2 阶段填充。
// URL 状态：tab 进入 URL query（/admin/diagnostics?tab=stock），刷新保持，可分享定位。
import { useSearchParams } from 'react-router-dom'
import AdminStockDebugPage from './AdminStockDebugPage'
import AdminVisitorsPage from './AdminVisitorsPage'

export type DiagnosticsTab = 'stock' | 'data-quality' | 'versions' | 'access' | 'visitors'

const TAB_ITEMS: { key: DiagnosticsTab; label: string }[] = [
  { key: 'stock', label: '个股调试' },
  { key: 'data-quality', label: '数据质量' },
  { key: 'versions', label: '运行版本' },
  { key: 'access', label: '权限诊断' },
  { key: 'visitors', label: '访问统计' },
]

export default function AdminDiagnosticsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTabRaw = searchParams.get('tab')
  const activeTab: DiagnosticsTab = (TAB_ITEMS.some((t) => t.key === activeTabRaw)
    ? (activeTabRaw as DiagnosticsTab)
    : 'stock') as DiagnosticsTab

  const handleTab = (tab: DiagnosticsTab) => {
    const params = new URLSearchParams(searchParams)
    if (tab === 'stock') {
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
          <h1 className="page-title">诊断工具</h1>
          <div className="page-desc">
            排查个股、字段、数据质量、运行版本与访问问题
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

      {/* 个股调试：并入原 AdminStockDebugPage（支持 symbol 从 query 透传） */}
      {activeTab === 'stock' && <AdminStockDebugPage />}

      {/* 访问统计：并入原 AdminVisitorsPage */}
      {activeTab === 'visitors' && <AdminVisitorsPage />}

      {/* 数据质量 / 运行版本 / 权限诊断：P2 阶段填充 */}
      {(activeTab === 'data-quality' || activeTab === 'versions' || activeTab === 'access') && (
        <div className="card section-gap">
          <div className="card-body">
            <div className="empty-state">
              该诊断能力将在诊断效率与全局搜索阶段（P2）提供
            </div>
            <div className="hint">
              当前阶段先完成信息架构收口，后续接入后端诊断接口与字段链路追踪。
            </div>
          </div>
        </div>
      )}
    </>
  )
}
