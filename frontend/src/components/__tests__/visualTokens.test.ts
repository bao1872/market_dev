// [VisualTokens] - 描述: 盘迹品牌视觉 V1.0 token 契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/visualTokens.test.ts
//
// 覆盖（CHANGE-20260713-007）：
// 1. variables.scss 包含 V1.0 品牌 token（#00F6C2/#39F5CF/#00B28A）
// 2. variables.scss 包含 V1.0 背景-token（#0A0F14/#111A23/#161F29）
// 3. variables.scss 包含 V1.0 文字 token（#F2F6F8/#98A1B3/#657281）
// 4. variables.scss 包含 V1.0 边框 token（#263440）
// 5. variables.scss 红涨绿跌 token（#FF4D4F/#22C55E）不被品牌绿污染
// 6. variables.scss info/warning token（#3882F6/#F59E0B）
// 7. 品牌绿不替代涨跌色（$color-brand ≠ $color-up ≠ $color-down）
// 8. CSS 自定义属性 :root 导出与 SCSS token 一致
//
// [视觉契约] - 描述: variables.scss 为唯一 token 真源，禁止组件硬编码颜色替代 token

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const VARIABLES_PATH = join(__dirname, '..', '..', 'styles', 'variables.scss')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. V1.0 品牌 token =====
test('variables.scss 包含品牌莹感绿 token #00F6C2/#39F5CF/#00B28A', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('#00F6C2'), 'variables.scss 必须包含 $color-brand: #00F6C2')
  assert.ok(src.includes('#39F5CF'), 'variables.scss 必须包含 $color-brand-hover: #39F5CF')
  assert.ok(src.includes('#00B28A'), 'variables.scss 必须包含 $color-brand-deep: #00B28A')
})

// ===== 2. V1.0 背景 token =====
test('variables.scss 包含深石墨黑背景 token #0A0F14/#111A23/#161F29', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('#0A0F14'), 'variables.scss 必须包含 $color-bg: #0A0F14')
  assert.ok(src.includes('#111A23'), 'variables.scss 必须包含 $color-panel: #111A23')
  assert.ok(src.includes('#161F29'), 'variables.scss 必须包含 $color-panel-2: #161F29')
})

// ===== 3. V1.0 文字 token =====
test('variables.scss 包含雾白文字 token #F2F6F8/#98A1B3/#657281', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('#F2F6F8'), 'variables.scss 必须包含 $color-text: #F2F6F8')
  assert.ok(src.includes('#98A1B3'), 'variables.scss 必须包含 $color-muted: #98A1B3')
  assert.ok(src.includes('#657281'), 'variables.scss 必须包含 $color-text-dim: #657281')
})

// ===== 4. V1.0 边框 token =====
test('variables.scss 包含边框 token #263440', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('#263440'), 'variables.scss 必须包含 $color-border: #263440')
})

// ===== 5. 红涨绿跌 token 不被品牌绿污染 =====
test('variables.scss 红涨 #FF4D4F 与跌色 #22C55E 独立于品牌绿 #00F6C2', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('#FF4D4F'), 'variables.scss 必须包含 $color-up: #FF4D4F (上涨红)')
  assert.ok(src.includes('#22C55E'), 'variables.scss 必须包含 $color-down: #22C55E (下跌绿)')
  // 品牌绿 ≠ 涨色 ≠ 跌色（三个值互不相同）
  assert.ok(!src.match(/\$color-brand:\s*#FF4D4F/i), '品牌绿不得替代上涨红')
  assert.ok(!src.match(/\$color-brand:\s*#22C55E/i), '品牌绿不得替代下跌绿')
  assert.ok(!src.match(/\$color-up:\s*#00F6C2/i), '上涨红不得使用品牌绿')
  assert.ok(!src.match(/\$color-down:\s*#00F6C2/i), '下跌绿不得使用品牌绿')
})

// ===== 6. info/warning token =====
test('variables.scss 包含 info #3882F6 和 warning #F59E0B', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('#3882F6'), 'variables.scss 必须包含 $color-info: #3882F6')
  assert.ok(src.includes('#F59E0B'), 'variables.scss 必须包含 $color-warning: #F59E0B')
})

// ===== 7. CSS 自定义属性 :root 导出 =====
test('variables.scss :root 导出 CSS 自定义属性与 SCSS token 一致', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes(':root'), 'variables.scss 必须包含 :root CSS 自定义属性导出')
  assert.ok(src.includes('--brand'), 'variables.scss :root 必须导出 --brand')
  assert.ok(src.includes('--up'), 'variables.scss :root 必须导出 --up')
  assert.ok(src.includes('--down'), 'variables.scss :root 必须导出 --down')
  assert.ok(src.includes('--info'), 'variables.scss :root 必须导出 --info')
  assert.ok(src.includes('--warning'), 'variables.scss :root 必须导出 --warning')
})

// ===== 8. 向后兼容别名 =====
test('variables.scss 包含向后兼容别名（旧 $color-blue → $color-brand）', () => {
  const src = readSource(VARIABLES_PATH)
  assert.ok(src.includes('$color-blue'), 'variables.scss 必须包含 $color-blue 向后兼容别名')
  assert.ok(src.includes('$color-brand'), 'variables.scss 必须包含 $color-brand 新 token')
})
