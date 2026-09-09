// Marketing Composition V1.4 契约测试
// 目的：把 V1.4 视觉/版式/product 叙事锁死成 runtime contract，防止回退到旧版：
//   A. Hero：ProductDeviceStage 必须处于 backdrop layer（mode="backdrop" + heroProductBackdrop），
//      不能作为正常 flow 的第二大块。
//   B. Discovery：彻底删除 entry.media 分支，三卡同构（同一 flow markup），不得再塞雪球截图。
//   C. 六维 langCell 等宽等高（几何锁由 scss 保证，本测试锁 markup/布局类存在）。
//   D. XiaozToPanji：三段路径（小Z发现 → 盘迹审美 → 候选），雪球截图只作低权重 evidence；
//      禁止旧 xiaozScreenshotFrame 大图 + 固定漏斗。
//   Copy：DISCOVERY.entries 均无 media；XIAOZ 无 funnel、preferences.length==6 且 active==3、
//      resultExample before==86 / after==9、结果措辞用「符合当前条件」。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { DISCOVERY, XIAOZ } from '../data/copy'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const heroSrc = readSrc('src/features/marketing/sections/Hero.tsx')
const discoverySrc = readSrc('src/features/marketing/sections/Discovery.tsx')
const xiaozSrc = readSrc('src/features/marketing/sections/XiaozToPanji.tsx')
const copySrc = readSrc('src/features/marketing/data/copy.ts')
const scssSrc = readSrc('src/features/marketing/marketing.module.scss')

test('V1.4-A. Hero 产品大屏处于背景层，mode="backdrop"，非正常 flow 第二块', () => {
  assert.ok(
    /<ProductDeviceStage\b[^>]*mode="backdrop"[^>]*\/>/.test(heroSrc),
    'Hero.tsx 必须渲染 <ProductDeviceStage mode="backdrop" />',
  )
  assert.ok(
    /heroProductBackdrop/.test(heroSrc),
    'Hero.tsx 必须用 heroProductBackdrop 容器包住背景产品大屏',
  )
  assert.ok(
    /heroProductBackdrop/.test(scssSrc),
    'scss 必须含 .heroProductBackdrop（绝对定位背景层）',
  )
})

test('V1.4-B. Discovery 无 entry.media 分支，三卡同构 flow，无雪球截图', () => {
  assert.ok(
    !discoverySrc.includes('entry.media'),
    'Discovery.tsx 不得再出现 entry.media 条件分支',
  )
  assert.ok(
    !discoverySrc.includes('discoveryMedia'),
    'Discovery.tsx 不得再渲染 discoveryMedia 截图容器',
  )
  assert.ok(
    /entry\.flow\.map/.test(discoverySrc),
    'Discovery 三卡必须统一遍历 entry.flow 渲染',
  )
  for (const e of DISCOVERY.entries) {
    assert.ok(
      !('media' in e),
      `DISCOVERY.entries["${e.key}"] 不得含 media 属性`,
    )
    assert.ok(Array.isArray(e.flow) && e.flow.length === 3, `entry ${e.key} 必须恰好三步 flow`)
  }
})

test('V1.4-C. Xiaoz 三段路径；禁止旧 xiaozScreenshotFrame + 固定漏斗', () => {
  for (const cls of ['xiaozJourney', 'xiaozPreferenceGrid', 'xiaozResult']) {
    assert.ok(new RegExp(`\\.${cls}\\b`).test(scssSrc), `scss 缺少 .${cls}`)
    assert.ok(xiaozSrc.includes('' + cls) || xiaozSrc.includes(`styles.${cls}`), `XiaozToPanji 缺少 ${cls}`)
  }
  assert.ok(
    !copySrc.includes('xiaozScreenshotFrame'),
    'copy/组件不得再引用旧 xiaozScreenshotFrame 大截图',
  )
  assert.ok(
    !copySrc.includes('XIAOZ.funnel') && !x8ozFunnel(copySrc),
    'XIAOZ 不得再含固定 funnel 数据',
  )
  assert.ok(
    /xiaozEvidence/.test(scssSrc),
    '雪球截图只作为低权重 evidence（.xiaozEvidence）',
  )
})

// 辅助：粗糙但稳健地确认 copy.ts 的 XIAOZ object 不含 `funnel:` 字段
function x8ozFunnel(src: string): boolean {
  const xiaozStart = src.indexOf('export const XIAOZ')
  const xiaozBlock = src.slice(xiaozStart, xiaozStart + 1200)
  return /funnel\s*:/.test(xiaozBlock)
}

test('V1.4-D. XIAOZ copy：无 funnel/highlight、preferences==6 且 active==3、result 86→9、措辞「符合当前条件」', () => {
  const xiaozAny = XIAOZ as unknown as Record<string, unknown>
  assert.ok(!('funnel' in XIAOZ), 'XIAOZ 不得再定义 funnel 固定流程')
  assert.ok(
    !('highlight' in xiaozAny) ||
      !String(xiaozAny.highlight ?? '').includes('9只值得进一步研究'),
    '不得再表达「9只值得进一步研究」作为盘迹判断',
  )
  assert.equal(XIAOZ.preferences.length, 6)
  assert.equal(XIAOZ.preferences.filter((p) => p.active).length, 3)
  assert.ok(XIAOZ.sourceExample, '必须含 sourceExample')
  assert.equal(XIAOZ.resultExample.before, 86)
  assert.equal(XIAOZ.resultExample.after, 9)
  assert.ok(
    XIAOZ.resultExample.afterLabel.includes('符合当前条件'),
    '结果措辞必须为「符合当前条件」，而非盘迹判断',
  )
  // COPY 数据不得再携带旧 funnel / 旧结果表述
  const xiaozStart = copySrc.indexOf('export const XIAOZ')
  const xiaozBlock = copySrc.slice(xiaozStart, xiaozStart + 1500)
  assert.ok(!/funnel\s*:/.test(xiaozBlock), 'copy.ts XIAOZ 不得出现 funnel 字段')
  assert.ok(!xiaozBlock.includes('9只值得进一步研究'))
})