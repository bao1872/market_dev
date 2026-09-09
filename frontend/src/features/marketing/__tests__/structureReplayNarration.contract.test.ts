// Structure Replay 叙事契约测试（V1.3）
// 锁死：
//   1. 解释层复用生产 DTO（events / order_blocks）构建 beats，绝不重新计算/伪造结构。
//   2. buildStructureNarrativeBeats / findCanonicalFrameIndex 为纯函数、无未来函数。
//   3. 用户可见文案禁止暴露 BOS / CHoCH / Order Block。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  buildStructureNarrativeBeats,
  findCanonicalFrameIndex,
  keyOfEvent,
  STRUCTURE_GUIDE,
} from '../data/structureReplayNarration'

interface FixtureEvent {
  type: string
  bias: number
  internal?: boolean
  anchor_index: number
  anchor_time: string | null
  confirmed_index: number
  confirmed_time: string | null
  level?: number | null
}
interface FixtureFrame {
  endIndex: number
  endTime: string
  smc: {
    view: { display_bars: number; total_bars: number }
    events?: FixtureEvent[]
    order_blocks?: Array<{
      bias: number
      anchor_time: string
      bar_low: number
      bar_high: number
      internal?: boolean
      structureLevel?: string | null
      mitigated: boolean
    }>
  }
}

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/
const REPLAY_PATH = 'public/marketing-media/zhongji-xuchuang-300308-1d-2y.json'
const KIND_SET = ['context', 'battle', 'continuation', 'reversal'] as const

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const structureStory = readSrc('src/features/marketing/sections/StructureStory.tsx')
const replayComponent = readSrc('src/features/marketing/components/RealStructureReplay.tsx')
const narrationSrc = readSrc('src/features/marketing/data/structureReplayNarration.ts')

// ---- fixture：frozen JSON ----
let payload: any
try {
  payload = JSON.parse(
    readFileSync(resolve(FRONTEND_ROOT, REPLAY_PATH), 'utf-8'),
  )
} catch {
  assert.fail(
    `真实回放 JSON 缺失/非法（${REPLAY_PATH}）。` +
      '这是 V1.3 的必需产物：请先在注册 verify runtime 运行 exporter 再提交。',
  )
}
const bars = payload.bars as {
  time: string
  open: number
  high: number
  low: number
  close: number
}[]
const frames = payload.frames as FixtureFrame[]

const replay = { bars, frames }

test('Q1. frozen JSON 尺寸：bars=500、frames=101（或约定值）', () => {
  assert.equal(bars.length, 500)
  assert.equal(frames.length, 101)
})

test('Q3/4. 回放从第一根可见 K 线开始，播完整段近两年', () => {
  assert.ok(frames[0].endIndex <= 40, `第一帧 endIndex<=40，实际 ${frames[0].endIndex}`)
  assert.equal(frames[frames.length - 1].endIndex, bars.length, '最后一帧=500 播完整段')
})

test('Q5/6/7. 每帧：display_bars==endIndex、total_bars>endIndex（有 warmup）、endTime 对齐', () => {
  for (const frame of frames) {
    assert.equal(frame.smc.view.display_bars, frame.endIndex)
    assert.ok(
      frame.smc.view.total_bars > frame.smc.view.display_bars,
      `帧 ${frame.endIndex} 缺少历史 warmup`,
    )
    assert.equal(frame.endTime, bars[frame.endIndex - 1].time, `帧 ${frame.endIndex} endTime 对齐`)
  }
})

test('Q8. 无未来：所有 canonical 事件确认时间不晚于该帧日期', () => {
  for (const frame of frames) {
    for (const ev of frame.smc.events ?? []) {
      if (ev.confirmed_time == null) continue
      assert.ok(ev.confirmed_time <= frame.endTime, `帧 ${frame.endIndex} 存在未来事件 ${ev.confirmed_time}`)
    }
  }
})

test('Q9a. buildStructureNarrativeBeats 产出合法、按时间升序、不越界', () => {
  const beats = buildStructureNarrativeBeats(replay)
  let prev = 0
  for (const b of beats) {
    assert.ok((KIND_SET as readonly string[]).includes(b.kind), `非法 kind: ${b.kind}`)
    assert.ok(Number.isInteger(b.atEndIndex) && b.atEndIndex >= 1 && b.atEndIndex <= bars.length,
      `atEndIndex 越界: ${b.atEndIndex}`)
    assert.ok(b.atEndIndex >= prev, `beats 必须按 atEndIndex 升序: ${prev} -> ${b.atEndIndex}`)
    assert.ok(b.title.length > 0 && b.meaning.length > 0 && b.watch.length > 0)
    prev = b.atEndIndex
  }
})

test('Q9b. 事件叙事必须对应真实新增 BOS/CHoCH（不伪造；第一帧只 seed）', () => {
  const keyOf = (ev: FixtureEvent) => keyOfEvent(ev as Parameters<typeof keyOfEvent>[0])
  const seed = new Set<string>()
  for (const ev of frames[0].smc.events ?? []) seed.add(keyOf(ev))
  // 期望的事件级 beats 数 = 从第 2 帧起所有「新出现」事件数。
  let expectedEvents = 0
  const seen = new Set(seed)
  for (let i = 1; i < frames.length; i += 1) {
    for (const ev of frames[i].smc.events ?? []) {
      const key = keyOf(ev)
      if (!seen.has(key)) {
        seen.add(key)
        expectedEvents += 1
      }
    }
  }

  const beats = buildStructureNarrativeBeats(replay)
  const eventBeats = beats.filter((b) => b.kind === 'continuation' || b.kind === 'reversal')
  assert.equal(
    eventBeats.length,
    expectedEvents,
    `事件叙事数应与真实新增事件一致（不伪造）：期望 ${expectedEvents}，实际 ${eventBeats.length}`,
  )

  // 每个事件叙事都能在 DTO 里找到一个 confirmed_time 一致的真实事件。
  const eventTimes = new Set(
    frames.flatMap((f) => (f.smc.events ?? []).map((e) => e.confirmed_time)),
  )
  for (const b of eventBeats) {
    assert.ok(b.eventTime != null && eventTimes.has(b.eventTime), `事件叙事 eventTime 无对应 DTO 事件: ${b.eventTime}`)
  }
})

test('Q10. 用户可见文案禁止暴露 BOS / CHoCH / Order Block', () => {
  const guideText = STRUCTURE_GUIDE.map((g) => `${g.label} ${g.meaning} ${g.desc}`).join(' ')
  assert.ok(!/BOS|CHoCH|Order Block/gi.test(guideText), 'STRUCTURE_GUIDE 不得暴露英文结构术语')
  // 组件渲染文案（不含注释/导入）。
  assert.ok(!/Order Block|BOS|CHoCH/gi.test(replayComponent), 'RealStructureReplay 不得展示英文结构术语')
  assert.ok(!/Order Block|BOS|CHoCH/gi.test(structureStory), 'StructureStory 不得展示英文结构术语')
})

test('F. findCanonicalFrameIndex 无未来：只取 endIndex <= 可见位置的最大帧', () => {
  for (let v = 1; v <= bars.length; v += 7) {
    const idx = findCanonicalFrameIndex(frames, v)
    if (idx === -1) {
      // 首帧之前不存在可用结构帧：不得因为选了 index 0 而选中未来帧。
      assert.ok(v < frames[0].endIndex, `首帧前不应返回结构帧（v=${v}, first=${frames[0].endIndex}）`)
    } else {
      assert.ok(frames[idx].endIndex <= v, `选了未来帧：frame ${frames[idx].endIndex} > visible ${v}`)
      if (idx < frames.length - 1) {
        assert.ok(frames[idx + 1].endIndex > v, `未取最大不超前帧：next ${frames[idx + 1].endIndex} <= ${v}`)
      }
    }
  }
})

test('F2. 叙事模块是纯函数（无 React / Canvas），可被 node test 直接运行', () => {
  assert.ok(narrationSrc.trim().startsWith('//'))
  assert.ok(!narrationSrc.includes('rendererIndicator') && !narrationSrc.includes('createElement('))
})