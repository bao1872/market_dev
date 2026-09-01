// [ScopeDynamicsChart] - 描述: Dynamics 图表数据适配层（Slice E correction）
//
// 硬契约（prompt §2、§3、§4）：
// - 图表数据源 ONLY composition.historical_dynamics.scope_dynamics.historical_dynamics。
// - 绝不前端重算 EMA/Velocity/Acceleration/Persistence/Phase。
// - 缺失观测 = whitespace 缺口（gap preservation），不填 0、不插值、不 carry。
// - Position 图固定 0–100 y 域（visual domain，不修改数据本身）。
// - Velocity/Acceleration 含 0 参考线（visible reference line，不是 autoscale trick）。
// - **禁止**使用较短者静默截断时间轴（dates 与 series 取较短者）。
//   每个 series 的日期来自各自 fact-object 的 trade_date；缺失日 = 显式 whitespace gap。
//   当 dates 与 values 长度不一致时，不按较短者截断——保留所有 date，缺失 value 标记为 gap。

import type { LineWidth, Time, LogicalRange } from 'lightweight-charts'

export interface ScopeDynamicsChartPoint {
  time: string
  value?: number
}

export type ScopeDynamicsChartData = ScopeDynamicsChartPoint[]

/** Position 视觉域边界（固定 0–100，不修改数据值） */
export const POSITION_MIN = 0
export const POSITION_MAX = 100

/**
 * 将 date + (nullable) value 序列对齐为 lightweight-charts 数据。
 * 缺失点 = 仅 time 的 whitespace point（无 value），不填 0、不 carry 前值。
 * 不静默截断：dates 与 values 长度不一致时，保留所有 date，缺失 value 标记为 gap。
 * 这是 date-aligned / never compressed 合同的前端实现。
 */
export function alignDynamicsSeries(
  dates: readonly string[],
  series: readonly (number | null)[],
): ScopeDynamicsChartData {
  const out: ScopeDynamicsChartData = []
  const maxLen = Math.max(dates.length, series.length)
  for (let i = 0; i < maxLen; i += 1) {
    const time = dates[i]
    if (!time) continue
    const v = series[i]
    if (v === null || v === undefined || Number.isNaN(v)) {
      out.push({ time })
    } else {
      out.push({ time, value: v })
    }
  }
  return out
}

/**
 * [Dynamics shared timeline] 合并三个 series 的交易日，构造共享时间轴 domain。
 *
 * 硬契约保持不变：
 * - 只决定 X 轴共同位置，不做任何插值 / 补值 / carry；
 * - 升序去重；
 * - 某个 series 在某日无观测 → 该 series 在该位置是 whitespace gap（见 alignToSharedDomain）。
 *
 * lightweigh-charts 的 ``time`` 就是交易日字符串，因此 domain 元素可直接作为 time。
 */
export function buildSharedTradingDates(
  positionDates: readonly string[],
  velocityDates: readonly string[],
  accelerationDates: readonly string[],
): string[] {
  const set = new Set<string>()
  for (const d of positionDates) if (d) set.add(d)
  for (const d of velocityDates) if (d) set.add(d)
  for (const d of accelerationDates) if (d) set.add(d)
  return [...set].sort()
}

/**
 * 按共享 domain 对齐单个 series。
 * - domain 中该日有观测 → { time, value }；
 * - 无观测 / null / NaN → { time }（whitespace gap，不填 0、不插值、不 carry）；
 * - 不在 domain 中的原始日期被忽略（domain 是三图并集，正常不会发生）。
 */
export function alignToSharedDomain(
  domain: readonly string[],
  dates: readonly string[],
  values: readonly (number | null)[],
): ScopeDynamicsChartData {
  const byDate = new Map<string, number | null>()
  const n = Math.max(dates.length, values.length)
  for (let i = 0; i < n; i += 1) {
    const d = dates[i]
    if (!d) continue
    const v = values[i]
    byDate.set(d, v === undefined || v === null || Number.isNaN(v) ? null : v)
  }
  const out: ScopeDynamicsChartData = []
  for (const time of domain) {
    const v = byDate.get(time)
    if (v === null || v === undefined || Number.isNaN(v)) {
      out.push({ time })
    } else {
      out.push({ time, value: v })
    }
  }
  return out
}

/** 取某交易日的 series 值（同步 tooltip 用）；缺失/无该日期返回 null。 */
export function valueAtDate(
  dates: readonly string[],
  values: readonly (number | null)[],
  date: string | null,
): number | null {
  if (!date) return null
  const i = dates.indexOf(date)
  if (i < 0) return null
  const v = values[i]
  if (v === undefined || v === null || Number.isNaN(v)) return null
  return v
}

/** 从 chart data 取某时间点的值（用于同步 crosshair 的 y 位置）；gap 返回 null。 */
export function chartValueAtTime(
  data: ScopeDynamicsChartData,
  time: string | null,
): number | null {
  if (!time) return null
  for (const p of data) {
    if (p.time === time) return 'value' in p && typeof p.value === 'number' ? p.value : null
  }
  return null
}

/**
 * 三图共享的 visible logical range 同步器。
 * 返回订阅/应用函数；调用方需持有 ``syncing`` 标志防止互相触发的无限循环。
 */
export function shouldApplyRange(
  range: LogicalRange | null,
  syncing: boolean,
): range is LogicalRange {
  return !syncing && range !== null && Number.isFinite(range.from) && Number.isFinite(range.to)
}

export type { Time, LogicalRange }

/**
 * 为 Position 图生成 autoscale 建议：
 * - 视觉域固定 0–100（后端 position 已是百分位值）。
 * - 返回固定域，不依赖数据实际范围。
 */
export function buildPositionAutoscale(
  _data: ScopeDynamicsChartData,
): { min: number; max: number } | null {
  return { min: POSITION_MIN, max: POSITION_MAX }
}

/**
 * 为 Velocity/Acceleration 图生成 autoscale 建议：
 * - 必须包含 0 参考线（neutral zero）。
 * - 不修改数据值本身，仅建议视觉范围。
 */
export function buildOffsetAutoscale(
  data: ScopeDynamicsChartData,
): { min: number; max: number } | null {
  const values: number[] = []
  for (const p of data) {
    if ('value' in p && typeof p.value === 'number' && Number.isFinite(p.value)) {
      values.push(p.value)
    }
  }
  if (values.length === 0) return null
  const dataMin = Math.min(...values)
  const dataMax = Math.max(...values)
  // 必须包含 0 参考线
  const min = Math.min(0, dataMin)
  const max = Math.max(0, dataMax)
  return { min, max }
}

/**
 * 为 offset 系列（Velocity/Acceleration）返回零参考线定义。
 * 使用 createPriceLine({ price: 0 }) 展示可见 neutral zero，
 * 不是仅靠 autoscale 包含 0 来隐式暗示。
 */
export function buildZeroReferenceLine(
  color = '#263440',
): { price: number; color: string; lineWidth: LineWidth; axisLabelVisible: boolean; title: string } {
  return {
    price: 0,
    color,
    lineWidth: 1 as LineWidth,
    axisLabelVisible: true,
    title: 'zero',
  }
}
