// [CHANGE-20260728-010 P0 修复] Capture 组合视图 Ready 纯函数
//
// 提取自 CaptureStockPage.tsx 的 computeCombinedReady，作为可独立测试的纯函数。
//
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/captureReady.test.ts
//
// Ready 合同（与后端 backend/app/services/smc_view_adapter.py 对齐）：
//   - Node Ready：profile_rows 为非空数组 + node_regions_hash 或 profile_hash 为非空字符串 + node_regions 为数组
//   - SMC Ready：smc 存在；events 与 order_blocks 为数组（允许空）；swing_bias 为有限 number（1/-1/0）；
//     params 为非 null object
//
// 关键不变量：swing_bias 是 number，不是数组。
//   后端 smc_view_adapter.py:57 明确："swing_bias: int 透传 core 的 swing_trend.bias（1/-1/0）"
//   旧实现错误地要求 Array.isArray(swing_bias)，导致组合截图永远无法 Ready。

import type { IndicatorResponse } from '@/api/endpoints'

/**
 * 判断 Capture 组合视图是否 Ready。
 *
 * 组合视图 = Node Cluster + SMC（结构 + 筹码共识）。
 * 基础 Ready（bars 存在 + indicators 存在 + frame matched）由调用方检查，
 * 本函数只检查组合视图的额外条件。
 *
 * @param indicators 后端 /capture/stocks/{id}/snapshot 返回的 indicators 字段
 * @returns true 表示 Node + SMC 合同均满足
 */
export function computeCombinedReady(indicators: IndicatorResponse | undefined): boolean {
  if (!indicators?.data) return false
  const data = indicators.data as Record<string, unknown>

  // ===== Node Ready =====
  // 兼容旧命名空间：node_cluster > watchlist_monitor > volume_node_monitor
  const vn = (data['node_cluster'] ?? data['watchlist_monitor'] ?? data['volume_node_monitor']) as
    | Record<string, unknown>
    | undefined
  if (!vn) return false
  const profileRows = vn.profile_rows
  const nodeRegionsHash = vn.node_regions_hash
  const profileHash = vn.profile_hash
  const nodeRegions = vn.node_regions
  const hasHash =
    (typeof nodeRegionsHash === 'string' && nodeRegionsHash.length > 0) ||
    (typeof profileHash === 'string' && profileHash.length > 0)
  const nodeReady =
    Array.isArray(profileRows) && profileRows.length > 0 &&
    hasHash &&
    Array.isArray(nodeRegions)
  if (!nodeReady) return false

  // ===== SMC Contract Ready =====
  // DTO 结构必须存在；数组允许为空（无事件时不能导致永久 loading）
  // 实际 API 返回扁平结构：events/order_blocks/equal_highs_lows/trailing/swing_bias/pivots/params/view
  // 注意：swing_bias 是 number（1/-1/0），不是数组。后端 smc_view_adapter.py:57 明确声明。
  const smc = data['smc'] as Record<string, unknown> | undefined
  if (!smc) return false
  const events = smc.events
  const orderBlocks = smc.order_blocks
  const swingBias = smc.swing_bias
  const params = smc.params
  const hasSmcStructure =
    Array.isArray(events) &&
    Array.isArray(orderBlocks) &&
    typeof swingBias === 'number' && Number.isFinite(swingBias) &&
    typeof params === 'object' && params !== null
  return hasSmcStructure
}
