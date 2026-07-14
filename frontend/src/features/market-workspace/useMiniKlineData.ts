// CHANGE-20260713-010: 右栏小 K 线数据 hook
// 薄封装 useInstrumentBySymbol + useBars，按 timeframe 提供 1d/1w/1mo 小 K 线数据。
// 面板收起时不挂载（由父组件控制），展开并选中股票后只请求当前周期；
// 切换周期后使用 React Query 缓存，不一次预取三周期。
import { useInstrumentBySymbol, useBars } from '@/hooks/useApi'

export type MiniKlineTimeframe = '1d' | '1w' | '1mo'

const BARS_COUNT: Record<MiniKlineTimeframe, number> = {
  '1d': 80,
  '1w': 60,
  '1mo': 48,
}

export interface MiniKlineData {
  instrumentId: string | undefined
  bars: Array<{ time: string; open: number; high: number; low: number; close: number }>
  isLoading: boolean
  isError: boolean
}

export function useMiniKlineData(symbol: string | null, timeframe: MiniKlineTimeframe): MiniKlineData {
  const instrumentQuery = useInstrumentBySymbol(symbol ?? '')
  const instrumentId = instrumentQuery.data?.id
  const barsCount = BARS_COUNT[timeframe]

  const barsQuery = useBars(instrumentId, {
    timeframe,
    adj: 'qfq',
    page_size: barsCount,
  }, {
    refetchInterval: false, // 小 K 线不轮询
  })

  const bars = (barsQuery.data?.items ?? []).map((b) => ({
    time: b.trade_date ?? '',
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
  })).filter((b) => b.time !== '')

  return {
    instrumentId,
    bars,
    isLoading: instrumentQuery.isLoading || barsQuery.isLoading,
    isError: instrumentQuery.isError || barsQuery.isError,
  }
}
