// Market React Query hooks owner
//
// [S3-B] 由 useApi.ts 迁出。useApi.ts 以兼容 barrel 重新导出（caller import 零改动）。
//
// 依赖方向：api/market ← useMarketApi ← useApi(barrel)。
// query key / staleTime / placeholderData / refetch 语义与迁移前逐字等价。

import { useQuery } from '@tanstack/react-query'
import * as marketApi from '../api/market'
import type { MarketStocksQueryParams } from '../api/market'

// [S3-B] 窄常量：实时行情 staleTime（30s）。迁移前复用 useApi.ts 的 STALE_REALTIME；
// 此处独立定义，避免为共享一个常量新建 cache-policy 抽象。
const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒

// ============================================================
// ===== Market hooks =====
// ============================================================

/**
 * 查询行情列表（服务端分页 + 批量加载，禁止 N+1）。
 * scope/query/page/page_size/sort 进 URL，selected 独立管理。
 * staleTime 30s（实时行情数据），placeholderData 保留上次成功数据避免闪烁。
 */
export function useMarketStocks(
  params: MarketStocksQueryParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['market-stocks', params],
    queryFn: ({ signal }) => marketApi.getMarketStocks(params, { signal }),
    staleTime: STALE_REALTIME,
    placeholderData: (prev) => prev,
    enabled: options?.enabled ?? true,
  })
}

/**
 * C9: 查询板块目录（只读），供行业/概念筛选下拉/自动完成使用。
 * staleTime 24 小时（板块目录每日 17:00 才同步一次，变更频率极低），
 * 不轮询、不增加持久化缓存。
 */
export function useMarketBoards(type?: 'industry' | 'concept') {
  return useQuery({
    queryKey: ['market-boards', type ?? 'all'],
    queryFn: ({ signal }) => marketApi.getMarketBoards(type ? { type } : undefined, { signal }),
    staleTime: 24 * 60 * 60 * 1000,
  })
}

/**
 * [CHANGE-20260730-013] 查询第一金字塔 99 字段的筛选元数据。
 * staleTime 24 小时（字段元数据变更频率极低，随部署更新）。
 * 用于前端筛选器动态生成类型化控件（enum 下拉、datetime 日期选择器等）。
 */
export function useMarketFilterSpecs() {
  return useQuery({
    queryKey: ['market-filter-specs'],
    queryFn: ({ signal }) => marketApi.getMarketFilterSpecs({ signal }),
    staleTime: 24 * 60 * 60 * 1000,
  })
}

/**
 * [P0-8] 响应式市场状态 hook — 用于详情页行情快照的市场阶段响应式依赖。
 *
 * 开盘、午休结束、hidden 恢复、切股和切周期时立即 invalidate 并刷新。
 * - 轮询 /market/status 每 15s 一次（比 AppShell 30s 更密集，确保阶段切换及时感知）
 * - 返回 market_session 字段，供调用方 useEffect 监听变化触发 invalidateQueries
 * - staleTime=10s 避免过度请求
 */
export function useMarketSessionReactive() {
  return useQuery({
    queryKey: ['market-status', 'reactive'],
    queryFn: () => marketApi.getMarketStatus(),
    staleTime: 10000,
    refetchInterval: 15000,
    refetchIntervalInBackground: true, // 后台也轮询，确保阶段切换及时感知
  })
}
