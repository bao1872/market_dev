// [BrandLogo] - 描述: 品牌标识组件契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/brandLogo.test.ts
//
// 覆盖（CHANGE-20260713-007 批准 Logo 资产）：
// 1. BrandLogo 源码引用批准 PNG 资产（logo_symbol_128.png + logo_horizontal_dark.png）
// 2. BrandLogo 源码不得包含手绘 SVG（polyline/circle/path 元素）
// 3. sidebar variant 使用 symbol 资产
// 4. landing/footer variant 使用 horizontal 资产
// 5. 批准 PNG 资产文件存在于 frontend/src/assets/brand/
//
// [视觉契约] - 描述: BrandLogo 必须使用 ref/盘迹品牌视觉资产包_v1.0/01_标志系统 中的批准资产
// 禁止恢复手绘 SVG 或在组件中重新构造标志几何

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const BRAND_LOGO_PATH = join(__dirname, '..', 'BrandLogo.tsx')
const ASSETS_DIR = join(__dirname, '..', '..', 'assets', 'brand')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. BrandLogo 引用批准 PNG 资产 =====
test('BrandLogo 源码引用 logo_symbol_128.png（sidebar variant）', () => {
  const src = readSource(BRAND_LOGO_PATH)
  assert.ok(
    src.includes('logo_symbol_128.png'),
    'BrandLogo 必须引用 logo_symbol_128.png 作为 sidebar variant 资产',
  )
})

test('BrandLogo 源码引用 logo_horizontal_dark.png（landing/footer variant）', () => {
  const src = readSource(BRAND_LOGO_PATH)
  assert.ok(
    src.includes('logo_horizontal_dark.png'),
    'BrandLogo 必须引用 logo_horizontal_dark.png 作为 landing/footer variant 资产',
  )
})

// ===== 2. BrandLogo 不得包含手绘 SVG =====
test('BrandLogo 源码不包含手绘 SVG polyline/circle/path 元素', () => {
  const src = readSource(BRAND_LOGO_PATH)
  // 禁止手绘 SVG 元素（polyline/circle/path/rect/line）
  assert.ok(
    !/<polyline[\s>]/.test(src),
    'BrandLogo 不得包含 <polyline> SVG 元素（必须使用批准 PNG 资产）',
  )
  assert.ok(
    !/<circle[\s>]/.test(src),
    'BrandLogo 不得包含 <circle> SVG 元素（必须使用批准 PNG 资产）',
  )
  assert.ok(
    !/<path[\s>]/.test(src),
    'BrandLogo 不得包含 <path> SVG 元素（必须使用批准 PNG 资产）',
  )
  assert.ok(
    !/<rect[\s>]/.test(src),
    'BrandLogo 不得包含 <rect> SVG 元素（必须使用批准 PNG 资产）',
  )
})

test('BrandLogo 使用 <img> 标签渲染批准 PNG 资产', () => {
  const src = readSource(BRAND_LOGO_PATH)
  assert.ok(
    /<img[\s>]/.test(src),
    'BrandLogo 必须使用 <img> 标签渲染批准 PNG 资产',
  )
})

// ===== 3. variant 区分 symbol vs horizontal =====
test('sidebar variant 使用 symbol 资产，landing/footer 使用 horizontal 资产', () => {
  const src = readSource(BRAND_LOGO_PATH)
  // 源码中应通过 isSidebar 或 variant === 'sidebar' 区分
  assert.ok(
    src.includes("variant === 'sidebar'") || src.includes("isSidebar"),
    'BrandLogo 应根据 variant 区分 symbol vs horizontal 资产',
  )
})

// ===== 4. 批准 PNG 资产文件存在 =====
test('批准 PNG 资产文件存在于 frontend/src/assets/brand/', () => {
  const symbolPath = join(ASSETS_DIR, 'logo_symbol_128.png')
  const horizontalPath = join(ASSETS_DIR, 'logo_horizontal_dark.png')
  assert.ok(
    existsSync(symbolPath),
    `logo_symbol_128.png 必须存在于 ${symbolPath}`,
  )
  assert.ok(
    existsSync(horizontalPath),
    `logo_horizontal_dark.png 必须存在于 ${horizontalPath}`,
  )
})

// ===== 5. 注释包含视觉真源引用 =====
test('BrandLogo 注释引用 ref/盘迹品牌视觉资产包_v1.0 作为视觉真源', () => {
  const src = readSource(BRAND_LOGO_PATH)
  assert.ok(
    src.includes('盘迹品牌视觉资产包_v1.0'),
    'BrandLogo 注释必须引用 ref/盘迹品牌视觉资产包_v1.0 作为视觉真源',
  )
})
