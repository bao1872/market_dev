// Structure Replay Narration（V1.3）— 结构解释层。
//
// 原则：Marketing 只「解释」生产 SMC DTO，绝不重新计算或重定义结构语义。
//   - 事件（BOS/CHoCH）与承接/压制区（Order Block）的判定逻辑完全来自生产 DTO，
//     本模块只把它们翻译成用户能看懂的中文叙事（consuming，不 reimplement）。
//   - 中文语义真源是 smcLabels：BOS→多头突破/空头跌破、CHoCH→转强/转弱拐点、
//     Order Block→多头承接区/空头压制区；本模块不得再造一套翻译。
//   - 无未来函数：只消费「上一 canonical frame 已存在」的 Order Block 做 touch 判断；
//     事件只从新出现的 canonical 帧里检测（第一帧只 seed seen，不算新事件）。
//
// 本模块是纯函数（无 React / Canvas 依赖），可被 node --test 直接运行。

import {
  formatSmcEvent,
  formatSmcOrderBlock,
} from '@/components/smcLabels'
import type {
  SmcEvent,
  SmcOrderBlock,
} from '@/components/smcRendering'

export type BeatKind = 'context' | 'battle' | 'continuation' | 'reversal'

export interface StructureBeat {
  /** 该叙事对应可见窗口内第几根 K 线（0 基），用于 seek。 */
  atEndIndex: number
  kind: BeatKind
  /** 用户可见标题，来自 smcLabels 的 semantic.label（不暴露 BOS/CHoCH/OB）。 */
  title: string
  /** 一句话含义。 */
  meaning: string
  /** 发生了什么。 */
  explanation: string
  /** 接下来观察什么。 */
  watch: string
  /** 事件确认时间（事件类叙事），K 线时间（博弈叙事）。 */
  eventTime?: string | null
}

/** 只消费用于解释的 SMC 子集；其余字段不在乎。 */
type ReplaySmc = {
  events?: SmcEvent[]
  order_blocks?: SmcOrderBlock[]
  swing_bias?: number
}

/** 可见窗口内单根 K 线（与 MarketingReplayBar 同构的子集）。 */
interface ReplayBarLike {
  time: string
  open: number
  high: number
  low: number
  close: number
}

interface ReplayFrameLike {
  endIndex: number
  smc: unknown
}

export interface NarrativeReplayInput {
  bars: ReplayBarLike[]
  frames: ReplayFrameLike[]
}

// ===== 用户进入这一屏先看到的 3 个解释（不得暴露 BOS/CHoCH/Order Block）=====
export const STRUCTURE_GUIDE = [
  {
    label: '承接 / 压制区',
    meaning: '博弈位置',
    desc: '价格回到这里，观察承接或压制是否真正生效。',
  },
  {
    label: '结构突破 / 跌破',
    meaning: '趋势延续',
    desc: '原方向突破关键结构位置，延续得到进一步确认。',
  },
  {
    label: '转强 / 转弱拐点',
    meaning: '反转信号',
    desc: '原有结构被破坏，方向开始出现切换可能。',
  },
] as const

// ===== 叙事文案（唯一中文语义来源，辅助 smcLabels 的解释性描述）=====
const NARRATION_COPY = {
  continuation: {
    meaning: '趋势延续确认',
    explanation: '价格突破了前一个关键结构位置，当前方向继续得到结构确认。',
    watch: '接下来观察突破位置回踩能否守住，以及是否继续形成同方向的新高或新低。',
  },
  reversal: {
    meaning: '趋势反转信号',
    explanation: '原有结构关系被破坏，方向开始出现切换信号。',
    watch: '这不是“已经反转”的保证；继续观察新方向能否形成新的高低点关系。',
  },
  battle: {
    meaning: '进入博弈区',
    explanation: '当前K线触及结构中的承接/压制区域，买卖力量开始重新定价。',
    watchBullish: '观察承接是否成立；若有效跌破，原承接逻辑减弱。',
    watchBearish: '观察压制是否成立；若有效突破，原压制逻辑减弱。',
  },
} as const

/** 事件去重 key：锚点/确认时间 + 类型 + 方向 + 级别唯一标识一次结构事件。 */
export function keyOfEvent(event: SmcEvent): string {
  return [
    event.type,
    event.bias,
    event.internal ? 'internal' : 'swing',
    event.anchor_time,
    event.confirmed_time,
    event.level,
  ].join('|')
}

/** K 线与 Order Block 价格区间是否 overlap（无未来：只用上一帧已存在的 OB）。 */
export function overlaps(bar: ReplayBarLike, ob: SmcOrderBlock): boolean {
  return bar.low <= ob.bar_high && bar.high >= ob.bar_low
}

/** 取给定 endIndex 下最大的 canonical frame（endIndex <= visibleEndIndex），禁止未来帧。 */
export function findCanonicalFrameIndex(
  frames: ReadonlyArray<{ endIndex: number }>,
  visibleEndIndex: number,
): number {
  let lo = 0
  let hi = frames.length - 1
  let best = -1
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2)
    if (frames[mid].endIndex <= visibleEndIndex) {
      best = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return best
}

function obStructureLevel(ob: SmcOrderBlock): 'swing' | 'internal' | undefined {
  if (ob.structureLevel) return ob.structureLevel
  if (ob.internal === true) return 'internal'
  if (ob.internal === false) return 'swing'
  return undefined
}

/**
 * 构建整条时间线上的叙事 beats。
 *
 * - 事件类（continuation / reversal）：从第 2 个 canonical 帧开始检测「新出现」的
 *   BOS/CHoCH；第一帧只用于 seed seen 集合，不把历史一次性事件当成新事件。
 * - 博弈类（battle）：只用「上一 canonical 帧已存在的未失效 Order Block」去判断后续
 *   相邻 bar 的 price overlap，保证无未来函数。
 *
 * 返回按 atEndIndex 升序稳定的 beats（同一时刻按检测顺序）。
 */
export function buildStructureNarrativeBeats(
  replay: NarrativeReplayInput,
): StructureBeat[] {
  const beats: StructureBeat[] = []
  const seenEvents = new Set<string>()
  const touchedObs = new Set<string>()

  const frames = replay.frames
  if (frames.length === 0) return beats

  // 第一帧：seed seen（不产生叙事）。
  const firstSmc = frames[0].smc as ReplaySmc
  for (const event of firstSmc.events ?? []) {
    seenEvents.add(keyOfEvent(event))
  }

  for (let i = 1; i < frames.length; i += 1) {
    const frame = frames[i]
    const smc = frame.smc as ReplaySmc

    // ---- 事件叙事（J）----
    for (const event of smc.events ?? []) {
      const key = keyOfEvent(event)
      if (seenEvents.has(key)) continue
      seenEvents.add(key)

      const level = event.internal ? 'internal' : 'swing'
      const semantic = formatSmcEvent({
        type: event.type,
        bias: event.bias,
        structureLevel: level,
      })

      // 只有 BOS 是延续；其余（CHoCH 等）都是反转信号。
      const isContinuation = event.type === 'BOS'
      const copy = isContinuation
        ? NARRATION_COPY.continuation
        : NARRATION_COPY.reversal

      beats.push({
        atEndIndex: frame.endIndex,
        kind: isContinuation ? 'continuation' : 'reversal',
        title: semantic.label || '结构未知',
        meaning: copy.meaning,
        explanation: copy.explanation,
        watch: copy.watch,
        eventTime: event.confirmed_time,
      })
    }

    // ---- 博弈叙事（K）：只用上一帧已存在的未失效 OB ----
    const previousSmc = frames[i - 1].smc as ReplaySmc
    const activeObs = (previousSmc.order_blocks ?? []).filter((ob) => !ob.mitigated)
    if (activeObs.length > 0) {
      const startBar = Math.max(0, (frames[i - 1] as ReplayFrameLike).endIndex)
      const endBar = Math.min(replay.bars.length, frame.endIndex)
      for (let barIndex = startBar; barIndex < endBar; barIndex += 1) {
        const bar = replay.bars[barIndex]
        if (!bar) continue
        for (const ob of activeObs) {
          const obKey = [
            ob.anchor_time,
            ob.bias,
            ob.bar_low,
            ob.bar_high,
          ].join('|')
          if (touchedObs.has(obKey)) continue
          if (!overlaps(bar, ob)) continue
          touchedObs.add(obKey)

          const semantic = formatSmcOrderBlock({
            bias: ob.bias,
            structureLevel: obStructureLevel(ob),
          })

          beats.push({
            atEndIndex: barIndex + 1,
            kind: 'battle',
            title: semantic.label || '结构未知',
            meaning: NARRATION_COPY.battle.meaning,
            explanation: NARRATION_COPY.battle.explanation,
            watch:
              semantic.direction === 'bullish'
                ? NARRATION_COPY.battle.watchBullish
                : NARRATION_COPY.battle.watchBearish,
            eventTime: bar.time,
          })
        }
      }
    }
  }

  // 升序稳定排序（同 atEndIndex 保持先收集顺序）。
  return beats.sort((a, b) => a.atEndIndex - b.atEndIndex)
}