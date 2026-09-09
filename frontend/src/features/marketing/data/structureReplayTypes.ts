// 中际旭创真实结构回放的 frozen JSON 类型（V1.2）。
// 这是一次性 canonical export 的产物 schema，前端只读静态 JSON，不请求公开行情 API。
// SMC frame DTO 来自生产 canonical CanonicalComputationService.compute(algorithm_id='smc')。
import type { IndicatorResponse } from '@/api/endpoints'

export interface MarketingReplayBar {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface MarketingStructureReplay {
  schemaVersion: 1
  instrument: {
    symbol: string
    name: string
  }
  timeframe: '1d'
  adj: 'qfq'
  firstVisibleDate: string
  lastVisibleDate: string
  generatedAt: string
  provenance: {
    source: string
    gitSha: string
  }
  bars: MarketingReplayBar[]
  smcLayer: IndicatorResponse['layers'][number]
  frames: {
    endIndex: number
    endTime: string
    smc: Record<string, unknown>
  }[]
}