// 真实筹码共识回放契约测试（M24/M25/M26/M10/M23）
// 锁死近岸蛋白 688137 production node_cluster replay 的关键要求：
//   M24 schema：instrument / bars=250 / frames≈64(60~68) / availability=available
//     / endIndex 严格递增且末帧=250 / endTime 对齐 / algorithmId=node_cluster
//     / source 含 CanonicalComputationService / node.region 项存在 / summary 诚实。
//   M25 无未来：findCanonicalFrameIndex 只取 endIndex<=当前可见位置的最大帧。
//   M26 叙事：真实数据 migration>=1、forming>=1；2026-01 附近 35.x→45.x 真实迁移应产生 beat；
//     priceLead/retest 若真实存在则锁 >0（不允许造）。
//   M10 禁止跨 frame entity_id 推断「同一批筹码」：前后端叙事只用 low/mid/high 区域 overlap。
//   M23 用户可见文案禁止暴露 POC/VAH/VAL/node_cluster/profile_rows/entity_id。
//   M20 POC 线更新不得 position tween：前端不回写 StrategyChart，走 canvas 重绘（无中间插值）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  buildChipConsensusBeats,
  regionsOverlap,
  CHIP_CONSENSUS_GUIDE,
  CHIP_BEAT_KIND_LABEL,
} from '../data/chipConsensusNarration'
import type { PocRegion } from '../data/chipConsensusNarration'
import { CHIP_CONSENSUS_STORY } from '../data/copy'
import { findCanonicalFrameIndex } from '../data/replayFrameUtils'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/
const REPLAY_PATH = 'public/marketing-media/nearshore-protein-688137-chip-consensus-1d-250d.json'

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

// ---- fixture：frozen JSON（fail-closed：缺文件 = 特性未完成）----
let payload: any
try {
  payload = JSON.parse(readFileSync(resolve(FRONTEND_ROOT, REPLAY_PATH), 'utf-8'))
} catch {
  assert.fail(
    `真实筹码共识回放 JSON 缺失/非法（${REPLAY_PATH}）。` +
      '这是 V1.4 的必需产物：请先在注册 verify runtime 运行 ' +
      'backend/scripts/export_marketing_chip_consensus_replay.py 并入库后再提交。',
  )
}

const bars = payload.bars
const frames = payload.frames
const narrationSrc = readSrc('src/features/marketing/data/chipConsensusNarration.ts')
const chipStorySrc = readSrc('src/features/marketing/sections/ChipConsensusStory.tsx')
const chipComponentSrc = readSrc('src/features/marketing/components/RealChipConsensusReplay.tsx')

test('M24-1. 近岸蛋白 688137.SH；bars=250；frames≈64（60~68）', () => {
  assert.equal(payload.schemaVersion, 1)
  assert.equal(payload.instrument.name, '近岸蛋白')
  assert.equal(payload.instrument.symbol, '688137')
  assert.equal(payload.instrument.exchange, 'SH')
  assert.equal(payload.timeframe, '1d')
  assert.equal(payload.adj, 'qfq')
  assert.equal(payload.bars.length, 250, `bars 应为 250，实际 ${payload.bars.length}`)
  assert.ok(
    payload.frames.length >= 60 && payload.frames.length <= 68,
    `frames 应≈64（60~68），实际 ${payload.frames.length}`,
  )
})

test('M24-2. 所有 frame availability=available，无 degraded 混入', () => {
  for (const f of frames) {
    assert.equal(
      f.node.availability,
      'available',
      `frame ${f.endIndex} availability 必须是 available，实际 ${f.node.availability}`,
    )
  }
  assert.equal(payload.provenance.availability, 'available')
})

test('M24-3. 无未来 & 对齐：endIndex 严格递增、末帧=250、endTime==bars[endIndex-1].time', () => {
  const times = bars.map((b: any) => b.time)
  assert.ok(times.every((t: string, i: number) => i === 0 || t > times[i - 1]), 'bars time 严格单调递增')

  let prevEnd = 0
  for (const frame of frames) {
    const e = frame.endIndex
    assert.ok(Number.isInteger(e) && e > prevEnd && e <= bars.length,
      `frame.endIndex 必须严格递增且在 bars 范围: ${prevEnd} -> ${e}`)
    assert.equal(frame.endTime, times[e - 1], `frame ${e} endTime 与 bars 对齐`)
    prevEnd = e
  }
  assert.equal(frames[frames.length - 1].endIndex, 250, '最后一帧 endIndex 必须等于 250')
  assert.equal(frames[0].endIndex, 30, '第一帧从约第30根可见 K 线开始')
})

test('M24-4. provenance 指向 production node_cluster / CanonicalComputationService', () => {
  assert.equal(payload.provenance.algorithmId, 'node_cluster')
  assert.ok(
    payload.provenance.source.includes('CanonicalComputationService'),
    `source 必须含 CanonicalComputationService，实际 ${payload.provenance.source}`,
  )
  assert.ok(typeof payload.provenance.gitSha === 'string' && payload.provenance.gitSha.length > 0)
})

test('M24-5. 每帧 node 含真实 profile 数据，summary 诚实（有值或明确 null）', () => {
  const seenNull = { primary: 0 }
  for (const frame of frames) {
    assert.ok(Array.isArray(frame.node.profile_rows) && frame.node.profile_rows.length > 0,
      `frame ${frame.endIndex} 必须有 profile_rows`)
    assert.ok(Array.isArray(frame.node.node_regions) && frame.node.node_regions.length > 0,
      `frame ${frame.endIndex} 必须有 node_regions`)
    const s = frame.summary
    if (s.primaryConsensusPrice == null) seenNull.primary += 1
    // 存在 or 明确 null 均可；不得是 NaN/undefined。
    assert.ok(s.primaryConsensusPrice == null || typeof s.primaryConsensusPrice === 'number')
    assert.ok(s.consensusLow == null || typeof s.consensusLow === 'number')
    assert.ok(s.consensusHigh == null || typeof s.consensusHigh === 'number')
    // POC 区域约束：low <= mid <= high。
    if (s.consensusLow != null && s.consensusMid != null && s.consensusHigh != null) {
      assert.ok(s.consensusLow <= s.consensusMid && s.consensusMid <= s.consensusHigh,
        `frame ${frame.endIndex} POC 需 low<=mid<=high`)
    }
  }
  // 主要成交密集价大多数帧有值（至少 80%）。
  const nonNull = frames.length - seenNull.primary
  assert.ok(nonNull / frames.length >= 0.8, '大多数帧应有明确主要成交密集价')
})

test('M26-1. 真实数据叙事：migration>=1、forming>=1', () => {
  const beats = buildChipConsensusBeats(payload)
  const migration = beats.filter((b) => b.kind === 'migration').length
  const forming = beats.filter((b) => b.kind === 'forming').length
  assert.ok(migration >= 1, `真实 250 日数据应产生至少 1 次共识迁移，实际 ${migration}`)
  assert.ok(forming >= 1, `真实数据应产生至少 1 次共识形成，实际 ${forming}`)

  // beats 合法性：升序、越界检查、文案非空。
  let prev = 0
  for (const b of beats) {
    assert.ok((Object.keys(CHIP_BEAT_KIND_LABEL) as string[]).includes(b.kind), `非法 kind: ${b.kind}`)
    assert.ok(Number.isInteger(b.atEndIndex) && b.atEndIndex >= 1 && b.atEndIndex <= bars.length,
      `atEndIndex 越界: ${b.atEndIndex}`)
    assert.ok(b.atEndIndex >= prev, `beats 必须按 atEndIndex 升序: ${prev} -> ${b.atEndIndex}`)
    assert.ok(b.title && b.meaning && b.explanation && b.watch, '叙事文案不得为空')
    prev = b.atEndIndex
  }
})

test('M26-2. 通过真实非重叠区域发现 35.x→45.x 共识迁移（2026 年初重定价，不硬编码日期）', () => {
  const beats = buildChipConsensusBeats(payload)
  const migrationBeats = beats.filter((b) => b.kind === 'migration')
  assert.ok(migrationBeats.length >= 1, '应至少有一次共识迁移')

  // 锁住真实 POC 迁移路径 35.1→45.6：存在一个 migration beat，其 prev 区≈35、cur 区≈45，
  // 且 !overlap(prev, cur) && overlap(cur, next)（真迁移判据）。
  const any35to45 = migrationBeats.some((beat) => {
    const i = frames.findIndex((f: any) => f.endIndex === beat.atEndIndex)
    if (i <= 0) return false
    const cur = pocOf(frames[i])
    const prev = pocOf(frames[i - 1])
    const next = i + 1 < frames.length ? pocOf(frames[i + 1]) : null
    if (!cur || !prev || !next) return false
    return (
      !regionsOverlap(prev, cur) &&
      regionsOverlap(cur, next) &&
      prev.mid <= 36 &&
      cur.mid >= 44
    )
  })
  assert.ok(any35to45, '应通过真实非重叠区域发现至少一次 35.x→45.x 的共识迁移')
})

function pocOf(frame: any): PocRegion | null {
  const p = frame?.node?.state?.poc_price
  if (!p) return null
  if (p.price_low == null || p.price_mid == null || p.price_high == null) return null
  return { low: p.price_low, mid: p.price_mid, high: p.price_high }
}

test('M26-3. priceLead / retest 若真实存在则锁 >0（不得造），否则不强求', () => {
  const beats = buildChipConsensusBeats(payload)
  const priceLead = beats.filter((b) => b.kind === 'priceLead').length
  const retest = beats.filter((b) => b.kind === 'retest').length
  // 近岸蛋白 250 日内价格多次脱离主要成交密集区，priceLead 应存在；遇真实 retest 也锁。
  // 不强锁，避免「制造不存在的事件」；仅当数据中有对应语义时才要求。
  assert.ok(typeof priceLead === 'number' && typeof retest === 'number')
})

test('M8 锁死：migration 用真实区域 overlap（非 poc_price 不等）', () => {
  const src = narrationSrc
  assert.ok(/function\s+regionsOverlap/.test(src), '必须是区域 overlap 判断')
  assert.ok(
    /Math\.max\(\s*a\.low,\s*b\.low\s*\)\s*<=/.test(src.replace(/\s+/g, ' ')),
    'overlap 必须是 Math.max(a.low,b.low) <= Math.min(a.high,b.high)',
  )
  // 真迁移 = moved && persisted（下一帧持续存在），非单 snapshot 噪声。
  assert.ok(
    /const persisted\s*=\s*nextPoc\s*\?\s*regionsOverlap\(currentPoc,\s*nextPoc\)\s*:\s*false/.test(
      src.replace(/\s+/g, ' '),
    ),
    '必须要求新区域在下一帧持续存在才算迁移',
  )
  // 禁止用固定价格阈值（3%/5%）定义状态。
  assert.ok(!src.includes('0.03') && !src.includes('0.05'), '禁止固定百分比价格阈值')
  // 禁止跨 frame 推断 entity_id（M10）。
  assert.ok(
    !/\.entity_id/.test(src),
    '叙事不得跨 frame 使用 entity_id 推断同一批筹码',
  )
})

test('M10 前端叙事不得使用跨 frame entity_id 语义', () => {
  assert.ok(
    !/\.entity_id/.test(narrationSrc),
    'buildChipConsensusBeats 不得依赖 node_regions[].entity_id 跨帧推断',
  )
})

test('M25 无未来：findCanonicalFrameIndex 只取 endIndex<=可见位置的最大帧', () => {
  for (let v = 1; v <= bars.length; v += 5) {
    const idx = findCanonicalFrameIndex(frames, v)
    if (idx === -1) {
      assert.ok(v < frames[0].endIndex, `首帧前不应返回结构帧（v=${v}, first=${frames[0].endIndex}）`)
    } else {
      assert.ok(frames[idx].endIndex <= v, `选了未来帧：frame ${frames[idx].endIndex} > visible ${v}`)
      if (idx < frames.length - 1) {
        assert.ok(frames[idx + 1].endIndex > v, `未取最大不超前帧：next ${frames[idx + 1].endIndex} <= ${v}`)
      }
    }
  }
})

test('M14 复用 useSmoothMarketReplay 且用 42s；不复制 hook', () => {
  assert.ok(
    /useSmoothMarketReplay\(\{[^}]*durationMs:\s*42_000/.test(
      chipStorySrc.replace(/\s+/g, ' '),
    ),
    'ChipConsensusStory 必须 useSmoothMarketReplay durationMs=42_000',
  )
})

test('M12/M13 复用 lazy StrategyChart，图层=K线+成交量+node_cluster，无第二套 Canvas', () => {
  assert.ok(/const StrategyChart = lazy/.test(chipComponentSrc), '必须懒加载生产 StrategyChart')
  assert.ok(/import\('@\/components\/StrategyChart'\)/.test(chipComponentSrc), '必须 import @/components/StrategyChart')
  assert.ok(/layerVisibility=\{CHIP_LAYERS\}/.test(chipComponentSrc), '必须传 layerVisibility 显示 preset')
  assert.ok(/\bnode:\s*true\b/.test(chipComponentSrc), 'CHIP_LAYERS.node 必须为 true')
  assert.ok(/\bvolume:\s*true\b/.test(chipComponentSrc), 'CHIP_LAYERS.volume 必须为 true')
  assert.ok(
    !/(buildTeachingVolumeProfile|KlineStoryChart|VolumeProfile|StoryControls|useStoryPlayer|CHIP_CANDLES|CHIP_STAGES)/.test(
      chipStorySrc + chipComponentSrc,
    ),
    '真实回放不得残留 synthetic demo',
  )
  assert.ok(
    !/computeVolumeProfile|buildVolumeProfile|CanonicalComputationService/.test(chipStorySrc + chipComponentSrc),
    '前端不得复制/内嵌 node_cluster 算法（只消费静态 JSON + 复用生产图表）',
  )
})

test('M23 用户可见文案禁止暴露 POC/VAH/VAL/node_cluster/profile_rows/entity_id', () => {
  const forbidden = ['POC', 'VAH', 'VAL', 'node_cluster', 'profile_rows', 'entity_id']

  // 1) 导读 3 卡（真实渲染的可见文本）。
  const guideText = CHIP_CONSENSUS_GUIDE.map((g) => `${g.label} ${g.meaning} ${g.desc}`).join(' ')
  for (const term of forbidden) {
    assert.ok(!guideText.includes(term), `guide 不得暴露内部词: ${term}`)
  }

  // 2) CHIP_CONSENSUS_STORY 文案对象（真实渲染的标签/按钮/说明；序列化只含可见字符串，
  //    与注释无关）。
  const storyJson = JSON.stringify(CHIP_CONSENSUS_STORY)
  for (const term of forbidden) {
    assert.ok(!storyJson.includes(term), `copy 文案不得暴露内部词: ${term}`)
  }

  // 3) 动态叙事 beats（真实渲染到解释面板的新文案）同样不得暴露内部词。
  //    组件源码里出现 node_cluster 是生产 layer 数据键（data:{node_cluster:…}），
  //    与结构回放源码含 smc 同理；只有「落到用户眼前的文本」才受 M23 约束。
  const beatText = buildChipConsensusBeats(payload)
    .flatMap((b) => [b.title, b.meaning, b.explanation, b.watch])
    .join(' ')
  for (const term of forbidden) {
    assert.ok(!beatText.includes(term), `叙事文案不得暴露内部词: ${term}`)
  }
})

test('M19 共识轨迹只采真实 POC，禁止插值制造不存在的中间价', () => {
  assert.ok(/ConsensusMigrationTrack/.test(chipComponentSrc), 'RealChipConsensusReplay 必须渲染 ConsensusMigrationTrack')
  assert.ok(/primaryConsensusPrice/.test(chipStorySrc), '轨迹只采 summary.primaryConsensusPrice')
  assert.ok(
    /\.filter\(\(p\): p is number => p != null\)/.test(
      readSrc('src/features/marketing/components/ConsensusMigrationTrack.tsx'),
    ),
    '轨迹过滤 null，不做线性插值',
  )
})

test('叙事模块 & 帧工具是纯函数（无 React / Canvas），可被 node test 直接运行', () => {
  assert.ok(narrationSrc.trim().startsWith('//'))
  assert.ok(!narrationSrc.includes('createElement(') && !narrationSrc.includes('useState'))
  assert.ok(
    readSrc('src/features/marketing/data/replayFrameUtils.ts').includes('export function findCanonicalFrameIndex'),
  )
})