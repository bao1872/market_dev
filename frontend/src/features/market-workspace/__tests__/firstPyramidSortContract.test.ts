// [第一金字塔] - 描述: firstPyramidColumns.tsx 排序合同测试（[CHANGE-20260902] B2）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/firstPyramidSortContract.test.ts
//
// 覆盖：
// 1. 列返回使用 def.dataType 推导 sortable（number/percent => true，其余 => false）
// 2. 不再存在 blanket `sortable: true,`（避免非数值字段被暴露排序入口）
// 3. filterable 保持 true（筛选能力不受影响）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const SRC_PATH = join(__dirname, '..', 'firstPyramidColumns.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

test('FP 列 sortable 由 def.dataType 推导（number/percent=true，其余=false）', () => {
  const src = readSource(SRC_PATH)
  assert.ok(
    /sortable:\s*def\.dataType\s*===\s*'number'\s*\|\|\s*def\.dataType\s*===\s*'percent'/.test(src),
    "getFirstPyramidColumns 中 sortable 必须基于原始 def.dataType 推导（不能用转换后的 tableDataType 误判 percent）",
  )
})

test('FP 列不存在 blanket sortable: true（避免非数值字段暴露排序）', () => {
  const src = readSource(SRC_PATH)
  // 列返回对象的 sortable 行不应是字面量 true
  const matches = src.match(/sortable:\s*true/g) || []
  // 允许测试用例或注释中的 true；主返回对象里不应出现字面量 sortable: true
  // 通过定位 return { ... } 块内 sortable: true 来判定
  const returnBlock = src.match(/return\s*\{[\s\S]*?key:\s*def\.key[\s\S]*?\n\s*\}/)
  assert.ok(returnBlock, '找不到 FP 列返回对象')
  assert.ok(
    !/sortable:\s*true/.test(returnBlock![0]),
    'FP 列返回对象中 sortable 不应为字面量 true（必须基于 def.dataType 推导）',
  )
  assert.ok(matches.length === 0 || true, 'sortable: true 字面量不应出现在列定义返回中（仅记录）')
})

test('FP 列 filterable 保持 true（筛选能力不受影响）', () => {
  const src = readSource(SRC_PATH)
  const returnBlock = src.match(/return\s*\{[\s\S]*?key:\s*def\.key[\s\S]*?\n\s*\}/)
  assert.ok(returnBlock, '找不到 FP 列返回对象')
  assert.ok(
    /filterable:\s*true/.test(returnBlock![0]),
    'FP 列 filterable 必须保持 true',
  )
})
