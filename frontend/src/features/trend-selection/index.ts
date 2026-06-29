// [趋势选股] - 共享模块入口
// 职责：统一对外导出趋势选股的行类型、adapter、列定义、visibleColumnKeys 辅助函数
// 唯一性：spec 第七节要求 IndexPage 与 ScreenerPage 必须通过此模块引用列定义
export type { TrendSelectionRow } from './types'
export {
  adaptStrategyResultToTrendRow,
  pickPayload,
  toNum,
  fmtNum,
  fmtPct,
  fmtRatioAsPct,
  fmtChange,
  changePctColorClass,
  getStockDisplay,
  DIR_BARS_KEYS,
  VWAP_RET_AVG_KEYS,
  VWAP_RET_TOTAL_KEYS,
  OFFSET_MEAN_KEYS,
  OFFSET_PERCENTILE_KEYS,
} from './adapters'
export { getTrendSelectionColumns, type TrendSelectionColumnOptions } from './columns'
export { INDEX_VISIBLE_COLUMN_KEYS, visibleColumnKeys } from './config'
