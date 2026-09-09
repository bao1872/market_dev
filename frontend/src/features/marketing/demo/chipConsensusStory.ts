import type { UTCTimestamp } from 'lightweight-charts'
import type { StoryCandle, StoryStage } from './storyTypes'

/**
 * Marketing educational model only.
 *
 * 用于解释成交如何逐步堆积成成交密集区、以及成交重心如何迁移。
 * 不是盘迹生产算法的重新实现，禁止业务页面复用。
 *
 * 全部 deterministic：不引入任何随机源，不读取当前时间，不接实时行情。
 */

const DAY = 86_400
const BASE_TIME = 1_786_000_000

const OPEN_OFFSET = [0.06, -0.05, 0.02]
const HIGH_OFFSET = [0.16, 0.19, 0.22]
const LOW_OFFSET = [0.15, 0.18]

const CLOSES = [
  20.2, 20.5, 20.3, 20.7, 20.4, 20.8, 20.6, 20.9, 21.4, 21.9, 22.3, 22.8, 23.1,
  23.4, 23.8, 24.1, 24.0, 24.3, 24.5, 24.2, 24.6, 24.4, 24.8, 24.7,
]

// 刻意设计：低位反复换手 → 中间缩量上行 → 高位重新大量换手
const VOLUMES = [
  260, 300, 280, 340, 320, 360, 330, 350, 90, 80, 75, 85, 70, 80, 180, 260, 420,
  520, 610, 560, 640, 590, 660, 620,
]

const round2 = (value: number) => Math.round(value * 100) / 100

export const CHIP_CANDLES: StoryCandle[] = CLOSES.map((close, index) => {
  const previousClose = index === 0 ? close - 0.15 : CLOSES[index - 1]
  const open = round2(previousClose + OPEN_OFFSET[index % OPEN_OFFSET.length])

  return {
    time: (BASE_TIME + index * DAY) as UTCTimestamp,
    open,
    close,
    high: round2(Math.max(open, close) + HIGH_OFFSET[index % HIGH_OFFSET.length]),
    low: round2(Math.min(open, close) - LOW_OFFSET[index % LOW_OFFSET.length]),
    volume: VOLUMES[index],
  }
})

export const CHIP_STAGES: StoryStage[] = [
  {
    id: 'accumulate',
    endFrame: 8,
    title: '成交先在一个区域反复累积',
    summary: '20–21 附近反复换手。',
    explanation:
      '成交不断发生在同一个价格区间里，堆积出一个明显的成交密集区。这是后面判断"共识在哪"的基准。',
  },
  {
    id: 'price-ahead',
    endFrame: 14,
    title: '价格上涨了，成交重心还没跟上',
    summary: '价格来到更高位置，但这一段成交不多。',
    explanation:
      '这一段的成交量明显偏小，说明新位置还没有经过充分换手，旧的成交密集区仍然是主要共识所在。',
  },
  {
    id: 'new-turnover',
    endFrame: 18,
    title: '新区域开始大量换手',
    summary: '24 附近成交明显增加。',
    explanation:
      '价格在新位置开始反复成交，新的成交共识开始形成，但还没有超过原来的密集区。',
  },
  {
    id: 'relocation',
    endFrame: 24,
    title: '成交重心发生迁移',
    summary: '主要成交密集价从低位移动到新的区域。',
    explanation:
      '随着新位置的成交不断累积，主要成交密集价整体上移——这才是"共识迁移"，而不是画出来的一条线。',
  },
]

export type ProfileBin = {
  price: number
  volume: number
}

/**
 * Marketing educational model only.
 *
 * 将单根 K 线的成交量平均分配到它覆盖的价格 bin，
 * 用于可视化"成交如何堆积成密集区"。
 *
 * 它不是盘迹 production canonical chip algorithm，
 * 禁止业务页面复用。
 */
export function buildTeachingVolumeProfile(
  candles: StoryCandle[],
  binSize = 0.25,
): ProfileBin[] {
  const bins = new Map<number, number>()

  for (const candle of candles) {
    const lowBin = Math.floor(candle.low / binSize)
    const highBin = Math.ceil(candle.high / binSize)

    const touched: number[] = []
    for (let bin = lowBin; bin <= highBin; bin += 1) {
      touched.push(bin)
    }

    const distributed = candle.volume / Math.max(touched.length, 1)

    for (const bin of touched) {
      bins.set(bin, (bins.get(bin) ?? 0) + distributed)
    }
  }

  return [...bins.entries()]
    .map(([bin, volume]) => ({ price: bin * binSize, volume }))
    .sort((a, b) => a.price - b.price)
}

export function getPrimaryConsensusPrice(profile: ProfileBin[]): number | null {
  if (!profile.length) return null

  return profile.reduce((best, item) => (item.volume > best.volume ? item : best))
    .price
}
