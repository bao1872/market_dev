// Chip Consensus Replay Narration（V1）— 筹码共识解释层。
//
// 原则：Marketing 只「解释」生产 node_cluster DTO，绝不重新计算或重定义筹码语义。
//   - 只消费生产 POC 区间（node.state.poc_price 的 low/mid/high）与真实 K 线做区域关系判断，
//     禁止用固定 3%/5% 价格阈值定义状态，禁止按日期人工写故事。
//   - 无未来函数：migration 必须在「下一 canonical frame 继续存在于新区域」才成立；
//     retest 只用「上一 canonical frame 已存在」的 POC 区域去判断相邻 bar 的重入。
//   - 禁止跨 frame 用一个快照内的「同类标签」去推断「同一批筹码」：那种标签只是单一
//     snapshot 内部的身份标识，跨帧判断只用 production low/mid/high 与区域 overlap。
//
// 本模块是纯函数（无 React / Canvas 依赖），可被 node --test 直接运行。

import { findCanonicalFrameIndex } from './replayFrameUtils'

export { findCanonicalFrameIndex }

export type ChipBeatKind = 'forming' | 'priceLead' | 'migration' | 'retest'

export interface ChipConsensusBeat {
  /** 该叙事对应可见窗口内第几根 K 线（0 基 bars 下标 + 1），用于 seek。 */
  atEndIndex: number
  kind: ChipBeatKind
  /** 用户可见标题（不暴露 POC/node_cluster 等内部词）。 */
  title: string
  /** 一句话含义。 */
  meaning: string
  /** 发生了什么。 */
  explanation: string
  /** 接下来观察什么。 */
  watch: string
  /** 事件时间（帧快照时间 / retest 所在 K 线时间）。 */
  eventTime: string | null
}

/** 生产 POC 价格区间（区域关系判断的唯一输入，来自 node.state.poc_price）。 */
export type PocRegion = {
  low: number
  mid: number
  high: number
}

/** 只消费用于解释的 node DTO 子集；其余字段不在乎。 */
interface ChipNarrationNodeLike {
  state?: {
    current_price?: number | null
    poc_price?: {
      price_low: number | null
      price_mid: number | null
      price_high: number | null
    } | null
  }
}

interface ChipNarrationFrameLike {
  endIndex: number
  endTime: string
  node: ChipNarrationNodeLike
}

/** 可见窗口内单根 K 线（与 MarketingReplayBar 同构的子集）。 */
interface ChipReplayBarLike {
  time: string
  open: number
  high: number
  low: number
  close: number
}

export interface ChipNarrationReplayInput {
  bars: ReadonlyArray<ChipReplayBarLike>
  frames: ReadonlyArray<ChipNarrationFrameLike>
}

// ===== 用户进入这一屏先看到的 3 个导读（M16，不暴露 POC 等内部词）=====
export const CHIP_CONSENSUS_GUIDE = [
  {
    label: '主要成交密集区',
    meaning: '市场成交最集中的位置',
    desc: '价格回到这里，观察买卖双方是否重新定价。',
  },
  {
    label: '价格脱离密集区',
    meaning: '价格跑在共识前面',
    desc: '价格先走，不代表市场共识已经跟过去。',
  },
  {
    label: '成交重心迁移',
    meaning: '新的市场共识正在形成',
    desc: '只有新的价格区域持续发生大量成交，主要成交密集区才会真正迁移。',
  },
] as const

// ===== 叙事文案（唯一中文语义来源）=====
const FORMING_COPY = {
  title: '共识在相近价格持续形成',
  meaning: '主要成交密集区域保持稳定',
  explanation: '成交持续集中在相近价格，市场共识位置保持稳定。',
  watch: '继续观察价格是否离开这个密集区，以及成交重心是否发生变化。',
} as const

const PRICE_LEAD_COPY = {
  title: '价格领先，共识还没跟过去',
  meaning: '价格跑在主要成交密集区前面',
  explanation: '价格已经离开原来的成交密集区域，但成交重心还没有迁移。',
  watch: '观察新的价格位置能否持续发生大量成交，共识是否会跟着迁移。',
} as const

const MIGRATION_COPY = {
  title: '主要成交密集区发生迁移',
  meaning: '新的市场共识形成',
  explanation: '新的价格区域持续发生大量成交，主要成交密集位置离开原来的价格带。',
  watch: '观察新的成交密集区能否继续保持，以及价格离开后是否重新回到这里博弈。',
} as const

const RETEST_COPY = {
  title: '重新进入主要成交密集区',
  meaning: '市场重新回到共识区博弈',
  explanation: '价格重新回到过去成交最集中的位置，买卖双方在这里重新定价。',
  watch: '观察价格能否重新脱离该区域，以及新的成交重心是否继续保持。',
} as const

/** 状态标签（M21 解释面板的 kind 标签，也是 M22 关键节点的类别）。 */
export const CHIP_BEAT_KIND_LABEL: Record<ChipBeatKind, string> = {
  forming: '共识形成',
  priceLead: '价格领先',
  migration: '共识迁移',
  retest: '重新博弈',
}

// ===== 区域关系判断（M8 锁死的唯一语义）=====
/** 两个 POC 价格区间是否重叠。 */
export function regionsOverlap(a: PocRegion, b: PocRegion): boolean {
  return Math.max(a.low, b.low) <= Math.min(a.high, b.high)
}

/** 从生产 node DTO 提取当前 POC 价格区间；缺省（无 POC）时返回 null。 */
export function pocRegionOf(frame: ChipNarrationFrameLike): PocRegion | null {
  const poc = frame.node?.state?.poc_price
  if (!poc) return null
  if (poc.price_low == null || poc.price_mid == null || poc.price_high == null) {
    return null
  }
  return { low: poc.price_low, mid: poc.price_mid, high: poc.price_high }
}

/** 价格与共识区的位置关系（M17：语义是区域关系，不是百分比）。 */
export type PricePosition = 'above' | 'inside' | 'below'

export function classifyPricePosition(
  price: number,
  region: PocRegion,
): PricePosition {
  if (price > region.high) return 'above'
  if (price < region.low) return 'below'
  return 'inside'
}

function barInRegion(bar: ChipReplayBarLike, region: PocRegion): boolean {
  return bar.low <= region.high && bar.high >= region.low
}

/**
 * 构建整条时间线上的筹码共识叙事 beats（M7/M8/M9）。
 *
 * - forming（共识形成）：连续多个 canonical frame 的 POC 区域重叠（同一价格带），
 *   在稳定 regime 建立时（连续 2 个重叠帧）产生一次 beat（M21：不要每帧改文案）。
 * - priceLead（价格领先）：POC 区域仍与上一帧重叠，但 close 已离开区域。
 * - migration（共识迁移）：!overlap(prev, cur) 且 overlap(cur, next) —— 原密集区与
 *   新密集区无重叠，且新区域在下一帧仍存在；单 snapshot 噪声不算迁移。
 * - retest（重新博弈）：bar 从区域外重新进入「上一 canonical frame 已存在的」POC 区域。
 *
 * 返回按 atEndIndex 升序稳定的 beats（同一时刻按检测顺序）。
 */
export function buildChipConsensusBeats(
  replay: ChipNarrationReplayInput,
): ChipConsensusBeat[] {
  const beats: ChipConsensusBeat[] = []
  const frames = replay.frames
  if (frames.length === 0) return beats

  // 第一帧只 seed 上一 POC（不产生叙事）。
  let previousPoc = pocRegionOf(frames[0])
  let lastKind: ChipBeatKind | null = null
  // 刚发生过真实迁移：下一个稳定帧即「新共识形成」。
  let justMigrated = false
  // retest 去重：同一价格带只报一次重新进入，避免边缘震荡刷屏。
  let lastRetestRegionKey: string | null = null

  for (let i = 1; i < frames.length; i += 1) {
    const frame = frames[i]
    const currentPoc = pocRegionOf(frame)
    const nextPoc = i + 1 < frames.length ? pocRegionOf(frames[i + 1]) : null

    if (!previousPoc || !currentPoc) {
      previousPoc = currentPoc
      continue
    }

    const stable = regionsOverlap(previousPoc, currentPoc)
    const moved = !stable
    const persisted = nextPoc ? regionsOverlap(currentPoc, nextPoc) : false

    if (moved && persisted) {
      beats.push({
        atEndIndex: frame.endIndex,
        kind: 'migration',
        ...MIGRATION_COPY,
        eventTime: frame.endTime,
      })
      lastKind = 'migration'
      justMigrated = true
    } else if (stable) {
      const close = replay.bars[frame.endIndex - 1]?.close
      const priceOutside =
        close != null && (close > currentPoc.high || close < currentPoc.low)
      if (justMigrated || i === 1) {
        // 回放开场，或真实迁移后的第一个稳定帧：新的共识带确立。
        beats.push({
          atEndIndex: frame.endIndex,
          kind: 'forming',
          ...FORMING_COPY,
          eventTime: frame.endTime,
        })
        lastKind = 'forming'
        justMigrated = false
      } else if (priceOutside && lastKind !== 'priceLead') {
        beats.push({
          atEndIndex: frame.endIndex,
          kind: 'priceLead',
          ...PRICE_LEAD_COPY,
          eventTime: frame.endTime,
        })
        lastKind = 'priceLead'
      } else if (!priceOutside && lastKind === 'priceLead') {
        // 价格回到密集区内：解除 priceLead 去重，允许下一次真实离开再次触发。
        lastKind = null
      }
    } else {
      // moved 但未持久（单 snapshot 噪声，M8）：不算迁移，也不打断稳定叙事。
    }

    // retest：只用「上一 canonical frame 已存在」的 POC 区域，对相邻 bar 判断重入。
    const retestRegion = previousPoc
    if (retestRegion) {
      const regionKey = `${retestRegion.low}|${retestRegion.high}`
      if (regionKey !== lastRetestRegionKey) {
        const startBar = Math.max(0, frames[i - 1].endIndex)
        const endBar = Math.min(replay.bars.length, frame.endIndex)
        for (let barIndex = startBar; barIndex < endBar; barIndex += 1) {
          const bar = replay.bars[barIndex]
          if (!bar) continue
          const prevBar = barIndex > 0 ? replay.bars[barIndex - 1] : undefined
          const reentered =
            prevBar &&
            !barInRegion(prevBar, retestRegion) &&
            barInRegion(bar, retestRegion)
          if (reentered) {
            lastRetestRegionKey = regionKey
            beats.push({
              atEndIndex: barIndex + 1,
              kind: 'retest',
              ...RETEST_COPY,
              eventTime: bar.time,
            })
            lastKind = 'retest'
            break
          }
        }
      }
    }

    previousPoc = currentPoc
  }

  // 升序稳定排序（同一 atEndIndex 保持先收集顺序）。
  return beats.sort((a, b) => a.atEndIndex - b.atEndIndex)
}
