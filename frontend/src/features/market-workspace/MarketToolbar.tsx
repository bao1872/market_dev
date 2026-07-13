// [MarketToolbar] - 描述: 行情页顶部工具栏（scope 分段按钮 + 顶部搜索框）
// PRD §6.1：行情/自选分段按钮；搜索是 /market 唯一全文搜索入口（单一 keyword 状态真源）。
// 筛选/排序/分页由 StrategyDataTable 内置 UI 承载（URL 状态由 screenerUrlState 管理）。
// 顶部搜索框 keyword 由 MarketWorkspacePage 持有，通过 externalKeyword 注入 StrategyDataTable。
// 提交时机：Enter/失焦提交，清空立即提交（避免逐字符触发 API）。
// 通知/头像由 AppShell 顶栏承载，本组件仅负责 scope + 搜索。
import { useState, useEffect } from 'react'
import clsx from 'clsx'
import type { MarketScope } from './marketWorkspaceUrlState'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
  scope: MarketScope
  onScopeChange: (scope: MarketScope) => void
  // 顶部搜索框受控 keyword（单一真源，由 MarketWorkspacePage 持有并同步到 URL）
  keyword: string
  onKeywordChange: (keyword: string) => void
  // placeholder（缺省时使用默认文案）
  searchPlaceholder?: string
}

export function MarketToolbar({
  scope,
  onScopeChange,
  keyword,
  onKeywordChange,
  searchPlaceholder = '搜索股票代码/名称/拼音首字母',
}: MarketToolbarProps) {
  // 本地输入值：打字时仅更新本地 state，避免逐字符触发 API/URL 写入
  // commit 时机：Enter / 失焦 / 清空（空串立即提交）
  const [inputValue, setInputValue] = useState(keyword)

  // 外部 keyword 变化时（如 URL hydration、preset 应用、清空）同步到本地输入
  useEffect(() => {
    setInputValue(keyword)
  }, [keyword])

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
      <input
        type="search"
        className={styles.searchInput}
        placeholder={searchPlaceholder}
        value={inputValue}
        onChange={(e) => {
          const v = e.target.value
          setInputValue(v)
          // 清空立即提交（空串是明确意图，无需等 Enter/blur）
          if (v === '') onKeywordChange('')
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onKeywordChange(inputValue)
          }
        }}
        onBlur={() => onKeywordChange(inputValue)}
        aria-label="搜索股票"
      />
    </div>
  )
}
