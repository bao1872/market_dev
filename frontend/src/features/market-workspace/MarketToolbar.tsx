// [MarketToolbar] - 描述: 行情页顶部工具栏（仅 scope 分段按钮）
// PRD §6.1：行情/自选分段按钮；搜索/筛选/排序/分页由 StrategyDataTable 内置 UI 承载。
// 筛选器进入 URL（由 StrategyDataTable 管理 sort/dir/keyword/filters/page/page_size）。
// 本组件仅负责 scope 切换；通知/头像由 AppShell 顶栏承载。
import clsx from 'clsx'
import type { MarketScope } from './marketWorkspaceUrlState'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
  scope: MarketScope
  onScopeChange: (scope: MarketScope) => void
}

export function MarketToolbar({
  scope,
  onScopeChange,
}: MarketToolbarProps) {
  return (
    <div className={styles.toolbar}>
      <div className={styles.scopeTabs}>
        <button
          className={clsx(styles.scopeTab, scope === 'watchlist' && styles.scopeTabActive)}
          onClick={() => onScopeChange('watchlist')}
          aria-label="自选"
        >
          自选
        </button>
        <button
          className={clsx(styles.scopeTab, scope === 'market' && styles.scopeTabActive)}
          onClick={() => onScopeChange('market')}
          aria-label="行情"
        >
          行情
        </button>
      </div>
    </div>
  )
}
