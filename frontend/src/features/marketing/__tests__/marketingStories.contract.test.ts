// 营销教学动画契约测试（M2，V1.2 更新）
// 目的：锁死 ChipConsensusStory 的 deterministic 教学语义，防止以后为了视觉效果把逻辑改坏，
//       同时防止内部术语 / 生产算法 / 实时数据回流到门户教学动画里。
// [V1.2] StructureStory 已改为真实产品结构回放（real replay，见 marketingStructureReplay.contract.test.ts），
//        不再属于 synthetic 教学模型，因此本文件的 structure 断言已移除。
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

const __dirname = dirname(fileURLToPath(import.meta.url))
const DEMO_DIR = join(__dirname, '../demo')

// 内部术语，门户教学动画一律不得出现
const FORBIDDEN_TERMS = ['BOS', 'CHoCH', 'CHOCH', 'Order Block']

function demoSources(): { name: string; source: string }[] {
  return readdirSync(DEMO_DIR)
    .filter((file) => file.endsWith('.ts'))
    .map((file) => ({ name: file, source: readFileSync(join(DEMO_DIR, file), 'utf-8') }))
}

// ===== Chip consensus story =====
test('chip 阶段 endFrame 严格单调递增且收尾于 candles.length', () => {
  for (let i = 1; i < CHIP_STAGES.length; i += 1) {
    assert.ok(
      CHIP_STAGES[i].endFrame > CHIP_STAGES[i - 1].endFrame,
      `chip 阶段 endFrame 必须严格单调递增: ${CHIP_STAGES[i - 1].endFrame} -> ${CHIP_STAGES[i].endFrame}`,
    )
  }
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

// ===== 术语禁用 =====
test('demo 数据不含内部结构术语', () => {
  const raw = JSON.stringify({ chip: { CHIP_STAGES } })

  for (const term of FORBIDDEN_TERMS) {
    assert.ok(!raw.includes(term), `demo 数据不得包含内部术语: ${term}`)
  }
})

// ===== demo 源码禁用随机 / 实时数据 =====
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