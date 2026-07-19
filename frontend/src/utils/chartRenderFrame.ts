// [ChartRenderFrame] - 描述: 周期切换原子渲染门禁 + 纵轴 domain policy
//
// 问题背景（PROMPT.md §五.255-307）：
//   1. Bars 和 Indicators 是两个独立 React Query 请求，切换周期时可能短暂出现
//      "新周期 K线 + 旧周期指标"。当前只有 DSA 做了 source mismatch 检查，
//      其他 BB/Node/SMC 没有统一 frame 级门禁，导致指标与 K线错位渲染。
//   2. drawTrading() 计算主图纵轴时直接 push 全部 Node lo/hi：
//        profile.nodes.forEach(n => { priceCandidates.push(n.lo, n.hi) })
//      没有判断该 Node 是否属于当前可见价格区间，结果远端 Node（如历史高/低
//      位）把纵轴拉大，K线被压缩，指标绝对位置和比例看起来错误。
//
// 解决方案：
//   1. ChartRenderFrame：从 bars response 和 indicators response 各自提取 frame，
//      frame 一致时才提交绘制，否则显示短暂加载状态（不允许旧指标覆盖新 K线）。
//   2. shouldIncludeNodeInPriceRange：基于可见 K线高低区间 + 容差，过滤远端 Node，
//      避免纵轴被非可见指标扩张。同样适用于 SMC trailing top/bottom。
//
// 设计原则：
//   - 纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试
//   - frame 字段对齐 PROMPT.md §五.296-305：instrument / timeframe / adj /
//     source bar 时间范围 / source hash / market data contract version
//   - 容差采用"可见区间 ± 50%"策略：远端历史 Node 不参与纵轴，但仍由 Node
//     图层渲染（被裁剪到画布外，不影响交互命中检测）

/**
 * 图表渲染帧 — Bars 与 Indicators 必须帧一致才提交绘制。
 *
 * 字段对齐 PROMPT.md §五.296-305：
 *   - instrumentId / timeframe / adj：基础标识
 *   - sourceBarHash：bars OHLCV SHA256 前 16 字符（与 indicators.source_bar_hash 比对）
 *   - sourceBarRangeKey：source_bar_times 首末时间拼接的 key（检测范围漂移）
 *   - marketDataContractVersion：MDAS 契约版本（v2 等）
 */
export interface ChartRenderFrame {
  instrumentId: string
  timeframe: string
  adj: string
  sourceBarHash: string | null
  sourceBarRangeKey: string | null
  marketDataContractVersion: string | null
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
 * 从 bars response 提取渲染帧。
 *
 * 入参字段对齐后端 BarListResponse（market_data_contract_version / source_bar_hash /
 * adjustment_as_of）。bars 时间范围通过 items 首末 trade_date/trade_time 计算。
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
}): ChartRenderFrame | null {
  if (!params.instrumentId) return null
  if (!params.timeframe || !params.adj) return null
  return {
    instrumentId: params.instrumentId,
    timeframe: params.timeframe,
    adj: params.adj,
    sourceBarHash: params.sourceBarHash ?? null,
    sourceBarRangeKey: computeSourceBarRangeKey(params.barTimes ?? null),
    marketDataContractVersion: params.marketDataContractVersion ?? null,
  }
}

/**
 * 从 indicators response 提取渲染帧。
 *
 * indicators.source_bar_hash 与 bars.source_bar_hash 应一致（同标的同周期同结束日）。
 * indicators 自身没有 instrumentId/timeframe/adj 字段（请求参数中有但响应不回显），
 * 由调用方传入用于帧比对。
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
}): ChartRenderFrame | null {
  if (!params.instrumentId) return null
  if (!params.timeframe || !params.adj) return null
  return {
    instrumentId: params.instrumentId,
    timeframe: params.timeframe,
    adj: params.adj,
    sourceBarHash: params.sourceBarHash ?? null,
    sourceBarRangeKey: computeSourceBarRangeKey(params.sourceBarTimes ?? null),
    marketDataContractVersion: params.marketDataContractVersion ?? null,
  }
}

/**
 * 判断 Bars 帧与 Indicators 帧是否匹配（一致时才提交指标绘制）。
 *
 * 比对维度（PROMPT.md §五.296-305）：
 *   1. instrumentId：标的必须一致
 *   2. timeframe：周期必须一致（防止新周期 K线 + 旧周期指标）
 *   3. adj：复权方式必须一致
 *   4. sourceBarHash：bars OHLCV 哈希应一致（任一端为 null 时降级到 range key）
 *   5. sourceBarRangeKey：bar 时间范围应一致（hash 缺失时降级比对）
 *
 * 比对策略：
 *   - 严格字段（instrumentId/timeframe/adj）：必须完全一致
 *   - 哈希字段（sourceBarHash）：两端都非 null 时必须一致；任一为 null 时降级
 *   - 范围字段（sourceBarRangeKey）：两端都非 null 时必须一致；任一为 null 时不阻塞
 *   - 任一帧为 null 时返回 false（保护性：未完整数据不应触发指标渲染）
 *
 * marketDataContractVersion 当前不参与严格比对（仅诊断用），因为 bars/indicators
 * 都从同一 MDAS 取行情，契约版本理论上必然一致；若未来出现分歧再启用严格比对。
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
