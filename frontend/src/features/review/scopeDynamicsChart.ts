// [ScopeDynamicsChart] - 描述: Canonical Historical Dynamics → lightweight-charts 数据适配（Slice E）
//
// 硬契约（prompt §6 gap preservation）：
// - runtime 序列日期对齐且刻意保留缺失观测；前端必须原样保留。
// - 禁止 filter(Boolean) / 删除空点重连 / forward-fill / zero-fill / carry previous / 差值伪连续。
// - 缺失观测用 lightweight-charts 的 whitespace point（仅 { time }，无 value）表示缺口，
//   有值点才带 value；lightweight-charts 据此断开连线。
// - 只做坐标映射，绝不重算 EMA/Velocity/Acceleration/Persistence/Phase。
//
// 纯 TS（无 React / lightweight-charts / @/ 别名依赖），可被 node --test 直接运行。

export interface ScopeDynamicsValuePoint {
  time: string
  value: number
}

/** whitespace point：缺失观测 = 缺口（lightweight-charts 断开连线） */
export interface ScopeDynamicsGapPoint {
  time: string
}

export type ScopeDynamicsChartData = Array<
  ScopeDynamicsValuePoint | ScopeDynamicsGapPoint
>

/** Position 合法域：canonical 0–100 historical percentile */
export const POSITION_MIN = 0
export const POSITION_MAX = 100

/**
 * 把日期对齐的观测值序列映射为 chart data。
 * - dates 与 series 逐位对齐（同一交易日下标）。
 * - null / undefined / NaN → whitespace（缺口），不填 0、不携带上一值、不插值。
 * - 有值则原样保留（Position 不做 ×100）。
 */
export function alignDynamicsSeries(
  dates: readonly string[],
  series: readonly (number | null | undefined)[],
): ScopeDynamicsChartData {
  const length = Math.min(dates.length, series.length)
  const out: ScopeDynamicsChartData = []
  for (let i = 0; i < length; i += 1) {
    const time = dates[i]
    if (!time) continue
    const v = series[i]
    if (v === null || v === undefined || Number.isNaN(v)) {
      out.push({ time })
      continue
    }
    out.push({ time, value: v })
  }
  return out
}

/** Position 是否落在合法 0–100 域内（不钳制、不伪造，仅用于校验/落点保护） */
export function positionInDomain(position: number): boolean {
  return Number.isFinite(position) && position >= POSITION_MIN && position <= POSITION_MAX
}

/**
 * Position 图固定 0–100 域 autoscale（min/max 固定），保证 y 轴语义稳定。
 * 返回 null 表示无可绘制的有值点（不渲染指标线）。
 */
export function buildPositionAutoscale(
  points: ScopeDynamicsChartData,
): { min: number; max: number } | null {
  if (!points.some((p): p is ScopeDynamicsValuePoint => 'value' in p)) return null
  return { min: POSITION_MIN, max: POSITION_MAX }
}

/**
 * 选取 velocity/acceleration 图的有值点，用于计算含 0 参考线的 y 域。
 * 仅坐标映射（保证 0 至少在某点值时包含），不重算数值。
 */
export function buildOffsetAutoscale(
  points: ScopeDynamicsChartData,
): { min: number; max: number } | null {
  let min: number | null = null
  let max: number | null = null
  for (const p of points) {
    if (!('value' in p)) continue
    const v = p.value
    if (min === null || v < min) min = v
    if (max === null || v > max) max = v
  }
  // 始终包含 0 参考线
  min = min === null ? 0 : Math.min(min, 0)
  max = max === null ? 0 : Math.max(max, 0)
  return { min, max }
}