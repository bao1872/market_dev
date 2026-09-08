// [StrategyDataTable] - 描述: P0 corrective / Blocker 3 筛选器多选契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/strategyDataTableFilterContract.test.ts
//
// 覆盖用户裁决验收点：
//   - 多选 enum：显示 label（结构突破/结构转折），DOM 不出现 BOS/CHoCH 原始值作为用户文字
//   - 多选提交 canonical value（toggle 逻辑产出 BOS,CHoCH）
//   - 单选 enum：显示 结构转折，option value 提交 CHoCH（label 中文）
//   - boolean：显示 是/否，提交 true/false

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { getFirstPyramidColumns } from '../firstPyramidColumns.tsx'
import {
  FilterPopover,
  toggleMultiEnumValue,
} from '../../../components/StrategyDataTable.tsx'

// FilterPopover 渲染期读取 window.innerWidth/innerHeight，node 环境打桩（仅为 SSR 渲染，非真实 DOM）
;(globalThis as unknown as { window: unknown }).window = { innerWidth: 1280, innerHeight: 800 }

const SPECS = {
  fp_structure_event_type: {
    data_type: 'enum',
    operators: ['eq', 'neq', 'in', 'not_in'],
    enum_values: ['BOS', 'CHoCH', 'OB_CREATED', 'OB_ENTERED', 'OB_MITIGATED', 'EQH', 'EQL'],
    input_control: 'multi_select',
    value_normalizer: '',
  },
} as unknown as Parameters<typeof getFirstPyramidColumns>[0]

function renderFilter(operator: string, value: string): string {
  const cols = getFirstPyramidColumns(SPECS)
  const column = cols.find((c) => c.key === 'fp_structure_event_type') as unknown as Parameters<typeof FilterPopover>[0]['column']
  return renderToStaticMarkup(
    createElement(FilterPopover as never, {
      column,
      current: { key: 'fp_structure_event_type', operator, value },
      anchor: {
        getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
      } as never,
      onApply: () => {},
      onClear: () => {},
      onClose: () => {},
    }),
  )
}

// ===== 多选提交值（纯逻辑）=====
test('多选 enum：toggle 产出 canonical 值串 BOS,CHoCH', () => {
  assert.equal(toggleMultiEnumValue('', 'BOS'), 'BOS')
  assert.equal(toggleMultiEnumValue('BOS', 'CHoCH'), 'BOS,CHoCH')
  assert.equal(toggleMultiEnumValue('BOS,CHoCH', 'BOS'), 'CHoCH')
  assert.equal(toggleMultiEnumValue('BOS,,CHoCH', 'CHoCH'), 'BOS')
  assert.equal(toggleMultiEnumValue('BOS,CHoCH', 'NEW'), 'BOS,CHoCH,NEW')
})

// ===== 多选 DOM：显示 label，不泄露 raw code =====
test('多选 enum（in）：显示 结构突破/结构转折，DOM 可见文字不得为 BOS/CHoCH', () => {
  const html = renderFilter('in', '')
  assert.ok(html.includes('结构突破'), '应显示 结构突破 label')
  assert.ok(html.includes('结构转折'), '应显示 结构转折 label')
  // 用户可见文字不得是 raw canonical code（input value 属性不算可见文字）
  assert.ok(!html.includes('>BOS<'), 'DOM 可见文字不得出现 BOS')
  assert.ok(!html.includes('>CHoCH<'), 'DOM 可见文字不得出现 CHoCH')
})

test('多选 enum（in）已选 BOS,CHoCH：勾选态 + hint 中文', () => {
  const html = renderFilter('in', 'BOS,CHoCH')
  assert.ok(html.includes('checked'), '已选项应有 checked 态')
  assert.ok(html.includes('结构突破、结构转折'), '已选 hint 应显示中文 label')
})

// ===== 单选 DOM：显示 结构转折，option value 提交 CHoCH =====
test('单选 enum（eq）：option label 中文 结构转折，value 提交 canonical CHoCH', () => {
  const html = renderFilter('eq', '')
  assert.ok(html.includes('结构转折'), 'option label 应为中文 结构转折')
  assert.ok(html.includes('value="CHoCH"'), 'option value 必须为 canonical CHoCH')
  assert.ok(!html.includes('>CHoCH<'), '可见文字不得为原始 CHoCH')
})

// ===== boolean =====
test('boolean 筛选：option 显示 是/否，value 提交 true/false', () => {
  const cols = getFirstPyramidColumns({
    fp_chip_available: {
      data_type: 'boolean',
      operators: ['eq', 'empty', 'not_empty'],
      enum_values: [],
      input_control: 'boolean_toggle',
      value_normalizer: '',
    },
  } as unknown as Parameters<typeof getFirstPyramidColumns>[0])
  const column = cols.find((c) => c.key === 'fp_chip_available') as unknown as Parameters<typeof FilterPopover>[0]['column']
  const html = renderToStaticMarkup(
    createElement(FilterPopover as never, {
      column,
      current: { key: 'fp_chip_available', operator: 'eq', value: '' },
      anchor: {
        getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
      } as never,
      onApply: () => {},
      onClear: () => {},
      onClose: () => {},
    }),
  )
  assert.ok(html.includes('>是<') || html.includes('是'), '应显示 是')
  assert.ok(html.includes('>否<') || html.includes('否'), '应显示 否')
  assert.ok(html.includes('value="true"'), '是 提交 value=true')
  assert.ok(html.includes('value="false"'), '否 提交 value=false')
})
