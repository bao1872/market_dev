// CHANGE-20260713-010: 右栏小 K 线数据 hook
// CHANGE-20260714-001: 扩展为五周期（15m/1h/1d/1w/1mo），intraday 使用 trade_time 转 UTCTimestamp
// 薄封装 useInstrumentBySymbol + useBars，按 timeframe 提供小 K 线数据。
// 面板收起时不挂载（由父组件控制），展开并选中股票后只请求当前周期；
// 切换周期后使用 React Query 缓存，不一次预取五周期。
import { useInstrumentBySymbol, useBars } from '@/hooks/useApi'

export type MiniKlineTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

// 五周期数量契约：15m=120, 1h=100, 1d=80, 1w=60, 1mo=48
const BARS_COUNT: Record<MiniKlineTimeframe, number> = {
  '15m': 120,
  '1h': 100,
  '1d': 80,
  '1w': 60,
  '1mo': 48,
}

// intraday 周期使用 trade_time（转 UTCTimestamp 秒），日周月使用 trade_date
const INTRADAY_TIMEFRAMES: ReadonlySet<MiniKlineTimeframe> = new Set(['15m', '1h'])

export interface MiniKlineData {
  instrumentId: string | undefined
  bars: Array<{ time: string | number; open: number; high: number; low: number; close: number }>
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

  const isIntraday = INTRADAY_TIMEFRAMES.has(timeframe)
  const bars = (barsQuery.data?.items ?? []).map((b) => {
    let time: string | number = ''
    if (isIntraday && b.trade_time) {
      // 后端返回 aware datetime(+08:00)，new Date 正确解析为 UTC 时刻
      const ts = new Date(b.trade_time).getTime() / 1000
      if (Number.isFinite(ts)) time = ts
    } else if (b.trade_date) {
      time = b.trade_date
    }
    return { time, open: b.open, high: b.high, low: b.low, close: b.close }
  }).filter((b) => b.time !== '')

  return {
    instrumentId,
    bars,
    isLoading: instrumentQuery.isLoading || barsQuery.isLoading,
    isError: instrumentQuery.isError || barsQuery.isError,
  }
}
