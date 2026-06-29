// [趋势选股] - 共享模块类型定义
// 职责：定义首页与趋势选股页统一使用的数据行模型
// 设计：保留原始 payload 供列渲染动态计算（服务端排序/筛选需要原始 key），
//       同时包含 instrument 级字段（避免 N+1 查询）和 watched 状态（主页操作列需要）

/**
 * 趋势选股统一行类型
 * - resultId: 结果 ID（ScreenerPage 用作 rowKey）
 * - instrumentId: 标的 ID（IndexPage 用作 rowKey + 加入自选）
 * - payload: 原始 payload（列渲染动态计算 + 服务端 metric_filters 透传）
 * - watched: 是否已自选（主页操作列根据此字段切换"已自选"标签/"+ 自选"按钮）
 */
export interface TrendSelectionRow {
  resultId: string
  instrumentId: string
  symbol: string
  name: string
  market: string
  payload: Record<string, unknown>
  watched: boolean
  [key: string]: unknown
}
