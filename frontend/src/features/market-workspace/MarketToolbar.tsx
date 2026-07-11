// [MarketToolbar] - 描述: 行情页顶部工具栏
// PRD §6.1：行情/复盘、全局股票搜索、行情/自选分段按钮、筛选、通知、头像。
// 本组件只包含 scope 分段按钮 + 搜索输入；通知/头像由 AppShell 顶栏承载。
import { useState, useEffect, useCallback } from 'react'
import clsx from 'clsx'
import type { MarketScope } from './marketWorkspaceUrlState'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
  scope: MarketScope
  query: string
  onScopeChange: (scope: MarketScope) => void
  onQueryChange: (query: string) => void
}

export function MarketToolbar({ scope, query, onScopeChange, onQueryChange }: MarketToolbarProps) {
  // 本地输入状态，避免每次按键都触发 URL 更新；Enter 或失焦时提交
  const [input, setInput] = useState(query)

  useEffect(() => {
    setInput(query)
  }, [query])

  const handleSubmit = useCallback(() => {
    const trimmed = input.trim()
    if (trimmed !== query) {
      onQueryChange(trimmed)
    }
  }, [input, query, onQueryChange])

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
        className={styles.searchInput}
        type="text"
        placeholder="搜索股票代码/名称/拼音首字母"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') handleSubmit()
        }}
        onBlur={handleSubmit}
        aria-label="股票搜索"
      />
    </div>
  )
}
