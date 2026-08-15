// [MarketToolbar] - 描述: 行情页顶部工具栏（搜索 + 行业/概念筛选）
// [Round 2026-07-28-4] 移除 scope 分段按钮（行情/自选切换已上移到一级导航）
// 工具栏层级：搜索 → 行业 → 概念（CHANGE-20260713-006）
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
import { useMemo } from 'react'
import type { MarketBoardItem } from '@/api/endpoints'
import { BoardFilterCombobox } from './BoardFilterCombobox'
import styles from './MarketWorkspace.module.scss'

interface MarketToolbarProps {
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
}

export function MarketToolbar({
  industry,
  onIndustryChange,
  concept,
  onConceptChange,
  boards,
}: MarketToolbarProps) {
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
