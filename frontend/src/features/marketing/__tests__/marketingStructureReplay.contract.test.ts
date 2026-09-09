// 真实结构回放契约测试（V1.2）
// 锁死本特性用户强调的三条关键要求：
//   1. 无未来函数（no look-ahead）：每帧只把「截至该历史前缀」的 bars 交给结构展示，
//      SMC DTO 必须与可见 bars 对齐，禁止把整段全量数据一次性注入。
//   2. 复用生产 StrategyChart + canonical SMC（不得另造 Canvas / 复制 SMC 算法）。
//   3. 只读静态 frozen JSON（MARKETING_MEDIA.structureReplay），绝不 fetch /v1/*。
// 同时把后端一次性导出脚本（backend/scripts/export_marketing_structure_replay.py）
// 也纳入契约：生产者必须走 CanonicalComputationService.compute(algorithm_id='smc')。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/
const REPLAY_PATH = 'public/marketing-media/zhongji-xuchuang-300308-1d-2y.json'

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const structureStory = readSrc(
  'src/features/marketing/sections/StructureStory.tsx',
)
const replayComponent = readSrc(
  'src/features/marketing/components/RealStructureReplay.tsx',
)
const copySrc = readSrc('src/features/marketing/data/copy.ts')
const exporterSrc = readSrc('../backend/scripts/export_marketing_structure_replay.py')

test('S1. StructureStory 只读静态 JSON，路径位于 media，绝不请求 /v1/*', () => {
  assert.ok(
    /fetch\(MARKETING_MEDIA\.structureReplay\)/.test(structureStory),
    'StructureStory 必须 fetch(MARKETING_MEDIA.structureReplay)',
  )
  assert.ok(
    !structureStory.includes('/v1/'),
    '结构回放不得请求公开行情 /v1/* API（一次性 frozen JSON）',
  )
  assert.ok(
    /structureReplay\b/.test(copySrc) &&
      /\/marketing-assets\/media\/zhongji-xuchuang-300308-1d-2y\.json/.test(
        copySrc,
      ),
    'MARKETING_MEDIA.structureReplay 必须位于 /marketing-assets/media/（公共门户只 serve media）',
  )
  assert.ok(
    !copySrc.includes('/marketing-assets/data/'),
    '不得引用 /marketing-assets/data/（nginx 未 serve 该目录，会 404）',
  )
})

test('S2. 无未来函数：每帧只渲染前缀 bars + 前缀视口', () => {
  assert.ok(
    /replay\.bars\.slice\(0,\s*frame\.endIndex\)/.test(structureStory),
    'StructureStory 必须按 frame.endIndex 截取前缀 bars（无未来数据）',
  )
  assert.ok(
    /buildReplayIndicators/.test(structureStory),
    'StructureStory 必须按帧构建 indicator 视图',
  )
  assert.ok(
    /createDefaultViewport\(currentBars\.length/.test(structureStory),
    '视口必须基于当帧 bars 数（不得引用全量 bars 数）',
  )
})

test('S3. 复用生产 StrategyChart（lazy），以显示 preset 限制图层，不复制 SMC 算法', () => {
  assert.ok(
    /const StrategyChart = lazy/.test(replayComponent),
    'RealStructureReplay 必须懒加载生产 StrategyChart',
  )
  assert.ok(
    /import\('@\/components\/StrategyChart'\)/.test(replayComponent),
    '必须 import @/components/StrategyChart（复用生产图表）',
  )
  assert.ok(
    /layerVisibility=\{REPLAY_LAYERS\}/.test(replayComponent),
    '必须传 layerVisibility 显示 preset（K线+成交量+结构）',
  )
  const combined = structureStory + replayComponent
  assert.ok(
    !/computeSMC|compute_smc|CanonicalComputationService|smc_pine/.test(combined),
    '前端不得复制/内嵌 SMC 算法实现（只消费静态 JSON + 复用生产图表）',
  )
})

test('S4-e. 后端导出脚本复用生产 canonical SMC + 真实 qfq 日线', () => {
  assert.ok(
    /CanonicalComputationService\.compute\(/.test(exporterSrc),
    '导出脚本必须调用生产 CanonicalComputationService.compute',
  )
  assert.ok(
    /algorithm_id=["']smc["']/.test(exporterSrc),
    '必须用 algorithm_id="smc"（注册的 canonical 算法）',
  )
  assert.ok(
    /get_bars\(/.test(exporterSrc) && /adj=["']qfq["']/.test(exporterSrc),
    '必须通过 MDAS get_bars 取真实 qfq 日线',
  )
  assert.ok(
    /display_bars=e/.test(exporterSrc) &&
      /prefix = full_df\.iloc\[:e\]/.test(exporterSrc),
    '必须逐前缀计算 SMC（无未来函数：只用前缀 bars）',
  )
  assert.ok(
    exporterSrc.includes('zhongji-xuchuang-300308-1d-2y.json') &&
      exporterSrc.includes('public/marketing-media'),
    '导出脚本输出必须落到 public/marketing-media/（build cp 的源目录）',
  )
})

test('S4-f. frozen JSON schema + 无未来函数审计（fail-closed：缺文件=特性未完成）', () => {
  const fullPath = resolve(FRONTEND_ROOT, REPLAY_PATH)
  let payload: any
  try {
    payload = JSON.parse(readFileSync(fullPath, 'utf-8'))
  } catch {
    assert.fail(
      `真实回放 JSON 缺失/非法（${REPLAY_PATH}）。` +
        '这是 V1.2 的必需产物：请先在注册 verify runtime 运行 ' +
        'backend/scripts/export_marketing_structure_replay.py 并把产物入库后再提交。',
    )
  }
  assert.equal(payload.schemaVersion, 1)
  assert.equal(payload.instrument.symbol, '300308')
  assert.equal(payload.timeframe, '1d')
  assert.equal(payload.adj, 'qfq')
  assert.ok(payload.bars.length >= 400, `bars 应覆盖近两年（>=400），实际 ${payload.bars.length}`)

  const times = payload.bars.map((b: any) => b.time)
  assert.ok(
    times.every((t: string, i: number) => i === 0 || t > times[i - 1]),
    'bars time 必须严格单调递增',
  )

  let prevEnd = 0
  for (const frame of payload.frames) {
    const e = frame.endIndex
    assert.ok(Number.isInteger(e) && e > prevEnd && e <= payload.bars.length,
      `frame.endIndex 必须严格递增且在 bars 范围内: ${prevEnd} -> ${e}`)
    assert.equal(frame.endTime, times[e - 1], `frame ${e} endTime 与 bars 对齐`)
    assert.equal(
      frame.smc.view.display_bars,
      e,
      `frame ${e} SMC display_bars 必须等于前缀长度（无未来）`,
    )
    assert.equal(
      frame.smc.view.total_bars,
      e,
      `frame ${e} SMC total_bars 必须等于前缀长度`,
    )
    assert.equal(frame.smc.time.length, e, `frame ${e} SMC time 必须覆盖前缀`)
    prevEnd = e
  }
  assert.ok(payload.frames.length >= 40, '回放帧数应足够支撑~50s 播放')
})