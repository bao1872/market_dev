// [PANJI-REVIEW-UI-UNIFY] ScopeExplorer 表头筛选 / slim meta bar 契约测试。
// 用法：tsx --test src/features/market-dashboard/__tests__/explorerHeaderFilter.test.ts
//
// 覆盖：
//   A 独立 filter-panel（MA5/MA10/5日Δ/成员数 min-max + 应用筛选 + 重置）已整体删除
//   B 只有 backend 真实支持的 5 列有 funnel；MA20/MA50/MA120 不得出现假筛选
//   C 条件（区间 / ≥ / ≤ / =）→ 仍写原 <col>_min / <col>_max URL params
//   D 清除列筛选删除对应 URL params，其余保留
//   E 快速筛选仍调用原有 MA5_PRESETS.filters（不建第二套 preset 逻辑）
//   F 排序行为不变
//   G slim meta bar：结果数 + chips + 快速筛选 ▾ + 清除筛选
//   H activeExplorerFilterChips 行为（区间 / 单边界 / 多列聚合）
//   H2 两个 5日Δ 列的 chip 必须可分辨（用 filterLabel 而非列表头 label）
//   I 列筛选弹层：条件（区间/≥/≤/=）+ 最小/最大值 + 清除/应用
//   J URL 仍是唯一正式状态（不引入 global store / localStorage）

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import {
  activeExplorerFilterChips,
  draftFromFilterMode,
  EXPLORER_FILTERABLE_COLUMNS,
  FILTER_COLUMN_BY_FIELD,
  filterKeysForColumn,
  isExplorerColumnFiltered,
  NUMERIC_FILTER_KEYS,
  parseIndustryExplorerSearch,
  serializeExplorerState,
  uiToRatio,
  updateExplorerState,
  type NumericFilterKey,
} from '../scopeExplorerUrlState'

function readSrc(...segments: string[]): string {
  return readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', ...segments),
    'utf-8',
  )
}

const PAGE_SRC = readSrc('features', 'market-dashboard', 'ScopeExplorerPage.tsx')
const TABLE_SRC = readSrc('features', 'market-dashboard', 'ScopeExplorerTable.tsx')
const MODULE_SCSS = readSrc('features', 'market-dashboard', 'dashboard.module.scss')
// 去掉注释后的源码，供「不得再出现」类断言使用：
// 文档里为说明历史而提到旧实现（如「独立筛选面板已删除」）不应被判定为违规。
const PAGE_CODE = codeOnly(PAGE_SRC)
const MODULE_CODE = codeOnly(MODULE_SCSS)

/** 去掉行注释与块注释（JSX 注释块与 SCSS 行注释同样处理）。 */
function codeOnly(src: string): string {
  return src
    .replace(/\{?\/\*[\s\S]*?\*\/\}?/g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '')
}

function filtersWith(
  partial: Partial<Record<NumericFilterKey, number | null>>,
): Record<NumericFilterKey, number | null> {
  const base = {} as Record<NumericFilterKey, number | null>
  for (const k of NUMERIC_FILTER_KEYS) base[k] = null
  return { ...base, ...partial }
}

function parse(search: string) {
  return parseIndustryExplorerSearch(new URLSearchParams(search))
}

// ===== A. 独立 filter-panel 已删除 =====
test('A. 独立 filter-panel 整块已删除', () => {
  assert.ok(!PAGE_CODE.includes('filter-panel'), '不得再渲染 filter-panel')
  assert.ok(!PAGE_CODE.includes('FilterGroup'), '独立面板的 FilterGroup 表单控件必须删除')
  assert.ok(!PAGE_CODE.includes('应用筛选'), '「应用筛选」按钮必须删除')
  assert.ok(!PAGE_CODE.includes('重置'), '独立面板的「重置」必须删除（改为 meta bar 的清除筛选）')
  assert.ok(!MODULE_CODE.includes('.filter-panel'), 'filter-panel 样式必须删除')
  assert.ok(!MODULE_CODE.includes('.filter-row'), 'filter-row 样式必须删除')
  assert.ok(!MODULE_CODE.includes('.filter-input-pair'), 'filter-input-pair 样式必须删除')
})

// ===== B. 诚实筛选面 =====
test('B. 只有 backend 真实支持的列有 funnel（MA20/MA50/MA120 无假筛选）', () => {
  assert.deepEqual(
    [...EXPLORER_FILTERABLE_COLUMNS],
    ['member_count', 'ma5', 'ma5_delta', 'ma10', 'ma10_delta'],
  )
  assert.deepEqual(Object.keys(FILTER_COLUMN_BY_FIELD).sort(), [
    'ma10',
    'ma10_delta',
    'ma5',
    'ma5_delta',
    'member_count',
  ])
  for (const field of ['name', 'ma20', 'ma50', 'ma120']) {
    assert.equal(
      FILTER_COLUMN_BY_FIELD[field as keyof typeof FILTER_COLUMN_BY_FIELD],
      undefined,
      `${field} 不得有 funnel（后端无对应 filter 参数）`,
    )
  }
  // 表格只在 FILTER_COLUMN_BY_FIELD 命中时才渲染 funnel
  assert.match(TABLE_SRC, /const filterColumn = FILTER_COLUMN_BY_FIELD\[field\]/)
  assert.match(TABLE_SRC, /\{filterColumn && \(/)
  assert.match(TABLE_SRC, /data-testid=\{`filter-\$\{filterColumn\}`\}/)
})

// ===== C. 条件 → 原 URL params =====
test('C. 条件（区间 / ≥ / ≤ / =）仍写原 min/max URL params', () => {
  assert.deepEqual(draftFromFilterMode('range', '60', '80'), { min: '60', max: '80' })
  assert.deepEqual(draftFromFilterMode('gte', '60', ''), { min: '60', max: '' })
  assert.deepEqual(draftFromFilterMode('lte', '', '80'), { min: '', max: '80' })
  assert.deepEqual(draftFromFilterMode('eq', '60', '80'), { min: '60', max: '60' })

  assert.deepEqual(filterKeysForColumn('ma5'), { min: 'ma5_min', max: 'ma5_max' })
  assert.deepEqual(filterKeysForColumn('member_count'), {
    min: 'member_count_min',
    max: 'member_count_max',
  })

  // UI 值 → backend ratio → URL（业务 query contract 完全不变）
  const next = updateExplorerState(parse('sort=ma5&direction=desc'), {
    filters: { ma5_min: uiToRatio('ma5_min', '60'), ma5_max: uiToRatio('ma5_max', '80') },
  })
  const serialized = serializeExplorerState(next, 'industry').toString()
  assert.ok(serialized.includes('ma5_min=0.6'), `应写 ma5_min=0.6，实际 ${serialized}`)
  assert.ok(serialized.includes('ma5_max=0.8'), `应写 ma5_max=0.8，实际 ${serialized}`)
  assert.equal(next.page, 1, '应用筛选必须回到第 1 页')

  // 成员数为整数（不缩放）
  assert.equal(uiToRatio('member_count_min', '30'), 30)
  assert.equal(uiToRatio('ma5_delta_min', '-3'), -0.03)
})

// ===== D. 清除 =====
test('D. 清除单列筛选删除对应 URL params，其余保留', () => {
  const before = filtersWith({ ma5_min: 0.6, ma5_max: 0.8, ma10_min: 0.5 })
  assert.equal(isExplorerColumnFiltered('ma5', before), true)
  assert.equal(isExplorerColumnFiltered('member_count', before), false)

  const cleared = updateExplorerState(parse('ma5_min=0.6&ma5_max=0.8&ma10_min=0.5'), {
    filters: { ma5_min: null, ma5_max: null },
  })
  const serialized = serializeExplorerState(cleared, 'industry').toString()
  assert.ok(!serialized.includes('ma5_min'), `ma5_min 必须被移除，实际 ${serialized}`)
  assert.ok(!serialized.includes('ma5_max'), `ma5_max 必须被移除，实际 ${serialized}`)
  assert.ok(serialized.includes('ma10_min=0.5'), `未清除的 ma10_min 必须保留，实际 ${serialized}`)
})

// ===== E. 快速筛选已删除（无第二套 preset）=====
test('E. 快速筛选 / MA5_PRESETS 菜单已从 Review 移除', () => {
  assert.ok(!PAGE_SRC.includes('MA5_PRESETS'), 'MA5_PRESETS 不得再被渲染')
  assert.ok(!PAGE_SRC.includes('quick-filter'), '不得保留 quick-filter 状态/菜单接线')
  assert.ok(!PAGE_SRC.includes('presetOpen'), '不得保留 preset 弹层状态')
  assert.ok(!PAGE_SRC.includes('quickFilterRef'), '不得保留 quick-filter 外部点击 ref')
  assert.match(TABLE_SRC, /data-testid=\{`filter-\$\{filterColumn\}`\}/, '筛选只走列 funnel')
})

// ===== F. 排序不变 =====
test('F. 排序行为不变（三分支 onSort 保留）', () => {
  assert.match(PAGE_SRC, /const onSort = \(field: ExplorerParsed\['sort'\]\) => \{/)
  assert.match(PAGE_SRC, /applyPatch\(\{ direction: parsed\.direction === 'asc' \? 'desc' : 'asc' \}\)/)
  assert.match(PAGE_SRC, /applyPatch\(\{ sort: field, direction: field === 'name' \? 'asc' : 'desc' \}\)/)
  // 排序仍写 URL 的 sort/direction
  const next = updateExplorerState(parse(''), { sort: 'ma20', direction: 'asc' })
  const serialized = serializeExplorerState(next, 'industry').toString()
  assert.ok(serialized.includes('sort=ma20') && serialized.includes('direction=asc'))
})

// ===== G. meta bar 复用 global.scss 表格 chrome（无第二套 Review 实现）=====
test('G. slim meta bar：复用 table-meta-bar + 全局 filter-chip，无 kebab module 查找', () => {
  // 复用行情 table chrome 的 meta bar / chips 视觉 owner
  assert.match(PAGE_SRC, /className="table-meta-bar"/, 'meta bar 复用全局 table-meta-bar')
  assert.match(PAGE_SRC, /className="filter-chip"/, 'chips 复用全局 filter-chip')
  assert.ok(!PAGE_SRC.includes("styles['filter-chip']"), 'filter-chip 必须走全局类，不得用 kebab module 查找')
  // 旧 module 副本已从 dashboard.module.scss 删除（P2）
  assert.ok(!MODULE_CODE.includes('.explorer-meta-bar'), '旧 module .explorer-meta-bar 必须删除')
  assert.ok(!MODULE_CODE.includes('.explorer-filter-chips'), '旧 module .explorer-filter-chips 必须删除')
  assert.ok(!MODULE_CODE.includes('.filter-chip'), '旧 module .filter-chip 副本必须删除（全局才是 owner）')
  // 既有契约保持
  assert.match(PAGE_SRC, /data-testid="clear-all-filters"/)
  assert.match(PAGE_SRC, /clearAllStatePatch\(\)/, '清除排序与筛选必须走既有 reset patch（filters + sort + direction）')
  assert.match(PAGE_SRC, /activeExplorerFilterChips\(parsed\.filters\)/, 'chips 必须由 URL 派生')
  assert.match(PAGE_SRC, /isExplorerColumnFiltered\(column, parsed\.filters\)/, 'funnel active 态必须由 URL 派生')
})

// ===== H. chips 行为 =====
test('H. activeExplorerFilterChips：区间 / 单边界 / 多列聚合', () => {
  assert.deepEqual(activeExplorerFilterChips(filtersWith({})), [])

  const range = activeExplorerFilterChips(filtersWith({ ma5_min: 0.6, ma5_max: 0.8 }))
  assert.equal(range.length, 1)
  assert.equal(range[0].column, 'ma5')
  assert.equal(range[0].label, 'MA5 60%–80%')
  assert.deepEqual([...range[0].keys], ['ma5_min', 'ma5_max'])

  const lowerOnly = activeExplorerFilterChips(filtersWith({ member_count_min: 30 }))
  assert.equal(lowerOnly.length, 1)
  assert.equal(lowerOnly[0].label, '成员数 ≥ 30')

  const upperOnly = activeExplorerFilterChips(filtersWith({ ma10_delta_max: -0.03 }))
  assert.equal(upperOnly.length, 1)
  assert.equal(upperOnly[0].label, 'MA10 5日Δ ≤ -3pp')

  const multi = activeExplorerFilterChips(filtersWith({ ma5_min: 0.6, member_count_min: 30 }))
  assert.equal(multi.length, 2)
  assert.deepEqual(
    multi.map((c) => c.column),
    ['member_count', 'ma5'],
    'chip 顺序应与 EXPLORER_FILTERABLE_COLUMNS 一致（成员数在 MA5 前）',
  )
})

// ===== H2. 两个 5日Δ 列的 chip 必须可分辨 =====
// MA5 与 MA10 的 5日Δ **列标题相同**（都是「5日Δ」），只有 filterLabel 能区分。
// 若 chip 用列表头 label，两条 chip 都会退化成「5日Δ ...」，用户无法判断属于哪一列。
test('H2. ma5_delta / ma10_delta 的 chip label 互不相同且带列前缀', () => {
  const chips = activeExplorerFilterChips(
    filtersWith({ ma5_delta_min: 0.03, ma10_delta_max: -0.03 }),
  )
  assert.equal(chips.length, 2, '应生成两条 delta chip')

  const byColumn = new Map(chips.map((c) => [c.column, c.label]))
  assert.equal(byColumn.get('ma5_delta'), 'MA5 5日Δ ≥ 3pp')
  assert.equal(byColumn.get('ma10_delta'), 'MA10 5日Δ ≤ -3pp')

  // 硬约束：两条 chip label 不得相同
  assert.notEqual(
    byColumn.get('ma5_delta'),
    byColumn.get('ma10_delta'),
    '两个 5日Δ chip 的 label 必须可分辨',
  )

  // 任何 chip 都不得退化为裸「5日Δ ...」（列表头名），必须带 MA5/MA10 前缀
  for (const chip of chips) {
    assert.ok(
      !/^5日Δ\s/.test(chip.label),
      `chip label 不得以裸「5日Δ」开头（无法分辨列），实际 ${chip.label}`,
    )
    assert.match(chip.label, /^MA(5|10) 5日Δ /, `delta chip 必须带列前缀，实际 ${chip.label}`)
  }

  // 非 delta 列不受影响：label == filterLabel
  const nonDelta = activeExplorerFilterChips(
    filtersWith({ ma5_min: 0.6, ma5_max: 0.8, member_count_min: 30 }),
  )
  assert.deepEqual(
    nonDelta.map((c) => c.label),
    ['成员数 ≥ 30', 'MA5 60%–80%'],
  )
})

// ===== I. 弹层结构 =====
test('I. 列筛选弹层：条件（区间/≥/≤/=）+ 最小/最大值 + 清除/应用', () => {
  assert.match(PAGE_SRC, /EXPLORER_FILTER_MODES\.map/)
  assert.match(PAGE_SRC, /data-testid=\{`filter-mode-\$\{m\.value\}`\}/)
  assert.match(PAGE_SRC, /data-testid="filter-min"/)
  assert.match(PAGE_SRC, /data-testid="filter-max"/)
  assert.match(PAGE_SRC, /data-testid="filter-apply"/)
  assert.match(PAGE_SRC, /data-testid="filter-clear"/)
  assert.match(
    PAGE_SRC,
    /applyColumnFilter\(filterPopover\.column, mode, lower, upper\)/,
    'Apply 必须回到 applyColumnFilter → applyPatch → URL',
  )
})

// ===== J. URL 仍是 SSOT =====
test('J. URL 仍是唯一正式状态（无新 global store / localStorage）', () => {
  assert.ok(!PAGE_CODE.includes('localStorage'), '不得把筛选状态放进 localStorage')
  assert.ok(!PAGE_CODE.includes('zustand'), '不得引入新的 state 库')
  assert.match(PAGE_SRC, /setSearchParams\(serializeExplorerState\(/, '所有筛选变更必须回写 URL')
})
