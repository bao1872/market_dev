// [WencaiBoardSyncContract] - 描述: 问财板块同步前端契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/wencaiBoardSyncContract.test.ts
//
// 覆盖：
//  1. MarketBoardsResponse 类型包含 source/stale/last_attempt_status 字段
//  2. MarketToolbar 行业/概念使用本地输入（Enter/blur 提交，非逐字符）
//  3. 行业显示将 `-` 渲染为 `/`（API 值不变）
//  4. stale 时 placeholder 显示"沿用上次板块数据"
//  5. 无效文本不提交（只接受当前目录精确值或空值）

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

const endpointsSrc = readFileSync(ENDPOINTS_PATH, 'utf-8')
const toolbarSrc = readFileSync(TOOLBAR_PATH, 'utf-8')
const pageSrc = readFileSync(PAGE_PATH, 'utf-8')

test('MarketBoardsResponse 包含 source/stale/last_attempt_status 字段', () => {
  assert.ok(endpointsSrc.includes('source: string | null'), 'MarketBoardsResponse 缺少 source 字段')
  assert.ok(endpointsSrc.includes('stale: boolean'), 'MarketBoardsResponse 缺少 stale 字段')
  assert.ok(
    endpointsSrc.includes('last_attempt_status: string | null'),
    'MarketBoardsResponse 缺少 last_attempt_status 字段',
  )
})

test('MarketToolbar 行业使用本地输入（非逐字符提交）', () => {
  // 行业输入应使用本地 state，onChange 只更新本地，不直接调用 onIndustryChange
  assert.ok(
    toolbarSrc.includes('industryInput') && toolbarSrc.includes('setIndustryInput'),
    '行业输入未使用本地 state',
  )
  // onChange 中不应直接调用 onIndustryChange（除非清空）
  assert.ok(
    !toolbarSrc.includes('onChange={(e) => onIndustryChange(e.target.value)}'),
    '行业输入不应逐字符提交',
  )
})

test('MarketToolbar 概念使用本地输入（非逐字符提交）', () => {
  assert.ok(
    toolbarSrc.includes('conceptInput') && toolbarSrc.includes('setConceptInput'),
    '概念输入未使用本地 state',
  )
  assert.ok(
    !toolbarSrc.match(/onChange={\(\) => onConceptChange\(e\.target\.value\)}/),
    '概念输入不应逐字符提交',
  )
})

test('MarketToolbar Enter 提交行业/概念', () => {
  assert.ok(
    toolbarSrc.includes("e.key === 'Enter'") && toolbarSrc.includes('commitIndustry'),
    '行业输入未在 Enter 时提交',
  )
  assert.ok(
    toolbarSrc.includes('commitConcept'),
    '概念输入未在 Enter 时提交',
  )
})

test('MarketToolbar blur 提交行业/概念', () => {
  assert.ok(
    toolbarSrc.includes('onBlur={() => commitIndustry'),
    '行业输入未在 blur 时提交',
  )
  assert.ok(
    toolbarSrc.includes('onBlur={() => commitConcept'),
    '概念输入未在 blur 时提交',
  )
})

test('MarketToolbar 清空立即提交', () => {
  // 清空时（v === ''）应立即调用 onIndustryChange('') / onConceptChange('')
  assert.ok(
    toolbarSrc.includes("if (v === '') onIndustryChange('')"),
    '行业清空未立即提交',
  )
  assert.ok(
    toolbarSrc.includes("if (v === '') onConceptChange('')"),
    '概念清空未立即提交',
  )
})

test('MarketToolbar 只接受当前目录精确值', () => {
  // commitIndustry/commitConcept 应校验输入是否在 industryNameSet/conceptNameSet 中
  assert.ok(
    toolbarSrc.includes('industryNameSet.has(trimmed)'),
    '行业提交未校验精确值',
  )
  assert.ok(
    toolbarSrc.includes('conceptNameSet.has(trimmed)'),
    '概念提交未校验精确值',
  )
})

test('MarketToolbar 行业显示将 - 渲染为 /', () => {
  assert.ok(
    toolbarSrc.includes('displayIndustryName') && toolbarSrc.includes("replace(/-/g, '/')"),
    '行业名称未将 - 渲染为 /',
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
