// [MarketDashboard][R3A] - 二级导航配置（纯数据 + 纯函数，无 React / DOM 依赖，可单测）
//
// 冻结文案：大盘 / 行业 / 概念 / 对比 N。
// 不得再使用「行业板块 / 概念板块」（旧命名已废弃）。
import { MARKET_DASHBOARD_ROUTES } from './types'

export type ReviewTabKey = 'market' | 'industry' | 'concept' | 'compare'

export interface ReviewTab {
  key: ReviewTabKey
  label: string
  path: string
}

export const REVIEW_TABS: readonly ReviewTab[] = [
  { key: 'market', label: '大盘', path: MARKET_DASHBOARD_ROUTES.market },
  { key: 'industry', label: '行业', path: MARKET_DASHBOARD_ROUTES.industry },
  { key: 'concept', label: '概念', path: MARKET_DASHBOARD_ROUTES.concept },
  { key: 'compare', label: '对比', path: MARKET_DASHBOARD_ROUTES.compare },
]

/** 对比 tab 的 label 携带共享比较篮数量（N = basket count）；其余 tab 固定文案。 */
export function reviewTabLabel(tab: ReviewTab, basketCount: number): string {
  return tab.key === 'compare' ? `${tab.label} ${basketCount}` : tab.label
}
