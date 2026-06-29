// [趋势选股] - 描述: 表格口径统一契约测试
// 用法：node --experimental-strip-types --test src/features/trend-selection/__tests__/contract.test.ts
//   覆盖：
//   1. 主页趋势字段必须是趋势选股字段的子集（visibleColumnKeys 机制）
//   2. 同 key 的标题、dataType 必须完全一致（源码扫描 columns.tsx）
//   3. 主页自选字段必须来自 watchlist-monitor 共享定义（IndexPage 不重新定义自选列）
//   4. 禁止 IndexPage 重新出现 DSA 字段转换和独立列定义（源码扫描）
//   5. visibleColumnKeys 按 column key 过滤，不依赖数组位置
//   6. adapter 唯一性：IndexPage 与 ScreenerPage 共用同一 adapter
//
// 测试策略：
// - 运行时测试：直接导入 adapters.ts / config.ts（仅类型导入，Node --experimental-strip-types 可执行）
// - 源码扫描测试：读取 columns.tsx / IndexPage.tsx / ScreenerPage.tsx 文本，正则验证契约
//   （columns.tsx 含 JSX 无法被 Node 直接执行，改用源码扫描验证列定义契约）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  adaptStrategyResultToTrendRow,
  pickPayload,
  toNum,
  fmtNum,
  fmtPct,
  fmtRatioAsPct,
  fmtChange,
  changePctColorClass,
} from '../adapters.ts'
import { INDEX_VISIBLE_COLUMN_KEYS, visibleColumnKeys } from '../config.ts'

// ===== 辅助：定位源码文件 =====
const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const COLUMNS_PATH = join(__dirname, '..', 'columns.tsx')
const INDEX_PAGE_PATH = join(__dirname, '..', '..', '..', 'pages', 'IndexPage.tsx')
const SCREENER_PAGE_PATH = join(__dirname, '..', '..', '..', 'pages', 'ScreenerPage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

/** 从 columns.tsx 源码中提取所有列定义的 key（按出现顺序） */
function extractColumnKeys(src: string): string[] {
  const keys: string[] = []
  // [趋势选股] - 描述: 匹配列定义对象中的 key: '...' 字段
  const re = /^\s*key:\s*['"]([^'"]+)['"]/gm
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    keys.push(m[1])
  }
  return keys
}

// ===== 1. 完整列集必须包含 spec 第七节要求的全部字段 =====
test('完整列集包含 spec 要求的全部 column key', () => {
  const src = readSource(COLUMNS_PATH)
  const keys = extractColumnKeys(src)
  // [趋势选股] - 描述: spec 第七节要求的完整列集（ScreenerPage 用）
  const expected = [
    'stock',
    'dsa_dir_bars',
    'vwap_ret_avg',
    'vwap_ret_total',
    'offset_mean',
    'offset_std',
    'offset_percentile',
    'dsa_vwap',
    'dsa_vwap_dev_pct',
    'offset_variance_rate',
    'price',
    'action',
  ]
  for (const k of expected) {
    assert.ok(keys.includes(k), `完整列集必须包含 column key="${k}"，实际 keys=${JSON.stringify(keys)}`)
  }
})

// ===== 2. 主页趋势字段必须是趋势选股字段的子集 =====
test('主页趋势字段是趋势选股字段的子集', () => {
  const src = readSource(COLUMNS_PATH)
  const fullKeys = new Set(extractColumnKeys(src))
  for (const k of INDEX_VISIBLE_COLUMN_KEYS) {
    assert.ok(
      fullKeys.has(k),
      `主页 visibleColumnKey="${k}" 必须存在于完整列集中，完整列 keys=${JSON.stringify([...fullKeys])}`,
    )
  }
})

// ===== 3. visibleColumnKeys 按 column key 过滤，不依赖数组位置 =====
test('visibleColumnKeys 按 column key 过滤，与数组顺序无关', () => {
  // 使用 mock 列数组测试函数逻辑（不依赖真实 columns.tsx）
  const mockCols = [
    { key: 'stock', title: '股票' },
    { key: 'dsa_dir_bars', title: '当前趋势' },
    { key: 'vwap_ret_avg', title: '日均趋势变化' },
    { key: 'offset_mean', title: '平均偏离趋势线' },
    { key: 'action', title: '操作' },
  ] as unknown as Parameters<typeof visibleColumnKeys>[0]
  // 故意打乱顺序传入，验证结果按完整列集顺序输出
  const reversed = [...INDEX_VISIBLE_COLUMN_KEYS].reverse()
  const filtered = visibleColumnKeys(mockCols, reversed)
  const filteredKeys = filtered.map((c) => c.key)
  // [趋势选股] - 描述: 用 Set<string> 做成员检查，避免 as const 字面量类型与 string 参数的 .includes 摩擦
  const indexKeySet = new Set<string>(INDEX_VISIBLE_COLUMN_KEYS)
  // 期望：filtered 中每个 key 都在 INDEX_VISIBLE_COLUMN_KEYS 中
  for (const k of filteredKeys) {
    assert.ok(
      indexKeySet.has(k),
      `过滤结果包含未请求的 key="${k}"`,
    )
  }
  // 期望：INDEX_VISIBLE_COLUMN_KEYS 中每个 key 都出现在过滤结果中
  for (const k of INDEX_VISIBLE_COLUMN_KEYS) {
    assert.ok(filteredKeys.includes(k), `过滤结果缺少请求的 key="${k}"`)
  }
  // 期望：过滤结果顺序与完整列集顺序一致（不依赖传入数组顺序）
  const expectedOrder = mockCols
    .filter((c) => indexKeySet.has(c.key))
    .map((c) => c.key)
  assert.deepEqual(
    filteredKeys,
    expectedOrder,
    '过滤结果顺序应与完整列集顺序一致，不受传入 key 顺序影响',
  )
})

// ===== 4. visibleColumnKeys 返回的列与完整列同 key 完全一致（同对象引用） =====
test('visibleColumnKeys 返回的列对象与完整列同 key 是同一引用', () => {
  const mockCols = [
    { key: 'stock', title: '股票' },
    { key: 'dsa_dir_bars', title: '当前趋势' },
    { key: 'vwap_ret_avg', title: '日均趋势变化' },
    { key: 'offset_mean', title: '平均偏离趋势线' },
    { key: 'action', title: '操作' },
  ] as unknown as Parameters<typeof visibleColumnKeys>[0]
  const filtered = visibleColumnKeys(mockCols, INDEX_VISIBLE_COLUMN_KEYS)
  for (const f of filtered) {
    const full = mockCols.find((c) => c.key === f.key)
    assert.ok(full, `完整列中找不到 key="${f.key}"`)
    assert.equal(
      f,
      full,
      `key="${f.key}" 的列对象必须是同一引用，确保 title/format/颜色规则完全一致`,
    )
  }
})

// ===== 5. adapter 唯一性：导出 adaptStrategyResultToTrendRow =====
test('导出唯一的 StrategyResult→TrendSelectionRow adapter', () => {
  assert.equal(typeof adaptStrategyResultToTrendRow, 'function', 'adapter 必须是函数')
  // 验证 adapter 返回对象包含必要字段
  const fakeResult = {
    id: 'r1',
    instrument_id: 'inst-1',
    instrument_name: '测试股票',
    instrument_symbol: '600000',
    instrument_market: 'SHA',
    payload: {
      dsa_dir_bars: 5,
      vwap_ret_avg: 0.035,
      vwap_ret_total: 0.12,
      offset_mean: 0.01,
      offset_std: 0.02,
      offset_percentile: 0.8,
      dsa_vwap: 10.5,
      dsa_vwap_dev_pct: 3.2,
      offset_variance_rate: 1.5,
      last_close: 10.8,
    },
  } as unknown as Parameters<typeof adaptStrategyResultToTrendRow>[0]
  const row = adaptStrategyResultToTrendRow(fakeResult, new Set(['inst-2']))
  assert.equal(row.instrumentId, 'inst-1', 'adapter 返回的行必须包含 instrumentId')
  assert.equal(row.resultId, 'r1', 'adapter 返回的行必须包含 resultId')
  assert.ok('payload' in row, 'adapter 返回的行必须保留 payload（供列渲染动态计算）')
  assert.equal(row.watched, false, 'watched 应根据 watchedIds 判断（inst-1 不在集合中）')

  // 验证 watchedIds 传入时 watched=true
  const row2 = adaptStrategyResultToTrendRow(fakeResult, new Set(['inst-1']))
  assert.equal(row2.watched, true, 'watched 应根据 watchedIds 判断（inst-1 在集合中）')

  // 验证 watchedIds 未传时 watched=false
  const row3 = adaptStrategyResultToTrendRow(fakeResult)
  assert.equal(row3.watched, false, 'watchedIds 未传时 watched 默认 false')
})

// ===== 6. 禁止 IndexPage 重新出现 DSA 字段转换和独立列定义 =====
test('IndexPage 不再包含独立 selectionColumns / toSelectionRow 定义', () => {
  const src = readSource(INDEX_PAGE_PATH)
  // [趋势选股] - 描述: spec 第七节明确禁止 IndexPage 重新手写另一套 SelectionRow 和 selectionColumns
  // 检查 1：禁止手写 selectionColumns 数组（useMemo(() => [ ...列定义对象... ])）
  // 允许：const selectionColumns = useMemo(() => visibleColumnKeys(...), ...)（引用共享模块）
  assert.ok(
    !/const\s+selectionColumns\s*=\s*useMemo\(\s*\(\)\s*=>\s*\[/.test(src),
    'IndexPage 禁止手写 selectionColumns 数组（应改用 visibleColumnKeys + getTrendSelectionColumns）',
  )
  // 检查 2：禁止定义自己的 adapter
  assert.ok(
    !/const\s+toSelectionRow\b/.test(src),
    'IndexPage 禁止重新定义 toSelectionRow（应改用 features/trend-selection adapter）',
  )
  // 检查 3：禁止定义自己的行接口
  assert.ok(
    !/interface\s+SelectionRow\b/.test(src),
    'IndexPage 禁止重新定义 SelectionRow 接口（应改用 TrendSelectionRow）',
  )
  // 检查 4：禁止出现旧版 IndexPage 专属列 key（direction/duration/avg_return/total_return）
  // 这些 key 已合并到共享模块的 dsa_dir_bars/vwap_ret_avg/vwap_ret_total
  assert.ok(
    !/key:\s*['"]direction['"]/.test(src),
    'IndexPage 禁止出现旧版 direction 列（已合并到共享模块 dsa_dir_bars）',
  )
  assert.ok(
    !/key:\s*['"]duration['"]/.test(src),
    'IndexPage 禁止出现旧版 duration 列（已合并到共享模块 dsa_dir_bars）',
  )
  assert.ok(
    !/key:\s*['"]avg_return['"]/.test(src),
    'IndexPage 禁止出现旧版 avg_return 列（已统一为共享模块 vwap_ret_avg）',
  )
})

// ===== 7. IndexPage 必须导入 features/trend-selection =====
test('IndexPage 导入 features/trend-selection 共享模块', () => {
  const src = readSource(INDEX_PAGE_PATH)
  assert.ok(
    src.includes("from '@/features/trend-selection'") ||
      src.includes("from '@/features/trend-selection/"),
    'IndexPage 必须从 @/features/trend-selection 导入共享列定义',
  )
})

// ===== 8. IndexPage 不得重复实现 DSA 字段候选 key 列表 =====
test('IndexPage 不再重复实现 DSA 字段候选 key 列表', () => {
  const src = readSource(INDEX_PAGE_PATH)
  // [趋势选股] - 描述: DSA 字段候选 key 列表（如 dsa_dir_bars/dsa_duration 等）属于 adapter 职责
  assert.ok(
    !src.includes("'dsa_dir_bars', 'dsa_duration'"),
    'IndexPage 不应重复出现 DSA 字段候选 key 列表（已迁移到 features/trend-selection/adapters）',
  )
  assert.ok(
    !src.includes("'vwap_ret_avg', 'dsa_avg_return'"),
    'IndexPage 不应重复出现 vwap_ret_avg 候选 key 列表',
  )
})

// ===== 9. ScreenerPage 的 dsaColumns 必须引用共享列定义 =====
test('ScreenerPage dsaColumns 引用 features/trend-selection 共享列定义', () => {
  const src = readSource(SCREENER_PAGE_PATH)
  assert.ok(
    src.includes("from '@/features/trend-selection'") ||
      src.includes("from '@/features/trend-selection/"),
    'ScreenerPage 必须从 @/features/trend-selection 导入共享列定义',
  )
})

// ===== 10. IndexPage 自选字段必须来自 watchlist-monitor 共享定义 =====
test('IndexPage 自选监控使用 watchlist-monitor 共享组件，不重新定义自选列', () => {
  const src = readSource(INDEX_PAGE_PATH)
  // [自选监控] - 描述: IndexPage 必须通过 WatchlistMonitorTable 共享组件引用自选列定义
  assert.ok(
    src.includes('WatchlistMonitorTable'),
    'IndexPage 必须使用 WatchlistMonitorTable 共享组件渲染自选监控',
  )
  assert.ok(
    !/const\s+\w*[Ww]atchlist\w*Columns\b/.test(src),
    'IndexPage 禁止重新定义 watchlist 列（应通过 WatchlistMonitorTable 共享组件引用）',
  )
})

// ===== 11. 颜色规则一致性：涨红跌绿（.market-up/.market-down） =====
test('列定义颜色规则遵循涨红跌绿（market-up/market-down）', () => {
  const src = readSource(COLUMNS_PATH)
  // [趋势选股] - 描述: 涨红跌绿规则，正数 market-up，负数 market-down，零或未知 market-flat
  assert.ok(src.includes('market-up'), '列渲染必须包含 market-up（涨红）')
  assert.ok(src.includes('market-down'), '列渲染必须包含 market-down（跌绿）')
  assert.ok(src.includes('market-flat'), '列渲染必须包含 market-flat（平/未知）')
})

// ===== 12. 完整列集每列必须含 key/title/dataType/sortable/filterable =====
test('完整列集每列含必需字段（key/title/dataType/sortable/filterable）', () => {
  const src = readSource(COLUMNS_PATH)
  const keys = extractColumnKeys(src)
  assert.ok(keys.length >= 12, `完整列集至少 12 列，实际 ${keys.length} 列`)
  // [趋势选股] - 描述: 每列必须含 key/title/dataType/sortable/filterable 五个字段
  // 通过正则验证源码中每个列定义块都包含这些字段
  for (const k of keys) {
    // 找到 key: 'k' 所在位置，向后检查 500 字符内的字段
    const idx = src.indexOf(`key: '${k}'`)
    assert.ok(idx >= 0, `找不到 key="${k}" 的列定义`)
    const block = src.substring(idx, idx + 800)
    assert.ok(/title:\s*['"]/.test(block), `key="${k}" 列必须含 title`)
    assert.ok(/dataType:\s*['"]/.test(block), `key="${k}" 列必须含 dataType`)
    assert.ok(/sortable:\s*(true|false)/.test(block), `key="${k}" 列必须含 sortable`)
    assert.ok(/filterable:\s*(true|false)/.test(block), `key="${k}" 列必须含 filterable`)
  }
})

// ===== 13. adapter 工具函数完整性与格式正确性 =====
test('adapter 工具函数（pickPayload/toNum/fmtNum/fmtPct/fmtRatioAsPct/fmtChange/changePctColorClass）', () => {
  // pickPayload：按候选 key 顺序取第一个非空值
  assert.equal(pickPayload({ a: null, b: '', c: 42 }, ['a', 'b', 'c']), 42)
  assert.equal(pickPayload({ a: 1 }, ['a', 'b']), 1)
  assert.equal(pickPayload({}, ['a', 'b']), undefined)

  // toNum：数字/字符串/空值转换
  assert.equal(toNum(3.14), 3.14)
  assert.equal(toNum('2.5'), 2.5)
  assert.equal(toNum(null), null)
  assert.equal(toNum(''), null)
  assert.equal(toNum('abc'), null)

  // fmtNum：保留指定小数位
  assert.equal(fmtNum(3.14159, 2), '3.14')
  assert.equal(fmtNum(null), '-')
  assert.equal(fmtNum(undefined), '-')

  // fmtPct：百分比（输入已是百分比数值，不 ×100）
  assert.equal(fmtPct(3.5), '3.50%')
  assert.equal(fmtPct(null), '-')

  // fmtRatioAsPct：ratio 小数 → 百分比（×100）
  assert.equal(fmtRatioAsPct(0.035), '3.50%')
  assert.equal(fmtRatioAsPct(null), '-')

  // fmtChange：涨跌幅（正数带 + 号）
  assert.equal(fmtChange(3.5), '+3.50%')
  assert.equal(fmtChange(-2.1), '-2.10%')
  assert.equal(fmtChange(0), '0.00%')
  assert.equal(fmtChange(null), '-')

  // changePctColorClass：涨红跌绿平灰
  assert.equal(changePctColorClass(3.5), 'market-up')
  assert.equal(changePctColorClass(-2.1), 'market-down')
  assert.equal(changePctColorClass(0), 'market-flat')
  assert.equal(changePctColorClass(null), 'market-flat')
})
