// E2E fixture 数据 - 固定 mock 数据，不依赖生产环境
// 所有时间戳使用固定 UTC 值，保证测试可复现
// 股票池：3 只固定股票，覆盖 000001/000002/600519 三种典型标的

export interface FixtureBar {
  trade_date: string
  trade_time: string | null
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number | null
}

export interface FixtureInstrument {
  id: string
  symbol: string
  name: string
  market: string
  status: string
  listing_date: string | null
  created_at: string
  updated_at: string
}

export const FIXTURE_INSTRUMENTS: Record<string, FixtureInstrument> = {
  '000001': {
    id: 'inst-000001',
    symbol: '000001',
    name: '平安银行',
    market: 'SZ',
    status: 'active',
    listing_date: '1991-04-03',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
  '000002': {
    id: 'inst-000002',
    symbol: '000002',
    name: '万科A',
    market: 'SZ',
    status: 'active',
    listing_date: '1991-01-29',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
  '600519': {
    id: 'inst-600519',
    symbol: '600519',
    name: '贵州茅台',
    market: 'SH',
    status: 'active',
    listing_date: '2001-08-27',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
}

// 生成固定 K 线序列（不使用 Math.random，保证可复现）
// 使用确定性公式：close = base + i * step + sin(i) * amplitude
export function buildBars(
  symbol: string,
  timeframe: string,
  count: number,
  opts: { basePrice?: number; lastPartial?: boolean } = {},
): FixtureBar[] {
  const basePrice = opts.basePrice ?? FIXTURE_BASE_PRICE[symbol] ?? 10
  const amplitude = basePrice * 0.02
  const step = basePrice * 0.001
  const bars: FixtureBar[] = []
  // 固定起始日期：2024-06-01（UTC）
  const startMs = Date.UTC(2024, 5, 1)
  const intervalMs = TIMEFRAME_MS[timeframe] ?? TIMEFRAME_MS['1d']
  for (let i = 0; i < count; i++) {
    const t = startMs + i * intervalMs
    const close = Number((basePrice + i * step + Math.sin(i * 0.3) * amplitude).toFixed(2))
    const open = Number((close - step).toFixed(2))
    const high = Number((Math.max(open, close) + amplitude * 0.5).toFixed(2))
    const low = Number((Math.min(open, close) - amplitude * 0.5).toFixed(2))
    const volume = 1000000 + i * 10000
    const isPartial = opts.lastPartial && i === count - 1
    const d = new Date(t)
    bars.push({
      trade_date: d.toISOString().slice(0, 10),
      trade_time: timeframe === '1d' ? null : d.toISOString(),
      open,
      high,
      low,
      close,
      volume,
      amount: volume * close,
    })
    void isPartial
  }
  return bars
}

const FIXTURE_BASE_PRICE: Record<string, number> = {
  '000001': 11.5,
  '000002': 7.2,
  '600519': 1680.0,
}

const TIMEFRAME_MS: Record<string, number> = {
  '15m': 15 * 60 * 1000,
  '1h': 60 * 60 * 1000,
  '1d': 24 * 60 * 60 * 1000,
  '1w': 7 * 24 * 60 * 60 * 1000,
  '1mo': 30 * 24 * 60 * 60 * 1000,
}

// 固定 source_bar_hash（不依赖 bar 内容计算，保证 fixture 稳定）
export const FIXTURE_SOURCE_BAR_HASH = 'fixture-stable-hash-0001'
export const FIXTURE_ADJ_FACTOR_HASH = 'fixture-adj-hash-0001'

export interface FixtureChartSnapshot {
  instrument: FixtureInstrument
  bars: {
    items: FixtureBar[]
    total: number
    is_partial: boolean
    data_source: string
    source_bar_hash: string
    adj_factor_hash: string
    adjustment_as_of: string | null
    completed_through: string | null
    degraded: boolean
    degraded_reason: string | null
  }
  indicators: {
    algorithm_id: string
    algorithm_version: string
    data: Record<string, unknown>
    generated_at: string
    source_bar_hash: string
    adj_factor_hash: string
    adjustment_as_of: string | null
  }
  events: { items: unknown[]; total: number }
  quote: {
    instrument_id: string
    current_price: number | null
    open: number | null
    high: number | null
    low: number | null
    amount: number | null
    change_pct: number | null
    is_realtime: boolean
    source: string
    freshness_seconds: number
    degraded: boolean
    total_market_cap: number | null
    float_market_cap: number | null
    market_cap_as_of: string | null
  }
  snapshot_time: string
  render_frame: {
    matched: boolean
    bars_count: number
    indicators_count: number
    bars_hash: string
    indicators_hash: string
    bars_first_time: string | null
    bars_last_time: string | null
    indicators_first_time: string | null
    indicators_last_time: string | null
    bars_adjustment_as_of: string | null
    indicators_adjustment_as_of: string | null
  }
}

// Node Cluster 指标 fixture（100 行 profile + node_regions_hash）
export function buildNodeClusterIndicators(symbol: string, barsCount: number) {
  const profileRows = Array.from({ length: 100 }, (_, i) => ({
    price_level: Number((i * 0.1).toFixed(2)),
    volume: 1000000 - i * 5000,
    node_type: i % 7 === 0 ? 'peak' : i % 5 === 0 ? 'valley' : 'neutral',
  }))
  return {
    node_cluster: {
      profile_rows: profileRows,
      node_regions_hash: 'fixture-node-regions-hash',
      node_regions: [
        { start: 0, end: 30, type: 'support', strength: 0.8 },
        { start: 60, end: 90, type: 'resistance', strength: 0.7 },
      ],
    },
  }
}

// Bollinger 指标 fixture
export function buildBollingerIndicators(barsCount: number) {
  const upper = Array.from({ length: barsCount }, (_, i) => 12 + i * 0.01)
  const middle = Array.from({ length: barsCount }, (_, i) => 11 + i * 0.005)
  const lower = Array.from({ length: barsCount }, (_, i) => 10 + i * 0.001)
  return {
    bb_monitor: { upper, middle, lower, period: 20, std_dev: 2 },
  }
}

// SMC 指标 fixture
export function buildSmcIndicators(barsCount: number) {
  return {
    smc: {
      algorithm_version: '1.0.0',
      bos: [{ index: 10, type: 'bullish', price: 11.0 }],
      choch: [{ index: 20, type: 'bearish', price: 11.5 }],
      ob: [{ start: 5, end: 8, type: 'bullish', mitigated: false }],
      eqh: [{ index: 15, ref_index: 5, threshold: 0.1 }],
      eql: [{ index: 25, ref_index: 12, threshold: 0.1 }],
    },
  }
}

export function buildChartSnapshot(
  symbol: string,
  timeframe: string,
  indicatorView: 'node_cluster' | 'bollinger' | 'smc' = 'node_cluster',
  barsCount = 250,
): FixtureChartSnapshot {
  const inst = FIXTURE_INSTRUMENTS[symbol] ?? FIXTURE_INSTRUMENTS['000001']
  const bars = buildBars(symbol, timeframe, barsCount, { lastPartial: true })
  const lastClose = bars[bars.length - 1].close
  const prevClose = bars[bars.length - 2].close
  const changePct = Number((((lastClose - prevClose) / prevClose) * 100).toFixed(2))

  let indicatorData: Record<string, unknown>
  let algorithmId: string
  if (indicatorView === 'bollinger') {
    indicatorData = buildBollingerIndicators(barsCount)
    algorithmId = 'bollinger_bands'
  } else if (indicatorView === 'smc') {
    indicatorData = buildSmcIndicators(barsCount)
    algorithmId = 'smc_module'
  } else {
    indicatorData = buildNodeClusterIndicators(symbol, barsCount)
    algorithmId = 'node_cluster'
  }

  return {
    instrument: inst,
    bars: {
      items: bars,
      total: bars.length,
      is_partial: true,
      data_source: 'fixture',
      source_bar_hash: FIXTURE_SOURCE_BAR_HASH,
      adj_factor_hash: FIXTURE_ADJ_FACTOR_HASH,
      adjustment_as_of: '2024-06-01',
      completed_through: bars[bars.length - 2].trade_date,
      degraded: false,
      degraded_reason: null,
    },
    indicators: {
      algorithm_id: algorithmId,
      algorithm_version: '1.0.0',
      data: indicatorData,
      generated_at: '2024-06-01T00:00:00Z',
      source_bar_hash: FIXTURE_SOURCE_BAR_HASH,
      adj_factor_hash: FIXTURE_ADJ_FACTOR_HASH,
      adjustment_as_of: '2024-06-01',
    },
    events: { items: [], total: 0 },
    quote: {
      instrument_id: inst.id,
      current_price: lastClose,
      open: bars[bars.length - 1].open,
      high: bars[bars.length - 1].high,
      low: bars[bars.length - 1].low,
      amount: bars[bars.length - 1].amount,
      change_pct: changePct,
      is_realtime: true,
      source: 'pytdx',
      freshness_seconds: 30,
      degraded: false,
      total_market_cap: null,
      float_market_cap: null,
      market_cap_as_of: null,
    },
    snapshot_time: '2024-06-01T08:30:00Z',
    render_frame: {
      matched: true,
      bars_count: bars.length,
      indicators_count: bars.length,
      bars_hash: FIXTURE_SOURCE_BAR_HASH,
      indicators_hash: FIXTURE_SOURCE_BAR_HASH,
      bars_first_time: bars[0].trade_date,
      bars_last_time: bars[bars.length - 1].trade_date,
      indicators_first_time: bars[0].trade_date,
      indicators_last_time: bars[bars.length - 1].trade_date,
      bars_adjustment_as_of: '2024-06-01',
      indicators_adjustment_as_of: '2024-06-01',
    },
  }
}

// Capture Snapshot 响应（与 chart-snapshot 相似，但加 capture 元数据）
export function buildCaptureSnapshot(
  symbol: string,
  indicatorView: 'node_cluster' | 'bollinger' | 'smc',
  timeframe = '1d',
): FixtureChartSnapshot & {
  capture: { user_id: string; event_id: string; scope: string }
  indicator_view: string
  include_smc: boolean
} {
  const snap = buildChartSnapshot(symbol, timeframe, indicatorView, 250)
  return {
    ...snap,
    capture: {
      user_id: 'fixture-user',
      event_id: 'fixture-event-001',
      scope: 'feishu_capture',
    },
    indicator_view: indicatorView,
    include_smc: indicatorView === 'smc',
  }
}

// Watchlist fixture
export const FIXTURE_WATCHLIST = {
  items: Object.values(FIXTURE_INSTRUMENTS).map((inst, idx) => ({
    id: `wl-${idx}`,
    instrument_id: inst.id,
    instrument_symbol: inst.symbol,
    instrument_name: inst.name,
    instrument_market: inst.market,
    user_id: 'fixture-user',
    strategy_id: 'strategy-watchlist-monitor',
    added_at: '2024-01-01T00:00:00Z',
    sort_order: idx,
    notes: null,
  })),
  total: 3,
}

// Strategy run results fixture（行情筛选列表）
export function buildStrategyRunResults(symbol: string) {
  const inst = FIXTURE_INSTRUMENTS[symbol] ?? FIXTURE_INSTRUMENTS['000001']
  return {
    items: [
      {
        id: `sr-${inst.id}`,
        run_id: 'fixture-run-001',
        strategy_version_id: 'strategy-version-watchlist-monitor',
        instrument_id: inst.id,
        instrument_symbol: inst.symbol,
        instrument_name: inst.name,
        instrument_market: inst.market,
        trade_date: '2024-06-01',
        payload: { signal: 'hold', score: 0.65 },
        created_at: '2024-06-01T08:00:00Z',
        item_status: 'success',
        latest_change_pct: 1.23,
        latest_change_trade_date: '2024-06-01',
      },
    ],
    total: 1,
    page: 1,
    page_size: 50,
    source_total: 1,
    filtered_total: 1,
  }
}

// Access profile fixture（认证后的访问上下文）
export const FIXTURE_ACCESS_PROFILE = {
  user_id: 'fixture-user',
  account_status: 'active',
  roles: ['member'],
  is_admin: false,
  is_member: true,
  subscription_active: true,
  plan_code: 'pro',
  plan_display_name: 'Pro',
  expires_at: '2025-12-31T00:00:00Z',
  features: ['market', 'stock_detail', 'capture'],
  limits: { watchlist: 100, strategies: 10 },
}

// User fixture
export const FIXTURE_USER = {
  id: 'fixture-user',
  email: 'fixture@example.com',
  status: 'active',
  timezone: 'Asia/Shanghai',
  roles: ['member'],
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

// Strategies fixture
export const FIXTURE_STRATEGIES = {
  items: [
    {
      id: 'strategy-watchlist-monitor',
      strategy_key: 'watchlist_monitor',
      kind: 'monitor',
      display_name: '自选监控',
      created_at: '2024-01-01T00:00:00Z',
    },
    {
      id: 'strategy-trend-selection',
      strategy_key: 'trend_selection',
      kind: 'screener',
      display_name: '趋势选股',
      created_at: '2024-01-01T00:00:00Z',
    },
  ],
  total: 2,
}
