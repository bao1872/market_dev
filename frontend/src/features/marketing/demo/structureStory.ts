import type { UTCTimestamp } from 'lightweight-charts'
import type { StoryCandle, StoryEvent, StoryStage } from './storyTypes'

/**
 * Marketing educational model only.
 *
 * 用于解释状态如何随 K 线逐步形成（高低点 → 短线状态变化 → 主要状态确认 → 延续）。
 * 不是盘迹生产算法的重新实现，禁止业务页面复用。
 *
 * 全部 deterministic：不引入任何随机源，不读取当前时间，不接实时行情。
 */

const DAY = 86_400
const BASE_TIME = 1_786_000_000

// 固定 offset pattern（禁止 random）
const OPEN_OFFSET = [0.08, -0.06, 0.03]
const HIGH_OFFSET = [0.28, 0.34, 0.31]
const LOW_OFFSET = [0.26, 0.32]

const CLOSES = [
  20.4, 20.8, 21.2, 20.9, 20.5, 20.7, 21.0, 21.4, 21.1, 21.6, 22.0, 21.7, 22.4,
  22.9, 22.6, 23.2, 23.7, 23.4, 24.0, 24.6, 24.3, 24.9, 25.4, 25.1,
]

const round2 = (value: number) => Math.round(value * 100) / 100

export const STRUCTURE_CANDLES: StoryCandle[] = CLOSES.map((close, index) => {
  const previousClose = index === 0 ? close - 0.2 : CLOSES[index - 1]
  const open = round2(previousClose + OPEN_OFFSET[index % OPEN_OFFSET.length])

  return {
    time: (BASE_TIME + index * DAY) as UTCTimestamp,
    open,
    close,
    high: round2(Math.max(open, close) + HIGH_OFFSET[index % HIGH_OFFSET.length]),
    low: round2(Math.min(open, close) - LOW_OFFSET[index % LOW_OFFSET.length]),
    volume: 80 + index * 3 + (index % 4) * 15,
  }
})

export const STRUCTURE_STAGES: StoryStage[] = [
  {
    id: 'range',
    endFrame: 6,
    title: '形成高低点',
    summary: '价格仍在原有范围中来回。',
    explanation:
      '这时价格只是形成了一段可以观察的区间，还不能因为一两根上涨的 K 线就判断状态已经改变。',
  },
  {
    id: 'internal-turn',
    endFrame: 11,
    title: '短线结构先发生变化',
    summary: '近期的小高点被重新突破。',
    explanation:
      '短周期的价格关系首先转强，但更主要的一层价格关系还没有被改变，还不能算完成确认。',
    eventLabel: '短线结构转强',
  },
  {
    id: 'major-confirm',
    endFrame: 17,
    title: '主要结构得到确认',
    summary: '更重要的前期高点被突破。',
    explanation:
      '当更大一级的价格关系也发生变化，主要结构才真正从原来的状态切换到新的状态。',
    eventLabel: '主要结构确认向上',
  },
  {
    id: 'continuation',
    endFrame: 24,
    title: '新的状态继续发展',
    summary: '高点和低点继续向上移动。',
    explanation:
      '结构变化是一个过程，不是某一根 K 线上的孤立标签。后续价格会继续验证，也可能重新否定这个状态。',
  },
]

export const STRUCTURE_EVENTS: StoryEvent[] = [
  { frame: 11, price: 22.0, label: '短线结构转强', tone: 'positive' },
  { frame: 17, price: 23.7, label: '主要结构确认', tone: 'positive' },
]
