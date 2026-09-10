// 近岸蛋白真实筹码共识回放 frozen JSON 类型（V1）。
// 一次性 canonical export 产物 schema；前端只读静态 JSON，不请求公开行情 API。
// frame.node 来自生产 CanonicalComputationService.compute(algorithm_id='node_cluster')，
// 本文件只声明形状，不复制任何算法。
import type { IndicatorResponse } from '@/api/endpoints'
import type { MarketingReplayBar } from './structureReplayTypes'

/** 生产 node DTO 的「节点区域」（node_regions 元素）。 */
export interface NodeRegion {
  entity_id: string
  kind: string
  low: number
  mid: number
  high: number
  bullish_volume: number
  bearish_volume: number
  total_volume: number
  is_poc: boolean
}

/** 价格档位快照（profile_rows 元素，100 行）。 */
export interface ProfileRow {
  price_low: number
  price_high: number
  price_mid: number
  bullish_volume: number
  bearish_volume: number
  total_volume: number
  is_peak: boolean
  is_poc: boolean
  is_value_area: boolean
}

export interface ChipConsensusFrameSummary {
  /** 主要成交密集价（= profile_meta.poc_price，只提取不重算）。 */
  primaryConsensusPrice: number | null
  consensusLow: number | null
  consensusMid: number | null
  consensusHigh: number | null
  bullishVolume: number | null
  bearishVolume: number | null
  totalVolume: number | null
}

/** frame.node 的生产 DTO（详情链同款序列化；本类型只声明前端消费的子集）。 */
export interface NodeClusterDto {
  availability: string
  degraded_reason: string | null
  profile_rows: ProfileRow[]
  profile_meta: {
    row_count: number
    price_step: number | null
    poc_price: number | null
    vah_price: number | null
    val_price: number | null
    [key: string]: unknown
  }
  peak_rows: Array<{
    price_mid: number
    bullish_volume: number
    bearish_volume: number
    total_volume: number
    is_peak: boolean
  }>
  node_regions: NodeRegion[]
  state: {
    current_price: number
    position_0_1: number
    poc_price: { price_low: number; price_mid: number; price_high: number } | null
    [key: string]: unknown
  }
  [key: string]: unknown
}

export interface ChipConsensusReplayFrame {
  endIndex: number
  endTime: string
  node: NodeClusterDto
  summary: ChipConsensusFrameSummary
}

export interface MarketingChipConsensusReplay {
  schemaVersion: 1
  instrument: {
    symbol: string
    name: string
    exchange: string
  }
  timeframe: '1d'
  adj: 'qfq'
  firstVisibleDate: string
  lastVisibleDate: string
  generatedAt: string
  provenance: {
    algorithmId: string
    source: string
    availability: string
    gitSha: string
  }
  bars: MarketingReplayBar[]
  /** 筹码共识图层显示 preset（ChartLayer，复用生产图表契约；同 structure smcLayer 做法）。 */
  nodeLayer: IndicatorResponse['layers'][number]
  frames: ChipConsensusReplayFrame[]
}
