// [SLICE 4 / PRICE] 涨跌幅分析 typed owner（唯一解析 + 单位 owner）。
//
// 硬契约：
// - 解析 owner 是本文件；ScopePriceAnalysisPanel 只 render typed VM。
// - 禁止前端重算：EW/AW/rolling mean/variance/std/zscore/Capital Tilt(AW-EW)/migration。
// - Capital Tilt 必须消费 persisted Composition fact（internal_structure_facts
//   .capital_tilt.capital_tilt），绝不 AW - EW。
// - Dynamics（Position/Velocity/Acceleration）只做输入适配，绝不重算算法。
// - 单位变换只在本文件的 typed formatter 内；React component 不做业务单位换算。
// - 缺失 = null，保留 date slot；null 绝不 0、不插值、不 forward-fill。
import {
  NULL_DISPLAY,
  formatNumberNullable,
  formatPercentNullable,
  formatRawDimensionlessNullable,
  formatZScoreNullable,
} from './reviewFormat'
import type {
  ReviewCrossSectionDTO,
  ReviewScopeHistoryDTO,
  ReviewScopeHistoryFieldDTO,
  ReviewScopePriceHistoryDTO,
} from './types'

type Json = Record<string, unknown>

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function deepGet(root: unknown, path: readonly string[]): unknown {
  let node: unknown = root
  for (const key of path) {
    const o = asRecord(node)
    if (!o || !(key in o)) return undefined
    node = o[key]
  }
  return node
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function numArray(v: unknown): Array<number | null> {
  if (!Array.isArray(v)) return []
  return v.map((x) => num(x))
}

// ---------------------------------------------------------------------------
// 单位（§四）—— 全部 typed formatter，绝不业务重算
// ---------------------------------------------------------------------------

/**
 * 收益型标量（EW / AW / mean / std）canonical = decimal return：
 * 0.0123 → "1.23%"。
 */
export function formatDecimalReturn(value: number | null | undefined, digits = 2): string {
  return formatPercentNullable(value, digits)
}

/**
 * variance canonical = decimal-return² → percentage-point²（%²）。
 *
 * 0.0004 × 10000 = "4.00 %²"。
 * 这是纯单位变换（不是重新计算 variance）：
 *  - 不是 0.04（少乘一次 100）
 *  - 不是 0.0004（未换算）
 */
export function formatReturnVariancePctSquared(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NULL_DISPLAY
  return `${(value * 10000).toFixed(digits)} %²`
}

/** Z：原始 z，无 "%" */
export function formatReturnZScore(value: number | null | undefined): string {
  return formatZScoreNullable(value)
}

/** 比率（breadth advance/decline/unchanged）→ 百分比 */
export function formatRatioPct(value: number | null | undefined, digits = 1): string {
  return formatPercentNullable(value, digits)
}

/** return_dispersion：canonical unit = None（无量纲原始标量），绝不 ×100 */
export function formatReturnDispersion(value: number | null | undefined): string {
  return formatRawDimensionlessNullable(value)
}

// ---------------------------------------------------------------------------
// Current（canonical Observation.price）
// ---------------------------------------------------------------------------

export interface PriceCurrentVM {
  equalWeightReturn: number | null
  amountWeightedReturn: number | null
  advanceRatio: number | null
  declineRatio: number | null
  unchangedRatio: number | null
  returnDispersion: number | null
  // 展示串（单位由本 owner 决定）
  equalWeightReturnText: string
  amountWeightedReturnText: string
  advanceRatioText: string
  declineRatioText: string
  unchangedRatioText: string
  returnDispersionText: string
}

export function parsePriceCurrent(observation: Json | null | undefined): PriceCurrentVM {
  const ew = num(deepGet(observation, ['price', 'equal_weight_return']))
  const aw = num(deepGet(observation, ['price', 'amount_weighted_return']))
  const adv = num(deepGet(observation, ['price', 'breadth', 'advance_ratio']))
  const dec = num(deepGet(observation, ['price', 'breadth', 'decline_ratio']))
  const unc = num(deepGet(observation, ['price', 'breadth', 'unchanged_ratio']))
  const disp = num(deepGet(observation, ['price', 'return_dispersion']))
  return {
    equalWeightReturn: ew,
    amountWeightedReturn: aw,
    advanceRatio: adv,
    declineRatio: dec,
    unchangedRatio: unc,
    returnDispersion: disp,
    equalWeightReturnText: formatDecimalReturn(ew),
    amountWeightedReturnText: formatDecimalReturn(aw),
    advanceRatioText: formatRatioPct(adv),
    declineRatioText: formatRatioPct(dec),
    unchangedRatioText: formatRatioPct(unc),
    returnDispersionText: formatReturnDispersion(disp),
  }
}

// ---------------------------------------------------------------------------
// Rolling（history.fields.* 直出；rolling 由 backend owner 计算）
// ---------------------------------------------------------------------------

export interface PriceRollingVM {
  /** 最后一个日期槽 = current（绝不另取一处 current 事实） */
  current: number | null
  mean20: number | null
  variance20: number | null
  std20: number | null
  zscore20: number | null
  baselineCount: number | null
  // 展示串
  currentText: string
  mean20Text: string
  variance20Text: string
  std20Text: string
  zscore20Text: string
}

function lastOrNull(arr: Array<number | null>): number | null {
  return arr.length === 0 ? null : arr[arr.length - 1]
}

export function parsePriceRolling(field: ReviewScopeHistoryFieldDTO | null | undefined): PriceRollingVM | null {
  if (!field) return null
  const series = numArray(field.series)
  const varianceArr = numArray(field.variance20)
  const stdArr = numArray(field.std20)
  const meanArr = numArray(field.mean20)
  const zArr = numArray(field.zscore20)
  const bArr = field.baselineCount ?? []
  const current = lastOrNull(series)
  const mean20 = lastOrNull(meanArr)
  const variance20 = lastOrNull(varianceArr)
  const std20 = lastOrNull(stdArr)
  const zscore20 = lastOrNull(zArr)
  return {
    current,
    mean20,
    variance20,
    std20,
    zscore20,
    baselineCount: bArr.length === 0 ? null : (bArr[bArr.length - 1] ?? null),
    currentText: formatDecimalReturn(current),
    mean20Text: formatDecimalReturn(mean20),
    variance20Text: formatReturnVariancePctSquared(variance20),
    std20Text: formatDecimalReturn(std20),
    zscore20Text: formatReturnZScore(zscore20),
  }
}

// ---------------------------------------------------------------------------
// EW main chart（EW Raw + lagged Mean ± 1σ）
// ---------------------------------------------------------------------------

export interface PriceEwChartVM {
  dates: string[]
  ew: Array<number | null>
  mean: Array<number | null>
  /** Mean + Std（绝不 Mean + Variance） */
  upperBand: Array<number | null>
  /** Mean - Std */
  lowerBand: Array<number | null>
}

export function buildPriceEwChart(
  dates: string[],
  ewField: ReviewScopeHistoryFieldDTO | null | undefined,
): PriceEwChartVM {
  if (!ewField) return { dates, ew: [], mean: [], upperBand: [], lowerBand: [] }
  const ew = numArray(ewField.series)
  const mean = numArray(ewField.mean20)
  const std = numArray(ewField.std20)
  const upper = mean.map((m, i) => (m === null || std[i] === null ? null : m + (std[i] as number)))
  const lower = mean.map((m, i) => (m === null || std[i] === null ? null : m - (std[i] as number)))
  return { dates, ew, mean, upperBand: upper, lowerBand: lower }
}

// ---------------------------------------------------------------------------
// EW / AW 双线 + Breadth composition + Dispersion
// ---------------------------------------------------------------------------

export interface PriceSeriesVM {
  dates: string[]
  values: Array<number | null>
}

export function parsePriceSeries(
  dates: string[],
  field: ReviewScopeHistoryFieldDTO | null | undefined,
): PriceSeriesVM {
  return { dates, values: field ? numArray(field.series) : [] }
}

export interface BreadthPointVM {
  date: string
  advance: number | null
  decline: number | null
  unchanged: number | null
}

export interface PriceBreadthVM {
  dates: string[]
  points: BreadthPointVM[]
}

export function parsePriceBreadth(
  dates: string[],
  advance: ReviewScopeHistoryFieldDTO | null | undefined,
  decline: ReviewScopeHistoryFieldDTO | null | undefined,
  unchanged: ReviewScopeHistoryFieldDTO | null | undefined,
): PriceBreadthVM {
  const a = advance ? numArray(advance.series) : []
  const d = decline ? numArray(decline.series) : []
  const u = unchanged ? numArray(unchanged.series) : []
  return {
    dates,
    points: dates.map((date, i) => ({
      date,
      advance: a[i] ?? null,
      decline: d[i] ?? null,
      unchanged: u[i] ?? null,
    })),
  }
}

// ---------------------------------------------------------------------------
// Capital Tilt（persisted Composition fact，绝不 AW - EW）
// ---------------------------------------------------------------------------

export interface CapitalTiltVM {
  dates: string[]
  /** 每日 persisted capital_tilt（verbatim direct projection） */
  values: Array<number | null>
  /** 当前 persisted capital_tilt */
  current: number | null
  currentText: string
  /** 参考展示：persisted EW / AW（仅展示，绝不用于推导 tilt） */
  currentEwText: string
  currentAwText: string
}

export function parseCapitalTilt(
  dates: string[],
  priceHistory: ReviewScopePriceHistoryDTO | null | undefined,
  currentTilt: number | null,
  currentEw: number | null,
  currentAw: number | null,
): CapitalTiltVM {
  return {
    dates,
    values: priceHistory ? numArray(priceHistory.capital_tilt) : [],
    current: currentTilt,
    currentText: formatDecimalReturn(currentTilt),
    currentEwText: formatDecimalReturn(currentEw),
    currentAwText: formatDecimalReturn(currentAw),
  }
}

// ---------------------------------------------------------------------------
// Leadership（persisted Composition.leadership 窄投影，verbatim）
// ---------------------------------------------------------------------------

export interface LeadershipPointVM {
  date: string
  status: string | null
  reason: string | null
  jaccardStability: number | null
  migration: number | null
  currentLeaderCount: number | null
  /** [] = 空 leader set（真实事实）；null = 缺失。两者不得合并 */
  currentLeaderIds: string[] | null
  /** unavailable 时展示 unavailable + reason，绝不吞成 "—" */
  unavailable: boolean
}

export interface LeadershipHistoryVM {
  dates: string[]
  points: LeadershipPointVM[]
}

export function parseLeadershipHistory(
  dates: string[],
  priceHistory: ReviewScopePriceHistoryDTO | null | undefined,
): LeadershipHistoryVM {
  const rows = priceHistory?.leadership ?? []
  return {
    dates,
    points: dates.map((date, i) => {
      const raw = rows[i] ?? null
      if (!raw) {
        return {
          date,
          status: null,
          reason: null,
          jaccardStability: null,
          migration: null,
          currentLeaderCount: null,
          currentLeaderIds: null,
          unavailable: true,
        }
      }
      const ids = Array.isArray(raw.current_leader_ids)
        ? (raw.current_leader_ids as unknown[]).filter((x): x is string => typeof x === 'string')
        : null
      return {
        date,
        status: typeof raw.status === 'string' ? raw.status : null,
        reason: typeof raw.reason === 'string' ? raw.reason : null,
        jaccardStability: num(raw.jaccard_stability),
        migration: num(raw.migration),
        currentLeaderCount: num(raw.current_leader_count),
        currentLeaderIds: ids,
        unavailable: raw.status !== 'ready',
      }
    }),
  }
}

// ---------------------------------------------------------------------------
// CrossSection（§十二：只允许已正式支持的 EW / AW；own-history Position 不得混为 peer percentile）
// ---------------------------------------------------------------------------

export interface PriceCrossSectionItemVM {
  field: string
  percentile: number | null
  status: string
  reason: string | null
  peerCount: number | null
  validPeerCount: number | null
  unavailable: boolean
  text: string
}

const PRICE_CROSS_SECTION_FIELDS = ['equal_weight_return', 'amount_weighted_return']

export function parsePriceCrossSection(crossSection: ReviewCrossSectionDTO | null | undefined): PriceCrossSectionItemVM[] {
  const fields = crossSection?.fields ?? []
  return PRICE_CROSS_SECTION_FIELDS.map((key) => {
    const f = fields.find((x) => x.field === key)
    if (!f) {
      return {
        field: key,
        percentile: null,
        status: 'unavailable',
        reason: 'NO_FIELD',
        peerCount: null,
        validPeerCount: null,
        unavailable: true,
        text: NULL_DISPLAY,
      }
    }
    const unavailable = f.status !== 'ready'
    return {
      field: key,
      percentile: num(f.percentile),
      status: f.status,
      reason: f.reason ?? null,
      peerCount: num(f.peer_count),
      validPeerCount: num(f.valid_peer_count),
      unavailable,
      text: unavailable
        ? `不可用${f.reason ? ` · ${f.reason}` : ''}`
        : `P${f.percentile === null || f.percentile === undefined ? NULL_DISPLAY : formatNumberNullable(f.percentile, 1)}`,
    }
  })
}

// ---------------------------------------------------------------------------
// 组合入口
// ---------------------------------------------------------------------------

export interface PriceAnalysisVM {
  dates: string[]
  current: PriceCurrentVM
  rolling: PriceRollingVM | null
  ewChart: PriceEwChartVM
  ew: PriceSeriesVM
  aw: PriceSeriesVM
  dispersion: PriceSeriesVM
  breadth: PriceBreadthVM
  capitalTilt: CapitalTiltVM
  leadership: LeadershipHistoryVM
  crossSection: PriceCrossSectionItemVM[]
}

export function parsePriceAnalysis(input: {
  dates: string[]
  observation: Json | null | undefined
  history: ReviewScopeHistoryDTO | null | undefined
  currentTilt: number | null
  crossSection: ReviewCrossSectionDTO | null | undefined
}): PriceAnalysisVM {
  const { dates, observation, history, currentTilt, crossSection } = input
  const fields = history?.fields ?? {}
  const ewField = fields['equal_weight_return']
  const awField = fields['amount_weighted_return']
  const current = parsePriceCurrent(observation)
  const rolling = parsePriceRolling(ewField)
  return {
    dates,
    current,
    rolling,
    ewChart: buildPriceEwChart(dates, ewField),
    ew: parsePriceSeries(dates, ewField),
    aw: parsePriceSeries(dates, awField),
    dispersion: parsePriceSeries(dates, fields['return_dispersion']),
    breadth: parsePriceBreadth(dates, fields['advance_ratio'], fields['decline_ratio'], fields['unchanged_ratio']),
    capitalTilt: parseCapitalTilt(dates, history?.price, currentTilt, current.equalWeightReturn, current.amountWeightedReturn),
    leadership: parseLeadershipHistory(dates, history?.price),
    crossSection: parsePriceCrossSection(crossSection),
  }
}
