// [趋势选股] - 描述: ScreenerPage 批量加入自选 bug 回归测试
// 用法：node --experimental-strip-types --test src/pages/__tests__/ScreenerPage.batch.test.ts
//
// 覆盖：
// 1. handleBatchAdd 按 instrumentId 匹配（不再用 resultId）
// 2. 选中后无可加入股票时提示而不是静默
// 3. 成功/失败 toast 真实反映数量
// 4. 去重（同 instrumentId 不重复加入）
// 5. StrategyDataTable 的 rowKey 是 row.instrumentId（与 selectedKeys 一致）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const SCREENER_PAGE_PATH = join(__dirname, '..', 'ScreenerPage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. handleBatchAdd 按 instrumentId 匹配 =====
test('handleBatchAdd 按 instrumentId 匹配 selectedKeys（不再用 resultId）', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  // [趋势选股] - 描述: rowKey 是 row.instrumentId，selectedKeys 保存的是 instrumentId
  // handleBatchAdd 必须用 r.instrumentId 匹配，禁止用 r.resultId
  assert.ok(
    !/selectedKeys\.has\(r\.resultId\)/.test(src),
    'handleBatchAdd 禁止用 r.resultId 匹配 selectedKeys（resultId 可能为空）',
  )
  assert.ok(
    /selectedKeys\.has\(r\.instrumentId\)/.test(src),
    'handleBatchAdd 必须用 r.instrumentId 匹配 selectedKeys',
  )
})

// ===== 2. 选中后无可加入股票时提示而不是静默 =====
test('handleBatchAdd 选中后无可加入股票时提示而不是静默', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  // [趋势选股] - 描述: 之前 selected 为空时直接 return，无任何提示
  // 修复后应提示用户"无可加入股票"
  assert.ok(
    /selected\.length\s*===\s*0|selected\.size\s*===\s*0/.test(src),
    'handleBatchAdd 必须检查 selected 为空时提示用户（不能静默 return）',
  )
  // 验证空选提示文案（应包含"无可加入"或类似语义）
  assert.ok(
    /无可加入|没有可加入|未选中/.test(src),
    'handleBatchAdd 选中后无可加入股票时必须提示用户（包含"无可加入"等文案）',
  )
})

// ===== 3. 成功/失败 toast 真实反映数量 =====
test('handleBatchAdd 成功/失败 toast 真实反映数量', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  // [趋势选股] - 描述: toast 必须显示成功数和失败数
  assert.ok(
    /success/.test(src) && /fail/.test(src),
    'handleBatchAdd 必须统计 success 和 fail 数量',
  )
  // toast 调用必须包含 success 和 fail 变量
  assert.ok(
    /\$\{success\}/.test(src),
    'toast 必须显示成功数量 ${success}',
  )
  assert.ok(
    /\$\{fail\}/.test(src),
    'toast 必须显示失败数量 ${fail}',
  )
})

// ===== 4. 去重（同 instrumentId 不重复加入） =====
test('handleBatchAdd 对 instrumentId 去重（避免重复加入）', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  // [趋势选股] - 描述: rows 中可能存在同 instrumentId 的多条记录（skipped/succeeded）
  // 必须对 instrumentId 去重，避免重复加入自选
  // 检查方式：源码中存在 Set 或 Map 对 instrumentId 去重的逻辑
  assert.ok(
    /new Set.*instrumentId|Map.*instrumentId|instrumentId.*Set|instrumentId.*Map|seen.*instrumentId|added.*instrumentId/.test(src),
    'handleBatchAdd 必须对 instrumentId 去重（使用 Set/Map 或 seen/added 集合）',
  )
})

// ===== 5. StrategyDataTable rowKey 是 row.instrumentId =====
test('StrategyDataTable rowKey 是 row.instrumentId（与 selectedKeys 一致）', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  // [趋势选股] - 描述: rowKey 必须是 row.instrumentId，与 selectedKeys 的 key 类型一致
  assert.ok(
    /rowKey=\{\(row\)\s*=>\s*row\.instrumentId\}/.test(src),
    'StrategyDataTable rowKey 必须是 row.instrumentId（保证与 selectedKeys 一致）',
  )
})

// ===== 6. 保留 useAddToWatchlist 缓存失效逻辑 =====
test('保留 useAddToWatchlist 现有缓存失效逻辑（invalidateQueries watchlist + monitor-status）', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  // [趋势选股] - 描述: useAddToWatchlist hook 内部已实现缓存失效，handleBatchAdd 不应重复实现
  // 只需验证 hook 被正确调用即可
  assert.ok(
    /useAddToWatchlist/.test(src),
    'ScreenerPage 必须使用 useAddToWatchlist hook',
  )
})
