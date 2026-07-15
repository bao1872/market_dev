// [MarketToolbar] - 描述: 行情页顶部工具栏（scope 分段按钮 + 搜索 + 行业/概念筛选）
// PRD §6.1：行情/自选分段按钮；搜索是 /market 唯一全文搜索入口（单一 keyword 状态真源）。
// 工具栏层级：scope → 搜索 → 行业 → 概念（CHANGE-20260713-006）。
// 筛选/排序/分页由 StrategyDataTable 内置 UI 承载（URL 状态由 screenerUrlState 管理）。
// boards.available=false 时行业/概念输入禁用，文案"板块数据暂不可用"；available=true 时使用 datalist 候选。
// 通知/头像由 AppShell 顶栏承载，本组件仅负责 scope + 搜索 + 板块筛选。
import { useState, useEffect } from 'react'
import clsx from 'clsx'
import type { MarketScope } from './marketWorkspaceUrlState'
import type { MarketBoardItem } from '@/api/endpoints'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
  scope: MarketScope
  onScopeChange: (scope: MarketScope) => void
  // 顶部搜索框受控 keyword（单一真源，由 MarketWorkspacePage 持有并同步到 URL）
  keyword: string
  onKeywordChange: (keyword: string) => void
  // 行业/概念筛选（CHANGE-20260713-006）
  industry: string
  onIndustryChange: (industry: string) => void
  concept: string
  onConceptChange: (concept: string) => void
  // 板块目录（available=false 时禁用输入）
  boards: { items: MarketBoardItem[]; available: boolean } | undefined
  // placeholder（缺省时使用默认文案）
  searchPlaceholder?: string
}

export function MarketToolbar({
  scope,
  onScopeChange,
  keyword,
  onKeywordChange,
  industry,
  onIndustryChange,
  concept,
  onConceptChange,
  boards,
  searchPlaceholder = '搜索股票代码/名称/拼音首字母',
}: MarketToolbarProps) {
  // 本地输入值：打字时仅更新本地 state，避免逐字符触发 API/URL 写入
  // commit 时机：Enter / 失焦 / 清空（空串立即提交）
  const [inputValue, setInputValue] = useState(keyword)

  // 外部 keyword 变化时（如 URL hydration、preset 应用、清空）同步到本地输入
  useEffect(() => {
    setInputValue(keyword)
  }, [keyword])

  const boardsAvailable = boards?.available ?? false
  const industryOptions = boards?.items.filter(b => b.type === 'industry') ?? []
  const conceptOptions = boards?.items.filter(b => b.type === 'concept') ?? []

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
      <input
        type="search"
        className={styles.filterInput}
        list="industry-options"
        placeholder={boardsAvailable ? '行业' : '板块数据暂不可用'}
        value={industry}
        onChange={(e) => onIndustryChange(e.target.value)}
        disabled={!boardsAvailable}
        aria-label="行业筛选"
      />
      <datalist id="industry-options">
        {industryOptions.map(b => (
          <option key={b.id} value={b.name} />
        ))}
      </datalist>
      <input
        type="search"
        className={styles.filterInput}
        list="concept-options"
        placeholder={boardsAvailable ? '概念' : '板块数据暂不可用'}
        value={concept}
        onChange={(e) => onConceptChange(e.target.value)}
        disabled={!boardsAvailable}
        aria-label="概念筛选"
      />
      <datalist id="concept-options">
        {conceptOptions.map(b => (
          <option key={b.id} value={b.name} />
        ))}
      </datalist>
    </div>
  )
}
