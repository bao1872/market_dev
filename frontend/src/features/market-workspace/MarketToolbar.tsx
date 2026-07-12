// [MarketToolbar] - 描述: 行情页顶部工具栏
// PRD §6.1：行情/复盘、全局股票搜索、行情/自选分段按钮、筛选、通知、头像。
// 本组件包含 scope 分段按钮 + 搜索输入 + 行业/概念/状态筛选器；通知/头像由 AppShell 顶栏承载。
// 筛选器进入 URL（可分享、刷新恢复）；筛选变化时重置分页。
// C9: 行业/概念筛选使用板块目录 API + <datalist> 原生自动完成（qstock 同步前为空列表，输入框仍可自由输入）。
import { useState, useEffect, useCallback, useMemo } from 'react'
import clsx from 'clsx'
import type { MarketScope, MarketStateFilter } from './marketWorkspaceUrlState'
import { useMarketBoards } from '@/hooks/useApi'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
  scope: MarketScope
  query: string
  industry: string | null
  concept: string | null
  state: MarketStateFilter
  onScopeChange: (scope: MarketScope) => void
  onQueryChange: (query: string) => void
  onFilterChange: (patch: { industry?: string | null; concept?: string | null; state?: MarketStateFilter }) => void
}

export function MarketToolbar({
  scope,
  query,
  industry,
  concept,
  state,
  onScopeChange,
  onQueryChange,
  onFilterChange,
}: MarketToolbarProps) {
  // 本地输入状态，避免每次按键都触发 URL 更新；Enter 或失焦时提交
  const [input, setInput] = useState(query)
  const [industryInput, setIndustryInput] = useState(industry ?? '')
  const [conceptInput, setConceptInput] = useState(concept ?? '')

  // C9: 板块目录（行业/概念），供 <datalist> 自动完成使用
  // C10 降级保护：available=false 时禁用筛选输入，不制造重复请求
  const boardsQuery = useMarketBoards()
  const boardsAvailable = boardsQuery.data?.available ?? false
  const industryOptions = useMemo(
    () => boardsQuery.data?.items.filter((b) => b.type === 'industry').map((b) => b.name) ?? [],
    [boardsQuery.data],
  )
  const conceptOptions = useMemo(
    () => boardsQuery.data?.items.filter((b) => b.type === 'concept').map((b) => b.name) ?? [],
    [boardsQuery.data],
  )

  useEffect(() => {
    setInput(query)
  }, [query])
  useEffect(() => {
    setIndustryInput(industry ?? '')
  }, [industry])
  useEffect(() => {
    setConceptInput(concept ?? '')
  }, [concept])

  const handleSubmit = useCallback(() => {
    const trimmed = input.trim()
    if (trimmed !== query) {
      onQueryChange(trimmed)
    }
  }, [input, query, onQueryChange])

  const handleIndustrySubmit = useCallback(() => {
    const trimmed = industryInput.trim()
    if (trimmed !== (industry ?? '')) {
      onFilterChange({ industry: trimmed || null })
    }
  }, [industryInput, industry, onFilterChange])

  const handleConceptSubmit = useCallback(() => {
    const trimmed = conceptInput.trim()
    if (trimmed !== (concept ?? '')) {
      onFilterChange({ concept: trimmed || null })
    }
  }, [conceptInput, concept, onFilterChange])

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
      <div className={styles.filters}>
        <input
          className={styles.filterInput}
          type="text"
          placeholder={boardsAvailable ? '行业' : '板块未开放'}
          value={industryInput}
          list="industry-board-options"
          onChange={(e) => setIndustryInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleIndustrySubmit()
          }}
          onBlur={handleIndustrySubmit}
          aria-label="行业筛选"
          disabled={!boardsAvailable}
        />
        <datalist id="industry-board-options">
          {industryOptions.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <input
          className={styles.filterInput}
          type="text"
          placeholder={boardsAvailable ? '概念' : '板块未开放'}
          value={conceptInput}
          list="concept-board-options"
          onChange={(e) => setConceptInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleConceptSubmit()
          }}
          onBlur={handleConceptSubmit}
          aria-label="概念筛选"
          disabled={!boardsAvailable}
        />
        <datalist id="concept-board-options">
          {conceptOptions.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <select
          className={styles.filterSelect}
          value={state ?? ''}
          onChange={(e) => {
            const val = e.target.value
            onFilterChange({ state: (val || null) as MarketStateFilter })
          }}
          aria-label="状态筛选"
        >
          <option value="">全部状态</option>
          <option value="up">上行</option>
          <option value="down">下行</option>
          <option value="sideways">震荡</option>
        </select>
      </div>
    </div>
  )
}
