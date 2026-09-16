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
  // 直接检查稳定源码合同：列定义不得出现字面量 sortable: true（必须基于 def.dataType 推导）。
  // 不再用脆弱 regex 截取 return object（会因无关代码位移而误判）。
  assert.doesNotMatch(
    src,
    /sortable:\s*true/,
    'FP 列不得出现字面量 sortable: true（必须基于 def.dataType 推导）',
  )
})

test('FP 列 filterable 保持 true（筛选能力不受影响）', () => {
  const src = readSource(SRC_PATH)
  // 直接检查稳定源码合同，不用脆弱 regex 截取 return object。
  assert.match(src, /filterable:\s*true/, 'FP 列 filterable 必须保持 true')
})
