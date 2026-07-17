// [WencaiBoardSyncContract] - 描述: 问财板块同步前端契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/wencaiBoardSyncContract.test.ts
//
// CHANGE-20260716-007 更新：
//  1. industry 改为关键词匹配：不再校验 industryNameSet.has(trimmed)
//  2. concept 仍保持精确匹配：conceptNameSet.has(trimmed)
//  3. 行业 placeholder 改为"搜索行业关键词"
//  4. BoardFilterCombobox 替换原生 datalist，提供键盘导航 / 高亮 / a11y
//  5. 保留：source/stale/last_attempt_status 字段、stale 时"沿用上次板块数据"提示、disabled 行为

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

const ENDPOINTS_PATH = join(__dirname, '..', '..', '..', 'api', 'endpoints.ts')
const TOOLBAR_PATH = join(__dirname, '..', 'MarketToolbar.tsx')
const PAGE_PATH = join(__dirname, '..', 'MarketWorkspacePage.tsx')
const COMBOBOX_PATH = join(__dirname, '..', 'BoardFilterCombobox.tsx')

const endpointsSrc = readFileSync(ENDPOINTS_PATH, 'utf-8')
const toolbarSrc = readFileSync(TOOLBAR_PATH, 'utf-8')
const pageSrc = readFileSync(PAGE_PATH, 'utf-8')
const comboboxSrc = readFileSync(COMBOBOX_PATH, 'utf-8')

test('MarketBoardsResponse 包含 source/stale/last_attempt_status 字段', () => {
  assert.ok(endpointsSrc.includes('source: string | null'), 'MarketBoardsResponse 缺少 source 字段')
  assert.ok(endpointsSrc.includes('stale: boolean'), 'MarketBoardsResponse 缺少 stale 字段')
  assert.ok(
    endpointsSrc.includes('last_attempt_status: string | null'),
    'MarketBoardsResponse 缺少 last_attempt_status 字段',
  )
})

test('MarketToolbar 使用 BoardFilterCombobox 替换原生 datalist', () => {
  // 应导入并使用 BoardFilterCombobox
  assert.ok(
    toolbarSrc.includes("from './BoardFilterCombobox'") &&
      toolbarSrc.includes('<BoardFilterCombobox'),
    'MarketToolbar 未使用 BoardFilterCombobox',
  )
  // 不应再使用原生 datalist
  assert.ok(
    !toolbarSrc.includes('<datalist'),
    'MarketToolbar 不应再使用原生 datalist',
  )
  assert.ok(
    !toolbarSrc.includes('list="industry-options"'),
    'MarketToolbar 不应再使用 list 属性绑定 datalist',
  )
})

test('MarketToolbar 行业 placeholder 为"搜索行业关键词"', () => {
  assert.ok(
    toolbarSrc.includes('搜索行业关键词'),
    '行业 placeholder 未改为"搜索行业关键词"',
  )
})

test('MarketToolbar 行业不再校验 industryNameSet（关键词匹配）', () => {
  // 不应再出现 industryNameSet.has 校验
  assert.ok(
    !toolbarSrc.includes('industryNameSet.has'),
    '行业不应再使用 industryNameSet.has 校验精确值（关键词匹配）',
  )
  // 也不应再出现 industryNameSet 定义
  assert.ok(
    !toolbarSrc.includes('industryNameSet'),
    '行业不应再定义 industryNameSet',
  )
})

test('MarketToolbar 概念仍保持精确校验（在 Combobox 内）', () => {
  // Combobox 中 concept 模式应使用 conceptNameSet 校验
  assert.ok(
    comboboxSrc.includes('conceptNameSet') && comboboxSrc.includes('conceptNameSet.has'),
    '概念模式应保留 conceptNameSet.has 精确校验',
  )
})

test('MarketToolbar stale 时显示沿用提示', () => {
  assert.ok(
    toolbarSrc.includes('boardsStale'),
    '未读取 stale 状态',
  )
  assert.ok(
    toolbarSrc.includes('沿用上次板块数据'),
    'stale 时未显示"沿用上次板块数据"提示',
  )
})

test('MarketToolbar disabled 当 boards 不可用时', () => {
  assert.ok(
    toolbarSrc.includes('disabled={!boardsAvailable}'),
    'boards 不可用时未禁用输入',
  )
})

test('MarketWorkspacePage 传递 stale 到 toolbar', () => {
  assert.ok(
    pageSrc.includes('stale: boardsQuery.data.stale'),
    'MarketWorkspacePage 未传递 stale 到 toolbar',
  )
})

// ===== BoardFilterCombobox 契约（CHANGE-20260716-007）=====

test('BoardFilterCombobox 支持 industry / concept 两种模式', () => {
  assert.ok(
    comboboxSrc.includes("mode === 'industry'") && comboboxSrc.includes("mode === 'concept'"),
    'BoardFilterCombobox 未区分 industry / concept 模式',
  )
})

test('BoardFilterCombobox 行业模式接受任意关键词提交', () => {
  // industry 模式：commit 不应做精确校验
  assert.ok(
    comboboxSrc.includes('任意关键词都接受') ||
      comboboxSrc.includes('industry 模式：任意关键词都接受'),
    '行业模式未明确允许任意关键词提交',
  )
})

test('BoardFilterCombobox 支持键盘导航 ArrowUp/ArrowDown/Enter/Escape', () => {
  assert.ok(comboboxSrc.includes("e.key === 'ArrowDown'"), '未处理 ArrowDown')
  assert.ok(comboboxSrc.includes("e.key === 'ArrowUp'"), '未处理 ArrowUp')
  assert.ok(comboboxSrc.includes("e.key === 'Enter'"), '未处理 Enter')
  assert.ok(comboboxSrc.includes("e.key === 'Escape'"), '未处理 Escape')
})

test('BoardFilterCombobox 最多展示 12 条建议', () => {
  assert.ok(
    comboboxSrc.includes('MAX_SUGGESTIONS = 12'),
    '最多展示建议数应为 12',
  )
})

test('BoardFilterCombobox 行业展示将 - 渲染为 /', () => {
  assert.ok(
    comboboxSrc.includes('displayIndustryName') && comboboxSrc.includes("replace(/-/g, ' / ')"),
    '行业名称未将 - 渲染为 /',
  )
})

test('BoardFilterCombobox 支持清除按钮', () => {
  assert.ok(
    comboboxSrc.includes('comboboxClear') && comboboxSrc.includes('handleClear'),
    '未实现清除按钮',
  )
})

test('BoardFilterCombobox 解决 blur 先于 click 的问题', () => {
  // 应使用 mousedown preventDefault 或 blur 延迟
  assert.ok(
    comboboxSrc.includes('BLUR_COMMIT_DELAY_MS') || comboboxSrc.includes('onMouseDown'),
    '未解决 blur 先于 click 的问题',
  )
})

test('BoardFilterCombobox 提供 aria-combobox/listbox/option', () => {
  assert.ok(comboboxSrc.includes('role="combobox"'), '缺少 role="combobox"')
  assert.ok(comboboxSrc.includes('role="listbox"'), '缺少 role="listbox"')
  assert.ok(comboboxSrc.includes('role="option"'), '缺少 role="option"')
  assert.ok(comboboxSrc.includes('aria-expanded'), '缺少 aria-expanded')
  assert.ok(comboboxSrc.includes('aria-activedescendant'), '缺少 aria-activedescendant')
})

test('BoardFilterCombobox 点击外部关闭面板', () => {
  assert.ok(
    comboboxSrc.includes('handleClickOutside') || comboboxSrc.includes('mousedown'),
    '未实现点击外部关闭',
  )
})

test('BoardFilterCombobox 高亮命中关键词', () => {
  assert.ok(
    comboboxSrc.includes('highlightSegments') && comboboxSrc.includes('comboboxHighlight'),
    '未实现命中关键词高亮',
  )
})

test('BoardFilterCombobox 概念模式不逐字符请求后端', () => {
  // onChange 中不应直接调用 onChange（仅更新本地 state），清空除外
  assert.ok(
    !comboboxSrc.match(/onChange=\{[^}]*onChange\(/),
    'Combobox 不应逐字符调用 onChange 提交',
  )
})

test('BoardFilterCombobox 清空立即提交空值', () => {
  assert.ok(
    comboboxSrc.includes("v === ''") && comboboxSrc.includes("onChange('')"),
    '清空未立即提交空值',
  )
})
