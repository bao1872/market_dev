// 营销教学动画契约测试（M2）
// 目的：锁死两个 story 的 deterministic 教学语义，防止以后为了视觉效果把逻辑改坏，
//       同防止内部术语 / 生产算法 / 实时数据回流到门户教学动画里。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  CHIP_CANDLES,
  CHIP_STAGES,
  buildTeachingVolumeProfile,
  getPrimaryConsensusPrice,
} from '../demo/chipConsensusStory'
import {
  STRUCTURE_CANDLES,
  STRUCTURE_EVENTS,
  STRUCTURE_STAGES,
} from '../demo/structureStory'

const __dirname = dirname(fileURLToPath(import.meta.url))
const DEMO_DIR = join(__dirname, '../demo')

// 内部术语，门户教学动画一律不得出现
const FORBIDDEN_TERMS = ['BOS', 'CHoCH', 'CHOCH', 'Order Block']

function demoSources(): { name: string; source: string }[] {
  return readdirSync(DEMO_DIR)
    .filter((file) => file.endsWith('.ts'))
    .map((file) => ({ name: file, source: readFileSync(join(DEMO_DIR, file), 'utf-8') }))
}

function assertMonotonic(stages: { id: string; endFrame: number }[], label: string) {
  for (let i = 1; i < stages.length; i += 1) {
    assert.ok(
      stages[i].endFrame > stages[i - 1].endFrame,
      `${label} 阶段 endFrame 必须严格单调递增: ${stages[i - 1].endFrame} -> ${stages[i].endFrame}`,
    )
  }
}

// ===== A / B. Structure story =====
test('structure 阶段 endFrame 严格单调递增', () => {
  assertMonotonic(STRUCTURE_STAGES, 'structure')
})

test('structure 最后一个 endFrame 等于 candles.length', () => {
  assert.equal(
    STRUCTURE_STAGES[STRUCTURE_STAGES.length - 1].endFrame,
    STRUCTURE_CANDLES.length,
  )
})

test('structure 每个阶段都有标题、摘要与解释', () => {
  for (const stage of STRUCTURE_STAGES) {
    assert.ok(stage.title.length > 0, `阶段 ${stage.id} 缺少标题`)
    assert.ok(stage.summary.length > 0, `阶段 ${stage.id} 缺少摘要`)
    assert.ok(stage.explanation.length > 0, `阶段 ${stage.id} 缺少解释`)
  }
})

test('structure 事件 frame 对应真实存在的 K 线', () => {
  for (const event of STRUCTURE_EVENTS) {
    assert.ok(
      event.frame >= 1 && event.frame <= STRUCTURE_CANDLES.length,
      `事件 frame 越界: ${event.frame}`,
    )
    assert.equal(
      STRUCTURE_CANDLES[event.frame - 1].close,
      event.price,
      `事件价格应落在该 K 线收盘价上: ${event.label}`,
    )
  }
})

// ===== C / D. Chip consensus story =====
test('chip 阶段 endFrame 严格单调递增且收尾于 candles.length', () => {
  assertMonotonic(CHIP_STAGES, 'chip')
  assert.equal(CHIP_STAGES[CHIP_STAGES.length - 1].endFrame, CHIP_CANDLES.length)
})

test('chip 成交重心随高位放量发生明显上移', () => {
  const early = getPrimaryConsensusPrice(
    buildTeachingVolumeProfile(CHIP_CANDLES.slice(0, 8)),
  )
  const final = getPrimaryConsensusPrice(buildTeachingVolumeProfile(CHIP_CANDLES))

  assert.ok(early !== null, '早期成交密集价不应为空')
  assert.ok(final !== null, '最终成交密集价不应为空')

  assert.ok(final > early + 2, `成交重心应明显上移: ${early} -> ${final}`)
})

test('chip 中段缩量：价格上行但成交重心仍在低位', () => {
  const stageOne = getPrimaryConsensusPrice(
    buildTeachingVolumeProfile(CHIP_CANDLES.slice(0, 8)),
  )
  const stageTwo = getPrimaryConsensusPrice(
    buildTeachingVolumeProfile(CHIP_CANDLES.slice(0, 14)),
  )

  assert.equal(stageOne, stageTwo)
})

// ===== E. 术语禁用 =====
test('story 数据不含内部结构术语', () => {
  const raw = JSON.stringify({
    structure: { STRUCTURE_STAGES, STRUCTURE_EVENTS },
    chip: { CHIP_STAGES },
  })

  for (const term of FORBIDDEN_TERMS) {
    assert.ok(!raw.includes(term), `story 数据不得包含内部术语: ${term}`)
  }
})

// ===== F. demo 源码禁用随机 / 实时数据 =====
test('demo 源码不使用 Math.random / Date.now / 实时接口', () => {
  for (const { name, source } of demoSources()) {
    assert.ok(!source.includes('Math.random'), `${name} 不得使用 Math.random`)
    assert.ok(!source.includes('Date.now'), `${name} 不得使用 Date.now`)
    assert.ok(!source.includes('new Date('), `${name} 不得使用 new Date(`)
    assert.ok(!source.includes('/v1/'), `${name} 不得请求实时接口`)
    assert.ok(!source.includes('fetch('), `${name} 不得发起网络请求`)
  }
})

test('demo 源码不含内部结构术语', () => {
  for (const { name, source } of demoSources()) {
    for (const term of FORBIDDEN_TERMS) {
      assert.ok(!source.includes(term), `${name} 不得出现内部术语: ${term}`)
    }
  }
})

test('demo 源码声明为教学模型（非生产算法）', () => {
  for (const { name, source } of demoSources()) {
    assert.ok(
      source.includes('Marketing educational model only'),
      `${name} 必须声明 Marketing educational model only`,
    )
  }
})

test('两个 story 的 K 线数量一致，便于统一播放节奏', () => {
  assert.equal(STRUCTURE_CANDLES.length, CHIP_CANDLES.length)
})
