import type { UTCTimestamp } from 'lightweight-charts'

/**
 * Marketing educational model only.
 *
 * 用于解释状态如何随 K 线和成交逐步形成。
 * 不是盘迹生产算法的重新实现，
 * 禁止业务页面复用。
 */

export type StoryCandle = {
  time: UTCTimestamp
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type StoryStage = {
  id: string
  /** 该阶段结束时的 frame；frame = 已展示的 K 线根数（不是数组下标） */
  endFrame: number

  title: string
  summary: string
  explanation: string

  eventLabel?: string
}

export type StoryEvent = {
  /** 事件出现在第几根 K 线（frame 语义，1-based） */
  frame: number
  price: number
  label: string

  tone: 'positive' | 'neutral' | 'negative'
}
