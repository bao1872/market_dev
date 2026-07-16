// [MarketToolbar] - 描述: 行情页顶部工具栏（scope 分段按钮 + 搜索 + 行业/概念筛选）
// PRD §6.1：行情/自选分段按钮；搜索是 /market 唯一全文搜索入口（单一 keyword 状态真源）。
// 工具栏层级：scope → 搜索 → 行业 → 概念（CHANGE-20260713-006）。
// 筛选/排序/分页由 StrategyDataTable 内置 UI 承载（URL 状态由 screenerUrlState 管理）。
// boards.available=false 时行业/概念输入禁用，文案"板块数据暂不可用"；available=true 时使用 datalist 候选。
// boards.stale=true 时显示"沿用上次板块数据"提示，控件仍可用。
// 行业/概念各用本地输入，Enter/blur/选中时才提交，只接受当前目录精确值；清空立即提交并重置 page=1。
// 行业显示可将 `-` 渲染为 `/`，API 值不变。
// 通知/头像由 AppShell 顶栏承载，本组件仅负责 scope + 搜索 + 板块筛选。
import { useState, useEffect, useMemo } from 'react'
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
  // 板块目录（available=false 时禁用输入；stale=true 时显示提示）
  boards:
    | { items: MarketBoardItem[]; available: boolean; stale?: boolean }
    | undefined
  // placeholder（缺省时使用默认文案）
  searchPlaceholder?: string
}

/** 将行业路径中的 `-` 渲染为 `/`（仅显示，API 值不变） */
function displayIndustryName(name: string): string {
  return name.replace(/-/g, '/')
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
  const [keywordInput, setKeywordInput] = useState(keyword)
  const [industryInput, setIndustryInput] = useState(industry)
  const [conceptInput, setConceptInput] = useState(concept)

  // 外部值变化时（如 URL hydration、preset 应用、清空）同步到本地输入
  useEffect(() => {
    setKeywordInput(keyword)
  }, [keyword])
  useEffect(() => {
    setIndustryInput(industry)
  }, [industry])
  useEffect(() => {
    setConceptInput(concept)
  }, [concept])

  const boardsAvailable = boards?.available ?? false
  const boardsStale = boards?.stale ?? false
  const industryOptions = useMemo(
    () => boards?.items.filter(b => b.type === 'industry') ?? [],
    [boards],
  )
  const conceptOptions = useMemo(
    () => boards?.items.filter(b => b.type === 'concept') ?? [],
    [boards],
  )

  // 行业精确值集合（用于校验输入是否为当前目录中的有效值）
  const industryNameSet = useMemo(
    () => new Set(industryOptions.map(b => b.name)),
    [industryOptions],
  )
  const conceptNameSet = useMemo(
    () => new Set(conceptOptions.map(b => b.name)),
    [conceptOptions],
  )

  // 提交行业筛选：只接受当前目录精确值或空值
  const commitIndustry = (value: string) => {
    const trimmed = value.trim()
    if (trimmed === '' || industryNameSet.has(trimmed)) {
      onIndustryChange(trimmed)
    } else {
      // 无效文本：重置为当前已提交值
      setIndustryInput(industry)
    }
  }

  // 提交概念筛选：只接受当前目录精确值或空值
  const commitConcept = (value: string) => {
    const trimmed = value.trim()
    if (trimmed === '' || conceptNameSet.has(trimmed)) {
      onConceptChange(trimmed)
    } else {
      // 无效文本：重置为当前已提交值
      setConceptInput(concept)
    }
  }

  // placeholder 文案：stale 时显示"沿用上次板块数据"
  const industryPlaceholder = !boardsAvailable
    ? '板块数据暂不可用'
    : boardsStale
      ? '行业（沿用上次板块数据）'
      : '行业'
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
      <input
        type="search"
        className={styles.filterInput}
        list="industry-options"
        placeholder={industryPlaceholder}
        value={industryInput}
        onChange={(e) => {
          const v = e.target.value
          setIndustryInput(v)
          // 清空立即提交并重置 page=1
          if (v === '') onIndustryChange('')
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commitIndustry(industryInput)
          }
        }}
        onBlur={() => commitIndustry(industryInput)}
        disabled={!boardsAvailable}
        aria-label="行业筛选"
      />
      <datalist id="industry-options">
        {industryOptions.map(b => (
          <option key={b.id} value={b.name}>
            {displayIndustryName(b.name)}
          </option>
        ))}
      </datalist>
      <input
        type="search"
        className={styles.filterInput}
        list="concept-options"
        placeholder={conceptPlaceholder}
        value={conceptInput}
        onChange={(e) => {
          const v = e.target.value
          setConceptInput(v)
          // 清空立即提交并重置 page=1
          if (v === '') onConceptChange('')
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commitConcept(conceptInput)
          }
        }}
        onBlur={() => commitConcept(conceptInput)}
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
