// [MarketToolbar] - 描述: 行情页顶部工具栏（scope 分段按钮 + 搜索 + 行业/概念筛选）
// PRD §6.1：行情/自选分段按钮；搜索是 /market 唯一全文搜索入口（单一 keyword 状态真源）。
// 工具栏层级：scope → 搜索 → 行业 → 概念（CHANGE-20260713-006）。
// 筛选/排序/分页由 StrategyDataTable 内置 UI 承载（URL 状态由 screenerUrlState 管理）。
//
// CHANGE-20260716-007：行业/概念筛选改用 BoardFilterCombobox（替换原生 datalist）
//  - 行业：关键词模式，输入任意关键词（如 "半导体" / "电子"）命中完整路径任意层级
//  - 概念：精确模式，只提交目录中存在的概念
//  - 行业不再校验精确目录值；placeholder 改为"搜索行业关键词"
//  - 支持键盘导航 / 点击外部关闭 / 清除按钮 / aria-combobox
//
// boards.available=false 时禁用输入，文案"板块数据暂不可用"；
// boards.stale=true 时显示"沿用上次板块数据"提示，控件仍可用。
import { useState, useEffect, useMemo } from 'react'
import clsx from 'clsx'
import type { MarketScope } from './marketWorkspaceUrlState'
import type { MarketBoardItem } from '@/api/endpoints'
import { BoardFilterCombobox } from './BoardFilterCombobox'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
  scope: MarketScope
  onScopeChange: (scope: MarketScope) => void
  // 顶部搜索框受控 keyword（单一真源，由 MarketWorkspacePage 持有并同步到 URL）
  keyword: string
  onKeywordChange: (keyword: string) => void
  // 行业/概念筛选（CHANGE-20260713-006）
  // industry 语义（CHANGE-20260716-007）：行业关键词（不再要求精确完整路径）
  industry: string
  onIndustryChange: (industry: string) => void
  concept: string
  onConceptChange: (concept: string) => void
  // 板块目录（available=false 时禁用输入；stale=true 时显示提示）
  boards:
    | { items: MarketBoardItem[]; available: boolean; stale?: boolean }
    | undefined
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
  // 顶部搜索框本地输入（与 industry/concept 不同，搜索框仍保留本地 state）
  const [keywordInput, setKeywordInput] = useState(keyword)

  // 外部值变化时（URL hydration、preset 应用、清空）同步到本地输入
  useEffect(() => {
    setKeywordInput(keyword)
  }, [keyword])

  const boardsAvailable = boards?.available ?? false
  const boardsStale = boards?.stale ?? false
  const industryOptions = useMemo(
    () => boards?.items.filter((b) => b.type === 'industry') ?? [],
    [boards],
  )
  const conceptOptions = useMemo(
    () => boards?.items.filter((b) => b.type === 'concept') ?? [],
    [boards],
  )

  // placeholder 文案：stale 时显示"沿用上次板块数据"
  // CHANGE-20260716-007：行业 placeholder 改为"搜索行业关键词"
  const industryPlaceholder = !boardsAvailable
    ? '板块数据暂不可用'
    : boardsStale
      ? '搜索行业关键词（沿用上次板块数据）'
      : '搜索行业关键词'
  const conceptPlaceholder = !boardsAvailable
    ? '板块数据暂不可用'
    : boardsStale
      ? '概念（沿用上次板块数据）'
      : '概念'

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
        value={keywordInput}
        onChange={(e) => {
          const v = e.target.value
          setKeywordInput(v)
          // 清空立即提交（空串是明确意图，无需等 Enter/blur）
          if (v === '') onKeywordChange('')
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onKeywordChange(keywordInput)
          }
        }}
        onBlur={() => onKeywordChange(keywordInput)}
        aria-label="搜索股票"
      />
      <BoardFilterCombobox
        value={industry}
        onChange={onIndustryChange}
        options={industryOptions}
        mode="industry"
        placeholder={industryPlaceholder}
        disabled={!boardsAvailable}
        ariaLabel="行业筛选"
      />
      <BoardFilterCombobox
        value={concept}
        onChange={onConceptChange}
        options={conceptOptions}
        mode="concept"
        placeholder={conceptPlaceholder}
        disabled={!boardsAvailable}
        ariaLabel="概念筛选"
      />
    </div>
  )
}
