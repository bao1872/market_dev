// [趋势选股] - 描述: IndexPage 趋势列与趋势选股共享模块的契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/index-page-columns.test.ts
// 覆盖：
// 1. IndexPage 不手写 SelectionRow / toSelectionRow / selectionColumns
// 2. IndexPage 不硬编码 DSA 候选 key 数组（复用 adapters 导出的常量）
// 3. IndexPage 从 @/features/trend-selection 导入指定函数/常量
// 4. IndexPage 自选相关引入仅来自 @/features/watchlist-monitor
// 5. INDEX_VISIBLE_COLUMN_KEYS 是完整趋势选股列 key 的严格子集
// 6. 同 key 的 title / dataType / format 一致
// 7. trend-selection 颜色函数遵循涨红跌绿平灰

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { INDEX_VISIBLE_COLUMN_KEYS } from '../../src/features/trend-selection/config.ts'
import {
  changePctColorClass,
  DIR_BARS_KEYS,
  VWAP_RET_AVG_KEYS,
  VWAP_RET_TOTAL_KEYS,
  OFFSET_MEAN_KEYS,
  OFFSET_PERCENTILE_KEYS,
} from '../../src/features/trend-selection/adapters.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const INDEX_PAGE_PATH = join(__dirname, '..', '..', 'src', 'pages', 'IndexPage.tsx')
const COLUMNS_PATH = join(__dirname, '..', '..', 'src', 'features', 'trend-selection', 'columns.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

interface ImportInfo {
  names: string[]
  module: string
}

/** 从源码中提取所有命名导入（支持多行、type 导入、as 别名） */
function extractImports(src: string): ImportInfo[] {
  const re = /import\s*(?:type\s+)?\{\s*([^}]+)\s*\}\s*from\s*['"]([^'"]+)['"]/g
  const result: ImportInfo[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    const names = m[1]
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => {
        const withoutType = s.replace(/^type\s+/, '').trim()
        return withoutType.split(/\s+as\s+/)[0].trim()
      })
      .filter(Boolean)
    result.push({ names, module: m[2] })
  }
  return result
}

function isFromTrendSelection(modulePath: string): boolean {
  return modulePath === '@/features/trend-selection' || modulePath.startsWith('@/features/trend-selection/')
}

function isFromWatchlistMonitor(modulePath: string): boolean {
  return modulePath === '@/features/watchlist-monitor' || modulePath.startsWith('@/features/watchlist-monitor/')
}

/** 从 columns.tsx 源码提取所有列 key（按出现顺序） */
function extractColumnKeys(src: string): string[] {
  const re = /^\s*key:\s*['"]([^'"]+)['"]/gm
  const keys: string[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    keys.push(m[1])
  }
  return keys
}

/** 提取 key 对应的列定义对象文本（按大括号深度匹配） */
function extractColumnBlock(src: string, key: string): string {
  const keyRe = new RegExp(`^\\s*key:\\s*['"]${key}['"]`, 'm')
  const match = keyRe.exec(src)
  assert.ok(match, `columns.tsx 中找不到 key="${key}" 的列定义`)
  // key 所在行之前即为列对象的起始 '{'，向后匹配到配对的 '}'
  let objectStart = match.index
  while (objectStart > 0 && src[objectStart] !== '{') {
    objectStart--
  }
  assert.ok(src[objectStart] === '{', `找不到 key="${key}" 的列定义开始位置`)
  let depth = 0
  let inString = false
  let stringChar = ''
  let escaped = false
  for (let i = objectStart; i < src.length; i++) {
    const ch = src[i]
    if (escaped) {
      escaped = false
      continue
    }
    if (ch === '\\') {
      escaped = true
      continue
    }
    if (inString) {
      if (ch === stringChar) inString = false
      continue
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = true
      stringChar = ch
      continue
    }
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) {
        return src.substring(objectStart, i + 1)
      }
    }
  }
  throw new Error(`无法找到 key="${key}" 的列定义结束位置`)
}

function extractTitle(block: string): string | undefined {
  const m = block.match(/title:\s*['"]([^'"]+)['"]/)
  return m?.[1]
}

function extractDataType(block: string): string | undefined {
  const m = block.match(/dataType:\s*['"]([^'"]+)['"]/)
  return m?.[1]
}

function extractFormatters(block: string): string[] {
  const names = [
    'fmtNum',
    'fmtPct',
    'fmtRatioAsPct',
    'fmtChange',
    'changePctColorClass',
    'renderStock',
    'renderDirBars',
  ]
  return names.filter((name) => block.includes(name))
}

const EXPECTED_VISIBLE_COLUMNS: Record<
  string,
  { title: string; dataType: string; formatters: string[] }
> = {
  stock: { title: '股票', dataType: 'text', formatters: ['renderStock'] },
  dsa_dir_bars: { title: '当前趋势', dataType: 'number', formatters: ['renderDirBars'] },
  vwap_ret_avg: {
    title: '日均趋势变化',
    dataType: 'percent',
    formatters: ['changePctColorClass', 'fmtRatioAsPct'],
  },
  offset_mean: {
    title: '平均偏离趋势线',
    dataType: 'percent',
    formatters: ['changePctColorClass', 'fmtRatioAsPct'],
  },
  action: { title: '操作', dataType: 'text', formatters: [] },
}

// ===== 1. IndexPage 不手写已废弃的 selection 抽象 =====
test('IndexPage 不定义 SelectionRow / toSelectionRow / selectionColumns', () => {
  const src = readSource(INDEX_PAGE_PATH)
  assert.ok(!/\bSelectionRow\b/.test(src), 'IndexPage 禁止出现 SelectionRow 接口/类型')
  assert.ok(!/\btoSelectionRow\b/.test(src), 'IndexPage 禁止定义 toSelectionRow adapter')
  assert.ok(!/\bselectionColumns\b/.test(src), 'IndexPage 禁止定义 selectionColumns')
})

// ===== 2. IndexPage 不从 trend-selection 以外硬编码候选 key 数组 =====
test('IndexPage 不硬编码 DSA 候选 key 数组', () => {
  const src = readSource(INDEX_PAGE_PATH)
  const candidateArrays = [
    DIR_BARS_KEYS,
    VWAP_RET_AVG_KEYS,
    VWAP_RET_TOTAL_KEYS,
    OFFSET_MEAN_KEYS,
    OFFSET_PERCENTILE_KEYS,
  ]
  for (const arr of candidateArrays) {
    if (arr.length >= 2) {
      const pattern = `['${arr[0]}', '${arr[1]}'`
      assert.ok(
        !src.includes(pattern),
        `IndexPage 不应硬编码候选 key 数组：${pattern}`,
      )
    }
  }
})

// ===== 3. IndexPage 从 trend-selection 导入指定函数/常量 =====
test('IndexPage 从 @/features/trend-selection 导入指定导出', () => {
  const src = readSource(INDEX_PAGE_PATH)
  const imports = extractImports(src)
  const trendSelection = imports.find((i) => isFromTrendSelection(i.module))
  assert.ok(trendSelection, 'IndexPage 必须从 @/features/trend-selection 导入')
  const required = [
    'adaptStrategyResultToTrendRow',
    'getTrendSelectionColumns',
    'visibleColumnKeys',
    'INDEX_VISIBLE_COLUMN_KEYS',
  ]
  for (const name of required) {
    assert.ok(
      trendSelection.names.includes(name),
      `IndexPage 必须从 @/features/trend-selection 导入 ${name}`,
    )
  }
})

// ===== 4. IndexPage 自选相关引入仅来自 watchlist-monitor =====
test('IndexPage 自选相关引入仅来自 @/features/watchlist-monitor', () => {
  const src = readSource(INDEX_PAGE_PATH)
  const imports = extractImports(src)
  const watchlistImports = imports.filter((i) => i.module.includes('watchlist'))
  assert.ok(watchlistImports.length > 0, 'IndexPage 必须导入自选监控共享模块')
  for (const imp of watchlistImports) {
    assert.ok(
      isFromWatchlistMonitor(imp.module),
      `IndexPage 自选相关导入必须来自 @/features/watchlist-monitor，实际来自 ${imp.module}`,
    )
  }
})

// ===== 5. INDEX_VISIBLE_COLUMN_KEYS 是完整列集的严格子集 =====
test('INDEX_VISIBLE_COLUMN_KEYS 是完整趋势选股列 key 的严格子集', () => {
  const columnsSrc = readSource(COLUMNS_PATH)
  const fullKeys = extractColumnKeys(columnsSrc)
  const visibleSet = new Set(INDEX_VISIBLE_COLUMN_KEYS)
  assert.equal(
    visibleSet.size,
    INDEX_VISIBLE_COLUMN_KEYS.length,
    'INDEX_VISIBLE_COLUMN_KEYS 不应包含重复 key',
  )
  for (const k of INDEX_VISIBLE_COLUMN_KEYS) {
    assert.ok(
      fullKeys.includes(k),
      `首页可见列 key="${k}" 必须存在于完整列集中（完整 keys=${JSON.stringify(fullKeys)}）`,
    )
  }
  assert.ok(
    INDEX_VISIBLE_COLUMN_KEYS.length < fullKeys.length,
    '首页可见列必须是完整列集的真子集',
  )
})

// ===== 6. 同 key 的 title / dataType / format 一致 =====
test('首页可见列与完整列定义 title / dataType / format 一致', () => {
  const columnsSrc = readSource(COLUMNS_PATH)
  for (const k of INDEX_VISIBLE_COLUMN_KEYS) {
    const expected = EXPECTED_VISIBLE_COLUMNS[k]
    assert.ok(expected, `未配置 key="${k}" 的期望契约`)
    const block = extractColumnBlock(columnsSrc, k)
    const title = extractTitle(block)
    const dataType = extractDataType(block)
    const formatters = extractFormatters(block)
    assert.equal(title, expected.title, `key="${k}" 的 title 不一致`)
    assert.equal(dataType, expected.dataType, `key="${k}" 的 dataType 不一致`)
    assert.deepEqual(
      formatters.sort(),
      expected.formatters.sort(),
      `key="${k}" 的 format 不一致`,
    )
  }
})

// ===== 7. 颜色函数：正收益红 / 负收益绿 / 未知灰 =====
test('changePctColorClass 涨红跌绿平灰', () => {
  assert.equal(changePctColorClass(0.05), 'market-up', '正收益应返回红色类名 market-up')
  assert.equal(changePctColorClass(-0.02), 'market-down', '负收益应返回绿色类名 market-down')
  assert.equal(changePctColorClass(0), 'market-flat', '零收益应返回灰色类名 market-flat')
  assert.equal(changePctColorClass(null), 'market-flat', 'null 应返回灰色类名 market-flat')
  assert.equal(
    changePctColorClass(undefined),
    'market-flat',
    'undefined 应返回灰色类名 market-flat',
  )
})
