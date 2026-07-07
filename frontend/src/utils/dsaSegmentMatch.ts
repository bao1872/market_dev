// [DSA Segment Match] - 描述: DSA visual_segments 与 K线 displayTimes 匹配统计
//   纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试。
//   供 StrategyChart.renderDsaPolyline 调用，并在 debugIndicatorAlignment=1 时
//   输出 matched ratio / first-last segment time / first-last display time 诊断。
//
// 修复根因（PR #34）：
//   - 旧后端 strftime("%Y-%m-%d") 把 15m/1h segment point time 截断为日期，
//     前端 normalizeChartTime('15m') 返回 null，renderer matched=0，开关打开也画不出线。
//   - 新后端 format_dsa_time 按 Timestamp 是否含时间部分序列化：
//     15m/1h 含 THH:MM:SS，1d/1w/1mo 为 YYYY-MM-DD。
//   - 本模块验证 visual_segments.points.time 经 normalizeChartTime 后能匹配 K线 displayTimes。
//
// 用法：
//   import { computeDsaSegmentMatchStats } from '@/utils/dsaSegmentMatch'
//   const stats = computeDsaSegmentMatchStats(segments, displayTimes, timeframe)
//   if (stats.ratio < 0.5) { /* 触发 mismatch / degraded 提示 */ }

import { normalizeChartTime } from './chartTime.ts'

/** DSA visual_segment 单点（与后端 dynamic_swing_anchored_vwap._make_segment 契约一致）。 */
export interface DsaSegmentPoint {
  time: string
  value: number
}

/** DSA visual_segment（Pine polyline 契约）。 */
export interface DsaVisualSegment {
  direction: 1 | -1
  points: DsaSegmentPoint[]
}

/** segment points 与 K线 displayTimes 匹配统计结果。 */
export interface DsaSegmentMatchStats {
  /** 所有 segment 的 points 总数（NaN 已被后端过滤）。 */
  total: number
  /** segment.points.time 经 normalizeChartTime 后能匹配 displayTimes canonical key 的数量。 */
  matched: number
  /** matched / total；total=0 时为 0。 */
  ratio: number
  /** 第一个 segment point 的原始 time（debug 用）。 */
  firstSegTime: string | null
  /** 最后一个 segment point 的原始 time（debug 用）。 */
  lastSegTime: string | null
  /** 第一个 K线 displayTime（debug 用）。 */
  firstDisplayTime: string | null
  /** 最后一个 K线 displayTime（debug 用）。 */
  lastDisplayTime: string | null
  /**
   * degraded reason（用于 debug 诊断）：
   *   - null: 正常匹配（ratio > 0.5）
   *   - 'no_segments': segments 为空
   *   - 'no_points': segments 非空但 points 总数为 0
   *   - 'no_display_times': displayTimes 为空
   *   - 'segment_time_no_match': ratio <= 0.5（含 15m 旧 YYYY-MM-DD 退化为日期场景）
   */
  degradedReason:
    | null
    | 'no_segments'
    | 'no_points'
    | 'no_display_times'
    | 'segment_time_no_match'
}

/**
 * 计算 DSA visual_segments 与 K线 displayTimes 的匹配统计。
 *
 * 实现要点：
 *   - K线 displayTimes 经 normalizeChartTime 构建 canonical key → display index map
 *   - 逐段遍历 points，逐点 normalizeChartTime 后查询 map，命中即 matched++
 *   - 不修改 segments（渲染器仍按原逻辑独立绘制每段，段间不连线）
 *   - 用于 renderDsaPolyline 内部诊断 + dsaSourceAlignment contract test
 *
 * @param segments DSA visual_segments（与后端契约一致）
 * @param displayTimes K线 displayTimes（含 aware +08:00 后缀）
 * @param timeframe 当前周期：1d/15m/1h/1w/1mo
 */
export function computeDsaSegmentMatchStats(
  segments: DsaVisualSegment[],
  displayTimes: string[],
  timeframe: string,
): DsaSegmentMatchStats {
  if (!segments || segments.length === 0) {
    return {
      total: 0,
      matched: 0,
      ratio: 0,
      firstSegTime: null,
      lastSegTime: null,
      firstDisplayTime: displayTimes.length > 0 ? displayTimes[0] : null,
      lastDisplayTime: displayTimes.length > 0 ? displayTimes[displayTimes.length - 1] : null,
      degradedReason: 'no_segments',
    }
  }

  if (!displayTimes || displayTimes.length === 0) {
    return {
      total: 0,
      matched: 0,
      ratio: 0,
      firstSegTime: null,
      lastSegTime: null,
      firstDisplayTime: null,
      lastDisplayTime: null,
      degradedReason: 'no_display_times',
    }
  }

  // K线 canonical key → display index（与 renderDsaPolyline 一致）
  const klineTimeIndex = new Map<string, number>()
  displayTimes.forEach((t, i) => {
    const key = normalizeChartTime(t, timeframe)
    if (key != null) klineTimeIndex.set(key, i)
  })

  let total = 0
  let matched = 0
  let firstSegTime: string | null = null
  let lastSegTime: string | null = null

  for (const seg of segments) {
    if (!seg.points) continue
    for (const pt of seg.points) {
      total++
      if (firstSegTime === null) firstSegTime = pt.time
      lastSegTime = pt.time
      const key = normalizeChartTime(pt.time, timeframe)
      if (key == null) continue
      if (klineTimeIndex.has(key)) matched++
    }
  }

  const ratio = total > 0 ? matched / total : 0
  let degradedReason: DsaSegmentMatchStats['degradedReason'] = null
  if (total === 0) {
    degradedReason = 'no_points'
  } else if (matched / total < 0.5) {
    degradedReason = 'segment_time_no_match'
  }

  return {
    total,
    matched,
    ratio,
    firstSegTime,
    lastSegTime,
    firstDisplayTime: displayTimes[0],
    lastDisplayTime: displayTimes[displayTimes.length - 1],
    degradedReason,
  }
}
