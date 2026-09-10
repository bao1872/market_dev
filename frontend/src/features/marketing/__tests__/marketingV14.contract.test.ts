// Marketing Composition V1.4 契约测试（保留 + 局部简化）
// 保留仍有效的 V1.4 视觉/版式 guardrail：
//   A. Hero：ProductDeviceStage 必须处于 backdrop layer（mode="backdrop"+heroProductBackdrop）。
//   B. Discovery：彻底删除 entry.media 分支，三卡同构（同一 flow markup），不得再塞雪球截图。
// [V1.5] 独立 XiaozToPanji section 及 XIAOZ 数据已删除，相关契约随组件移除而退役。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { DISCOVERY } from '../data/copy'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const heroSrc = readSrc('src/features/marketing/sections/Hero.tsx')
const discoverySrc = readSrc('src/features/marketing/sections/Discovery.tsx')
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