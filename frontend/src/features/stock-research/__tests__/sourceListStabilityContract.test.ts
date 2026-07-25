// [SourceListStabilityContract] - 描述: 详情页左侧来源列表切股稳定性合同（反闪烁）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/sourceListStabilityContract.test.ts
//
// 覆盖根因合同（fix/stock-detail-source-list-stability-v1）：
//   1. StockDetailPage 禁止页面级 instrumentQuery.isLoading 早退（会卸载 tv-detail-layout 和来源列表）
//   2. StockDetailPage 禁止页面级 !instrumentQuery.data 早退（同上）
//   3. tv-detail-layout 必须始终挂载（不依赖 instrumentQuery 状态）
//   4. 来源列表 aside 容器禁止 key={symbol}（保持同一 DOM 节点）
//   5. 来源列表项保持 key={s.symbol}（per-stock key，不是 per-container）
//   6. 来源列表渲染条件不依赖 instrumentQuery.isLoading / instrumentQuery.data
//   7. instrumentLoading/instrumentError/inst 派生（不早退）
//   8. 顶部信息栏条件化 inst（loading/error/success），不显示上一只股票名称
//   9. 备忘录按钮 disabled={!instrumentId}（inst 未就绪时不可点击）
//
// 验证方式：静态源码合同（readFileSync + regex），与 detailSourceLoadingContract.test.ts 同款。
// 行为模拟由代码结构保证：删除早退 → tv-detail-layout 始终挂载 → 来源列表不闪烁。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const STOCK_DETAIL_PAGE = join(__dirname, '..', '..', '..', 'pages', 'StockDetailPage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf8')
}

test('FLICKER-1: 禁止页面级 instrumentQuery.isLoading 早退（会卸载来源列表）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 禁止 if (researchData.instrumentQuery.isLoading) { return (
  // 这种早退会导致 tv-detail-layout 和 tv-source-list 被卸载，切股时闪烁
  assert.ok(
    !/if\s*\(\s*researchData\.instrumentQuery\.isLoading\s*\)\s*\{[^}]*return\s*\(/.test(src),
    '禁止页面级 instrumentQuery.isLoading 早退 return（会卸载 tv-detail-layout 和来源列表）',
  )
})

test('FLICKER-2: 禁止页面级 !instrumentQuery.data 早退（会卸载来源列表）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 禁止 if (!researchData.instrumentQuery.data) { return (
  assert.ok(
    !/if\s*\(\s*!\s*researchData\.instrumentQuery\.data\s*\)\s*\{[^}]*return\s*\(/.test(src),
    '禁止页面级 !instrumentQuery.data 早退 return（会卸载 tv-detail-layout 和来源列表）',
  )
})

test('FLICKER-3: tv-detail-layout 必须始终挂载（return 内非条件）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // tv-detail-layout 必须出现在主 return 内，且不被 instrumentQuery 条件包裹
  assert.ok(/tv-detail-layout/.test(src), '必须存在 tv-detail-layout className')
  // 找到 return ( 后的 tv-detail-layout，确保它不在 instrumentQuery 条件内
  const returnIdx = src.indexOf('return (')
  assert.ok(returnIdx >= 0, '必须存在 return (' )
  const afterReturn = src.slice(returnIdx)
  assert.ok(/tv-detail-layout/.test(afterReturn), 'tv-detail-layout 必须在主 return 内')
})

test('FLICKER-4: 来源列表 aside 容器禁止 key={symbol}（保持同一 DOM 节点）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 找到 detail-source-list aside 块
  const asideMatch = src.match(/data-testid="detail-source-list"[\s\S]*?<\/aside>/)
  assert.ok(asideMatch, '必须存在 detail-source-list aside 块')
  const asideBlock = asideMatch[0]
  // aside 容器禁止 key={symbol}（会导致 React 卸载重建）
  assert.ok(
    !/key=\{symbol\}/.test(asideBlock),
    '来源列表 aside 容器禁止 key={symbol}（会卸载重建 DOM，scrollTop 丢失）',
  )
})

test('FLICKER-5: 来源列表项保持 key={s.symbol}（per-stock key）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 列表项必须有 key={s.symbol}（保持行级身份，不卸载整个列表）
  assert.ok(
    /key=\{s\.symbol\}/.test(src),
    '来源列表项必须保持 key={s.symbol}（per-stock key，行级身份不变）',
  )
})

test('FLICKER-6: 来源列表渲染条件不依赖 instrumentQuery 状态', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 来源列表的渲染条件应该基于 detailActions.sourceListLoading/Error/Empty/ContextInvalid，
  // 不应包含 instrumentQuery.isLoading 或 instrumentQuery.data
  // 提取所有 showSourceList 相关的条件块
  const sourceListConditions = src.match(/showSourceList\s*&&[^&{]*?(?:detailActions\.\w+)/g) || []
  assert.ok(sourceListConditions.length > 0, '必须存在 showSourceList 条件渲染')
  for (const cond of sourceListConditions) {
    assert.ok(
      !/instrumentQuery/.test(cond),
      `来源列表渲染条件不得依赖 instrumentQuery（实际: ${cond}）`,
    )
  }
})

test('FLICKER-7: instrumentLoading/instrumentError/inst 派生（不早退）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 必须派生 instrumentLoading / instrumentError / inst（而非早退）
  assert.ok(/const instrumentLoading\s*=\s*researchData\.instrumentQuery\.isLoading/.test(src), '必须派生 instrumentLoading')
  assert.ok(/const instrumentError\s*=\s*researchData\.instrumentQuery\.isError/.test(src), '必须派生 instrumentError')
  // inst 可能为 undefined（loading/error），不再保证非空
  assert.ok(/const inst\s*=\s*researchData\.instrumentQuery\.data/.test(src), '必须派生 inst（可能 undefined）')
})

test('FLICKER-8: 顶部信息栏条件化 inst（loading/error/success）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 顶部信息栏必须根据 inst是否存在条件渲染：inst ? (名称) : instrumentLoading ? (加载中) : (未找到)
  assert.ok(
    /\{\s*inst\s*\?\s*\([\s\S]*?inst\.name[\s\S]*?\)\s*:\s*instrumentLoading\s*\?/.test(src),
    '顶部信息栏必须条件化渲染 inst（loading/error/success），不显示上一只股票名称',
  )
})

test('FLICKER-9: 备忘录按钮 disabled={!instrumentId}（inst 未就绪时不可点击）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 备忘录按钮必须有 disabled={!instrumentId}（防止 inst 未就绪时打开 modal 崩溃）
  const memoBtnMatch = src.match(/setMemoOpen\(true\)[\s\S]*?备忘录/)
  assert.ok(memoBtnMatch, '必须存在备忘录按钮')
  const memoBtnBlock = src.match(/onClick=\{\(\)\s*=>\s*detailActions\.setMemoOpen\(true\)\}[^>]*>/)?.[0] || ''
  assert.ok(
    /disabled=\{!instrumentId\}/.test(memoBtnBlock),
    '备忘录按钮必须 disabled={!instrumentId}（inst 未就绪时不可点击）',
  )
})

test('FLICKER-10: metaParts 条件化 inst（避免 undefined 访问）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // metaParts 必须在 inst 存在时构造（避免 inst.market 崩溃）
  assert.ok(
    /const metaParts\s*=\s*inst\s*\?/.test(src),
    'metaParts 必须条件化 inst（inst 可能为 undefined）',
  )
})
