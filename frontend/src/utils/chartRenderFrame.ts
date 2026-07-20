// [ChartRenderFrame] - 描述: 周期切换原子渲染门禁 + 纵轴 domain policy
//
// 问题背景（PROMPT.md §一、§二）：
//   1. Bars 和 Indicators 是两个独立 React Query 请求，切换周期时可能短暂出现
//      "新周期 K线 + 旧周期指标"。当前只有 DSA 做了 source mismatch 检查，
//      其他 BB/Node/SMC 没有统一 frame 级门禁，导致指标与 K线错位渲染。
//   2. 之前严格比较 bars.source_bar_hash 与 indicators.source_bar_hash，但两者
//      来源不同（bars 是展示窗口 100 根，indicators 是算法输入 250 根），导致
//      1d 周期永久 mismatch，指标图层被屏蔽，页面持续显示"指标加载中"。
//   3. drawTrading() 计算主图纵轴时直接 push 全部 Node lo/hi：
//        profile.nodes.forEach(n => { priceCandidates.push(n.lo, n.hi) })
//      没有判断该 Node 是否属于当前可见价格区间，结果远端 Node（如历史高/低
//      位）把纵轴拉大，K线被压缩，指标绝对位置和比例看起来错误。
//
// 解决方案（PROMPT.md §二.1 展示帧/算法输入帧分离）：
//   1. 后端新增 display_frame：只描述真正交给前端绘制的 K线窗口。
//      bars API 与 indicators API 调用同一个 build_display_frame() 生成。
//   2. 算法输入 hash（source_bar_hash/daily_hash/15m_hash/profile_hash）
//      移入 calculation_diagnostics，不参与展示帧匹配。
//   3. ChartRenderFrame：优先比较 display_frame（display_hash + display_times），
//      display_frame 缺失时降级到旧 source_bar_hash 比对（向后兼容）。
//   4. shouldIncludeNodeInPriceRange：基于可见 K线高低区间 + 容差，过滤远端 Node。
//
// 设计原则：
//   - 纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试
//   - 展示帧 ≠ 算法输入帧：display_hash 只描述展示窗口，不包含 warmup/算法输入
//   - 容差采用"可见区间 ± 50%"策略：远端历史 Node 不参与纵轴，但仍由 Node
//     图层渲染（被裁剪到画布外，不影响交互命中检测）

import type { DisplayFrame } from '../api/endpoints'

// 重新导出 DisplayFrame，方便调用方从 chartRenderFrame 统一入口导入
export type { DisplayFrame }

/**
 * 图表渲染帧 — Bars 与 Indicators 必须帧一致才提交绘制。
 *
 * 优先比较 display_frame（display_hash + display_times 范围 key），
 * display_frame 缺失时降级到旧 source_bar_hash 比对（向后兼容）。
 */
export interface ChartRenderFrame {
  instrumentId: string
  timeframe: string
  adj: string
  sourceBarHash: string | null
  sourceBarRangeKey: string | null
  marketDataContractVersion: string | null
  /** 展示帧 hash（来自 display_frame.display_hash），优先参与比对 */
  displayHash: string | null
  /** 展示帧时间范围 key（display_times 首末拼接），优先参与比对 */
  displayRangeKey: string | null
}

/**
 * 从 source_bar_times 数组构造范围 key（首末时间拼接）。
 *
 * 不直接比较整个数组（O(n)），仅比较首末时间，足以检测：
 *   - 周期切换（首末时间完全不同）
 *   - 新 bar 追加（末时间变化）
 *   - 历史范围变化（首时间变化）
 *
 * 返回 null 表示 source_bar_times 为空或无效。
 */
export function computeSourceBarRangeKey(
  sourceBarTimes: readonly string[] | null | undefined,
): string | null {
  if (!sourceBarTimes || sourceBarTimes.length === 0) return null
  const first = sourceBarTimes[0]
  const last = sourceBarTimes[sourceBarTimes.length - 1]
  if (!first || !last) return null
  return `${first}|${last}`
}

/**
 * 从后端 display_frame 提取展示帧字段，返回 ChartRenderFrame 所需的 displayHash
 * 与 displayRangeKey（基于 display_times 首末时间拼接）。
 *
 * display_frame 缺失时返回 { displayHash: null, displayRangeKey: null }，
 * 调用方据此降级到旧 source_bar_hash 比对（向后兼容）。
 *
 * 注意：display_times 为空数组时 displayHash 也视为 null（与后端 build_display_frame
 * 的空 DataFrame 路径一致：display_hash="" → 视为缺失，触发降级）。
 */
export function extractDisplayFrameFields(
  displayFrame: DisplayFrame | null | undefined,
): { displayHash: string | null; displayRangeKey: string | null } {
  if (!displayFrame) return { displayHash: null, displayRangeKey: null }
  const times = displayFrame.display_times
  const rangeKey = computeSourceBarRangeKey(times)
  const hash = displayFrame.display_hash || null
  return { displayHash: hash, displayRangeKey: rangeKey }
}

/**
 * 从 bars response 提取渲染帧。
 *
 * 入参字段对齐后端 BarListResponse（market_data_contract_version / source_bar_hash /
 * adjustment_as_of / display_frame）。bars 时间范围通过 items 首末 trade_date/trade_time 计算。
 *
 * display_frame（PROMPT.md §二.1）优先参与比对：
 *   - 传入 displayFrame 时从中提取 displayHash/displayRangeKey
 *   - 未传入时 displayHash/displayRangeKey 为 null，isFrameMatched 自动降级到 source_bar_hash
 *
 * 返回 null 表示 bars 数据不完整（无法构造帧，不应触发渲染）。
 */
export function buildBarsFrame(params: {
  instrumentId: string | null | undefined
  timeframe: string
  adj: string
  sourceBarHash?: string | null
  marketDataContractVersion?: string | null
  barTimes?: readonly string[]
  displayFrame?: DisplayFrame | null
}): ChartRenderFrame | null {
  if (!params.instrumentId) return null
  if (!params.timeframe || !params.adj) return null
  const { displayHash, displayRangeKey } = extractDisplayFrameFields(params.displayFrame)
  return {
    instrumentId: params.instrumentId,
    timeframe: params.timeframe,
    adj: params.adj,
    sourceBarHash: params.sourceBarHash ?? null,
    sourceBarRangeKey: computeSourceBarRangeKey(params.barTimes ?? null),
    marketDataContractVersion: params.marketDataContractVersion ?? null,
    displayHash,
    displayRangeKey,
  }
}

/**
 * 从 indicators response 提取渲染帧。
 *
 * indicators.source_bar_hash 与 bars.source_bar_hash 在算法输入层面可能不同
 * （如 1d 周期 Node 算法输入 250 根，bars 展示窗口 100 根），因此不能直接比对。
 *
 * PROMPT.md §二.1 解决方案：indicators API 同样返回 display_frame（描述真正展示
 * 给前端的 K 线窗口，与 bars API 共用 build_display_frame 生成），ChartRenderFrame
 * 只比对 display_frame；source_bar_hash 移入 calculation_diagnostics 不参与匹配。
 *
 * 返回 null 表示 indicators 数据不完整。
 */
export function buildIndicatorsFrame(params: {
  instrumentId: string | null | undefined
  timeframe: string
  adj: string
  sourceBarHash?: string | null
  sourceBarTimes?: readonly string[] | null
  marketDataContractVersion?: string | null
  displayFrame?: DisplayFrame | null
}): ChartRenderFrame | null {
  if (!params.instrumentId) return null
  if (!params.timeframe || !params.adj) return null
  const { displayHash, displayRangeKey } = extractDisplayFrameFields(params.displayFrame)
  return {
    instrumentId: params.instrumentId,
    timeframe: params.timeframe,
    adj: params.adj,
    sourceBarHash: params.sourceBarHash ?? null,
    sourceBarRangeKey: computeSourceBarRangeKey(params.sourceBarTimes ?? null),
    marketDataContractVersion: params.marketDataContractVersion ?? null,
    displayHash,
    displayRangeKey,
  }
}

/**
 * 判断 Bars 帧与 Indicators 帧是否匹配（一致时才提交指标绘制）。
 *
 * 比对维度（PROMPT.md §二.1 + §五.296-305）：
 *   1. instrumentId：标的必须一致
 *   2. timeframe：周期必须一致（防止新周期 K线 + 旧周期指标）
 *   3. adj：复权方式必须一致
 *   4. displayHash（优先）：两端都非 null 时必须一致；displayRangeKey 同样比对
 *   5. sourceBarHash（降级）：display_frame 双侧都缺失时降级到 source_bar_hash 比对
 *
 * 比对策略：
 *   - 严格字段（instrumentId/timeframe/adj）：必须完全一致
 *   - display_frame 路径（优先）：bars/indicators 都提供 displayHash 时严格比对；
 *     displayRangeKey 双侧非 null 时也必须一致（防止 hash 相同但窗口不同）
 *   - source_bar_hash 路径（降级）：仅当双侧 displayHash 都为 null 时启用
 *   - 任一帧为 null 时返回 false（保护性：未完整数据不应触发指标渲染）
 *
 * 一侧提供 display_frame、另一侧缺失时，视为 mismatch（display_frame 不对称，
 * 通常出现在 API 升级过渡期，应提示而非静默降级），由调用方显示 mismatch-error。
 */
export function isFrameMatched(
  barsFrame: ChartRenderFrame | null,
  indicatorsFrame: ChartRenderFrame | null,
): boolean {
  // 任一帧为 null：保护性拒绝（数据不完整不应触发指标渲染）
  if (barsFrame == null || indicatorsFrame == null) return false

  // 严格字段：必须完全一致
  if (barsFrame.instrumentId !== indicatorsFrame.instrumentId) return false
  if (barsFrame.timeframe !== indicatorsFrame.timeframe) return false
  if (barsFrame.adj !== indicatorsFrame.adj) return false

  // display_frame 路径（优先）：任一端提供 displayHash 即进入 display_frame 比对
  const barsHasDisplay = barsFrame.displayHash != null
  const indHasDisplay = indicatorsFrame.displayHash != null
  if (barsHasDisplay || indHasDisplay) {
    // 一侧提供、另一侧缺失：display_frame 不对称 → mismatch
    if (!barsHasDisplay || !indHasDisplay) return false
    // 双侧都提供：displayHash 必须一致
    if (barsFrame.displayHash !== indicatorsFrame.displayHash) return false
    // displayRangeKey 双侧非 null 时必须一致（防止 hash 相同但窗口不同）
    if (
      barsFrame.displayRangeKey != null &&
      indicatorsFrame.displayRangeKey != null &&
      barsFrame.displayRangeKey !== indicatorsFrame.displayRangeKey
    ) {
      return false
    }
    return true
  }

  // 降级路径：双侧 display_frame 都缺失，使用 source_bar_hash 比对（向后兼容）
  // sourceBarHash：两端都非 null 时必须一致
  if (
    barsFrame.sourceBarHash != null &&
    indicatorsFrame.sourceBarHash != null &&
    barsFrame.sourceBarHash !== indicatorsFrame.sourceBarHash
  ) {
    return false
  }

  // sourceBarRangeKey：两端都非 null 时必须一致（hash 缺失时降级比对）
  if (
    barsFrame.sourceBarRangeKey != null &&
    indicatorsFrame.sourceBarRangeKey != null &&
    barsFrame.sourceBarRangeKey !== indicatorsFrame.sourceBarRangeKey
  ) {
    return false
  }

  return true
}

// =============================================================================
// 纵轴 domain policy：过滤非可见价格区间的指标值
// =============================================================================

/**
 * 可见 K线价格区间（用于 domain policy 过滤）。
 *
 * 由 display K线的 min(low) / max(high) 计算，外加容差区间。
 */
export interface VisiblePriceBounds {
  /** 可见 K线最低 low。 */
  low: number
  /** 可见 K线最高 high。 */
  high: number
  /** 容差下界（low - tolerance）。 */
  lowerBound: number
  /** 容差上界（high + tolerance）。 */
  upperBound: number
}

/**
 * 计算可见 K线价格区间 + 容差。
 *
 * 容差策略：可见区间 ± 50%（range * 0.5）。
 *   - 50% 容差允许 Node 略微超出可见 K线范围（如刚跌破的历史支撑 Node）
 *   - 但远端历史高位/低位 Node（如价格翻倍前的节点）会被过滤出纵轴候选
 *   - 容差比例固定为 0.5，避免依赖外部配置；未来可参数化
 *
 * 边界情况：
 *   - display 为空：返回 null（调用方应处理 null）
 *   - 所有 K线价格相同（range=0）：容差 = low * 0.5（避免 0 容差导致全部 Node 被过滤）
 *
 * @param displayLows  可见 K线 low 数组
 * @param displayHighs 可见 K线 high 数组
 * @returns VisiblePriceBounds 或 null（display 为空时）
 */
export function computeVisiblePriceBounds(
  displayLows: readonly number[],
  displayHighs: readonly number[],
): VisiblePriceBounds | null {
  if (displayLows.length === 0 || displayHighs.length === 0) return null
  const low = Math.min(...displayLows)
  const high = Math.max(...displayHighs)
  const range = Math.max(high - low, low * 0.001)
  const tolerance = range * 0.5
  return {
    low,
    high,
    lowerBound: low - tolerance,
    upperBound: high + tolerance,
  }
}

/**
 * 判断 Node 是否应纳入纵轴价格候选（domain policy）。
 *
 * PROMPT.md §五.255-282：drawTrading 之前直接 push 全部 Node lo/hi，导致远端
 * Node 把纵轴拉大、K线被压缩、指标比例错误。修复后只纳入与可见价格区间相交
 * （含容差）的 Node。
 *
 * 相交判定：Node.hi >= lowerBound && Node.lo <= upperBound
 *   - Node 完全在容差区间下方：hi < lowerBound → 不纳入（远端历史低位）
 *   - Node 完全在容差区间上方：lo > upperBound → 不纳入（远端历史高位）
 *   - Node 与容差区间相交：纳入（含恰好接触边界）
 *
 * 注意：被过滤的 Node 仍由 Node 图层正常渲染（drawNodeCluster），只是不参与
 * 纵轴 min/max 计算；Canvas 会自动裁剪超出纵轴的 Node 矩形。
 *
 * @param node          待判定的 Node（含 lo/hi 字段）
 * @param bounds        可见价格区间（含容差），null 时降级到"全部纳入"
 *                      （保持旧行为，避免 bounds 缺失时 Node 全部消失）
 * @returns true 表示该 Node 应纳入纵轴候选
 */
export function shouldIncludeNodeInPriceRange(
  node: { lo: number; hi: number },
  bounds: VisiblePriceBounds | null,
): boolean {
  // bounds 缺失时降级到旧行为（全部纳入），保证不破坏现有渲染
  if (bounds == null) return true
  // 相交判定：Node 区间 [lo, hi] 与容差区间 [lowerBound, upperBound] 相交
  return node.hi >= bounds.lowerBound && node.lo <= bounds.upperBound
}

/**
 * 判断 SMC trailing top/bottom 是否应纳入纵轴价格候选（domain policy）。
 *
 * SMC trailing 表示当前最新结构极值，通常在可见区间附近；但若 SMC trailing
 * 来自历史 bar（如日线 trailing 极值在很久以前），可能远超当前可见价格，
 * 导致纵轴被拉大。
 *
 * 策略：与 Node 相同的相交判定。
 *
 * 注意：collectVisibleSmcPriceCandidates 已经按"窗口内"过滤了 event/OB/EQH，
 * 但 trailing 始终视为可见（"trailing 表示当前最新结构极值"），不过滤窗口。
 * 本函数对 trailing top/bottom 额外应用 domain policy，避免远端 trailing
 * 把纵轴拉大。
 *
 * @param value  待判定的 trailing top/bottom 价格
 * @param bounds 可见价格区间（含容差），null 时降级到"全部纳入"
 * @returns true 表示该 trailing 值应纳入纵轴候选
 */
export function shouldIncludeSmcTrailingInPriceRange(
  value: number,
  bounds: VisiblePriceBounds | null,
): boolean {
  if (bounds == null) return true
  return value >= bounds.lowerBound && value <= bounds.upperBound
}
