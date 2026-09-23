// [MarketDashboard][R3A] - 二级导航配置（纯数据 + 纯函数，无 React / DOM 依赖，可单测）
//
// 冻结文案：大盘 / 行业 / 概念（顶层 Review 不再含「对比」tab；
// 对比页仍由 /review/compare 路由可达，入口为各详情页「查看对比」链接）。
// 不得再使用「行业板块 / 概念板块」（旧命名已废弃）。
import { MARKET_DASHBOARD_ROUTES } from './types'

export type ReviewTabKey = 'market' | 'industry' | 'concept'

export interface ReviewTab {
  key: ReviewTabKey
  label: string
  path: string
}

export const REVIEW_TABS: readonly ReviewTab[] = [
  { key: 'market', label: '大盘', path: MARKET_DASHBOARD_ROUTES.market },
  { key: 'industry', label: '行业', path: MARKET_DASHBOARD_ROUTES.industry },
  { key: 'concept', label: '概念', path: MARKET_DASHBOARD_ROUTES.concept },
]

/**
 * 顶层 tab 文案。对比页不再进入顶层 tab（入口为详情页「查看对比」链接），
 * 因此不再需要「对比 N」动态计数标签；此处统一返回固定文案。
 */
export function reviewTabLabel(tab: ReviewTab, _basketCount: number): string {
  return tab.label
}
