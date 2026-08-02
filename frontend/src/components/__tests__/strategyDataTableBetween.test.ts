// [StrategyDataTableBetween] - 描述: 区间（between）筛选双输入框与上下界校验契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/strategyDataTableBetween.test.ts
//
// 背景：number/percent 列的 filterSpec.input_control 由后端固定下发 'number_input'
// （backend/app/services/first_pyramid_flatten.py），此前 isNumberInput 不带 operator 约束，
// 导致 operator='between' 时先命中单输入框分支，「区间」只显示一个输入框。
//
// 覆盖（源码契约，不依赖具体字段名）：
//   1. isNumberInput 必须排除 between，使 between 统一落到双输入框分支
//   2. number/percent 的 between 渲染两个带明确标记（下界/上界）的输入
//   3. datetime 的 between 渲染两个带明确标记（起始日期/结束日期）的输入
//   4. between 任一值为空时提示且不提交、不静默清空
//   5. 数值下界大于上界时拦截并提示
//   6. 日期起始晚于结束时拦截并提示
//   7. 提交 payload 同时携带 value 与 value2
//   8. 不为特定字段硬编码；不存在第二套筛选器组件

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { test } from 'node:test'

function readSrc(...segments: string[]): string {
  return readFileSync(resolve(import.meta.dirname, '..', '..', ...segments), 'utf-8')
}

const src = readSrc('components', 'StrategyDataTable.tsx')

// ===== 1. isNumberInput 必须排除 between =====

test('isNumberInput 定义中排除 between，避免抢占双输入框分支', () => {
  const match = src.match(/const isNumberInput =[\s\S]*?\n\n/)
  assert.ok(match, '未找到 isNumberInput 定义')
  const decl = match[0]
  assert.ok(
    decl.includes('!isBetween'),
    'isNumberInput 必须包含 !isBetween 约束，否则 between 会命中单输入框分支',
  )
})

test('isBetween 在 isNumberInput 之前定义，确保约束生效', () => {
  const betweenIdx = src.indexOf('const isBetween =')
  const numberIdx = src.indexOf('const isNumberInput =')
  assert.ok(betweenIdx > -1, '未找到 isBetween 定义')
  assert.ok(numberIdx > -1, '未找到 isNumberInput 定义')
  assert.ok(betweenIdx < numberIdx, 'isBetween 必须先于 isNumberInput 定义')
})

test('between 判定基于 operator，而非任何具体字段名', () => {
  assert.ok(
    /const isBetween = operator === 'between'/.test(src),
    'isBetween 必须仅由 operator 决定',
  )
})

// ===== 2/3. 双输入框渲染（数值与日期两条路径）=====

test('数值区间渲染两个输入并带下界/上界标记', () => {
  const block = src.match(/\/\/ 区间输入[\s\S]*?\n {4}}/)
  assert.ok(block, '未找到数值 between 渲染分支')
  const decl = block[0]
  assert.ok(decl.includes('filter-between-inputs'), '缺少双输入容器')
  assert.equal(
    (decl.match(/<input/g) ?? []).length,
    2,
    '数值 between 必须渲染且仅渲染两个输入',
  )
  assert.ok(decl.includes('aria-label="下界"'), '缺少下界标记')
  assert.ok(decl.includes('aria-label="上界"'), '缺少上界标记')
  assert.ok(decl.includes('value={value}'), '下界必须绑定 value')
  assert.ok(decl.includes('value={value2}'), '上界必须绑定 value2')
})

test('日期区间渲染两个输入并带起始/结束标记', () => {
  const block = src.match(/if \(isDatePicker\) \{[\s\S]*?\n {6}\}/)
  assert.ok(block, '未找到日期 between 渲染分支')
  const decl = block[0]
  assert.ok(decl.includes('aria-label="起始日期"'), '缺少起始日期标记')
  assert.ok(decl.includes('aria-label="结束日期"'), '缺少结束日期标记')
  assert.ok(decl.includes('value={value2}'), '结束日期必须绑定 value2')
})

// ===== 4/5/6. 校验：空值、下界>上界、起始>结束 =====

const applyBlock = (() => {
  const m = src.match(/const handleApply = \(\) => \{[\s\S]*?\n {2}\}/)
  assert.ok(m, '未找到 handleApply')
  return m[0]
})()

test('between 任一值为空时提示且不提交、不静默清空', () => {
  assert.ok(
    /if \(!val \|\| !val2\) \{[\s\S]*?setError\(/.test(applyBlock),
    'between 空值必须 setError 提示',
  )
  const emptyBranch = applyBlock.match(/if \(!val \|\| !val2\) \{[\s\S]*?\n {6}\}/)
  assert.ok(emptyBranch, '未找到 between 空值分支')
  assert.ok(
    !emptyBranch[0].includes('onClear()'),
    'between 空值不得调用 onClear 静默清空已有筛选',
  )
  assert.ok(!emptyBranch[0].includes('onApply('), 'between 空值不得提交')
})

test('数值区间下界大于上界时拦截并提示', () => {
  assert.ok(applyBlock.includes('lower > upper'), '缺少下界>上界比较')
  assert.ok(applyBlock.includes('下界不能大于上界'), '缺少下界>上界提示文案')
  const guard = applyBlock.match(/if \(lower > upper\) \{[\s\S]*?\n {8}\}/)
  assert.ok(guard, '未找到下界>上界拦截分支')
  assert.ok(!guard[0].includes('onApply('), '下界>上界不得提交')
})

test('数值区间非数值输入被拦截', () => {
  assert.ok(applyBlock.includes('Number.isNaN'), '缺少 NaN 校验')
})

test('日期区间起始晚于结束时拦截并提示', () => {
  assert.ok(applyBlock.includes('起始日期不能晚于结束日期'), '缺少日期区间提示文案')
  assert.ok(
    /isDatePicker && String\(val\) > String\(val2\)/.test(applyBlock),
    '缺少日期起始>结束比较',
  )
})

// ===== 7. 提交 payload 同时携带 value 与 value2 =====

test('between 提交时同时携带 value 与 value2', () => {
  assert.ok(
    /onApply\(\{ key: column\.key, operator, value: val, value2: val2 \}\)/.test(applyBlock),
    'between 提交必须同时包含 value 与 value2',
  )
})

test('非 between 提交不携带 value2', () => {
  assert.ok(
    /onApply\(\{ key: column\.key, operator, value: val \}\)/.test(applyBlock),
    '非 between 提交只应携带 value',
  )
})

test('校验通过后清除错误态', () => {
  assert.ok(applyBlock.includes("setError('')"), '提交成功路径必须清除错误态')
})

// ===== 8. 通用性与单一组件约束 =====

test('错误提示节点存在且可被无障碍读取', () => {
  assert.ok(src.includes('className="filter-error"'), '缺少错误提示节点')
  assert.ok(src.includes('role="alert"'), '错误提示需带 role="alert"')
})

test('切换操作符时清除错误态', () => {
  const sel = src.match(/setOperator\(e\.target\.value as FilterOperator\)[\s\S]{0,80}/)
  assert.ok(sel, '未找到操作符切换处理')
  assert.ok(sel[0].includes("setError('')"), '切换操作符必须清除错误态')
})

test('between 逻辑不为任何具体字段名硬编码', () => {
  const forbidden = ['offset_percentile', 'change_pct', 'dsa_dir_bars', 'vwap_ret_avg']
  for (const key of forbidden) {
    assert.ok(
      !applyBlock.includes(key),
      `handleApply 不得对具体字段 ${key} 硬编码`,
    )
  }
})

test('仅存在一个筛选弹窗组件，未复制第二套', () => {
  assert.equal(
    (src.match(/function FilterPopover/g) ?? []).length,
    1,
    '不得复制第二套筛选器组件',
  )
  assert.equal(
    (src.match(/className="filter-between-inputs"/g) ?? []).length,
    2,
    '双输入容器只应出现在数值与日期两条 between 分支',
  )
})
