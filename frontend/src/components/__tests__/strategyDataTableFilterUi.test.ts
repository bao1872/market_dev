// [PANJI-REVIEW-UI-UNIFY] StrategyDataTable 表头 / 筛选 UI 契约测试（纯逻辑 + 源码契约，无 DOM）。
// 用法：tsx --test src/components/__tests__/strategyDataTableFilterUi.test.ts
//
// 覆盖：
//   A compact-table 表头不再允许中文逐字换行（white-space: nowrap + word-break: keep-all）
//   B 表头统一高度（表头 42px / 数据行 40px）
//   C 筛选按钮是明确控件（SVG funnel，非字符 ⌁）+ 常显三态（idle/hover/active）
//   D 排序与筛选是两个独立点击区域
//   E 筛选按钮暴露 aria-pressed 状态 + 稳定 testid
//   F meta bar 用 active filter chips 展示**具体条件**（不再是「N 个列筛选」）
//   G formatFilterChipLabel 行为（enum 中文 label / 区间 / 无值操作符 / shortTitle / 多选）
//   H 单条 chip 清除只清对应列

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { formatFilterChipLabel } from '../StrategyDataTable.tsx'

function readSrc(...segments: string[]): string {
  return readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', ...segments),
    'utf-8',
  )
}

const TABLE_SRC = readSrc('components', 'StrategyDataTable.tsx')
const GLOBAL_SCSS = readSrc('styles', 'global.scss')
// 去掉注释后的源码，供「不得再出现」类断言使用：
// 文档里为说明历史而提到旧实现（如「不再是 N 个列筛选」）不应被判定为违规。
const TABLE_CODE = codeOnly(TABLE_SRC)

/** 去掉行注释与块注释（JSX 注释块与 SCSS 行注释同样处理）。 */
function codeOnly(src: string): string {
  return src
    .replace(/\{?\/\*[\s\S]*?\*\/\}?/g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '')
}

/** 提取单层（无嵌套花括号）SCSS 规则体。 */
function scssRule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = GLOBAL_SCSS.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))
  assert.ok(match, `global.scss 应存在 ${selector} 规则块`)
  return match![1]
}

// ===== A. 表头不再逐字换行 =====
test('A. compact-table 表头不再允许中文逐字换行', () => {
  const th = scssRule('.compact-table thead th')
  assert.match(th, /white-space\s*:\s*nowrap/, '表头必须 nowrap')
  assert.match(th, /word-break\s*:\s*keep-all/, '表头必须 word-break: keep-all')
  assert.ok(
    !/white-space\s*:\s*normal/.test(th),
    '表头不得再使用 white-space: normal（历史上导致中文逐字竖排）',
  )

  const label = scssRule('.compact-table .th-label')
  assert.match(label, /white-space\s*:\s*nowrap/)
  assert.match(label, /word-break\s*:\s*keep-all/)
  assert.match(label, /text-overflow\s*:\s*ellipsis/, '长表头必须 ellipsis 截断')
  assert.ok(!/white-space\s*:\s*normal/.test(label), '.th-label 不得再换行')
  assert.ok(!/text-overflow\s*:\s*clip/.test(label), '.th-label 不得用 clip')

  const shell = scssRule('.compact-table .th-shell')
  assert.match(shell, /align-items\s*:\s*center/)
  assert.ok(!/align-items\s*:\s*flex-start/.test(shell), '表头不再顶部对齐（说明已不允许多行）')
})

// ===== B. 表头统一高度 =====
test('B. 表头 42px / 数据行 40px 统一高度', () => {
  assert.match(scssRule('.interactive-table thead th'), /height\s*:\s*42px/)
  assert.match(scssRule('.th-shell'), /height\s*:\s*42px/)
  assert.match(scssRule('.compact-table .th-sort'), /height\s*:\s*42px/)
  assert.match(scssRule('.compact-table td'), /height\s*:\s*40px/)
})

// ===== C. 筛选按钮：明确 SVG 控件 + 常显三态 =====
test('C. 筛选按钮是 SVG funnel 控件（字符 ⌁ 已删除）且常显', () => {
  assert.ok(!TABLE_CODE.includes('⌁'), '必须删除字符图标 ⌁')
  assert.match(TABLE_SRC, /<TableFilterIcon\s*\/>/, '应使用共享 SVG funnel 图标')

  const filterRule = GLOBAL_SCSS.match(/\n\.th-filter\s*\{([\s\S]*?)\n\}/)
  assert.ok(filterRule, 'global.scss 应存在 .th-filter 规则块')
  const body = filterRule![1]
  assert.match(body, /border\s*:\s*1px solid/, 'idle 必须有浅边框（常显，不依赖 hover 才出现）')
  assert.match(body, /background\s*:\s*#/, 'idle 必须有深背景')
  assert.match(body, /&:hover/, '必须有 hover 增强态')
  assert.match(body, /&\.active/, '必须有 active 态')
  assert.match(body, /&\.active::after/, 'active 必须有右上状态点')
  assert.match(body, /v\.\$color-brand/, 'active 必须使用品牌色（B17：品牌色=筛选激活）')
})

// ===== D. 排序 / 筛选两个独立点击区域 =====
test('D. 排序与筛选是两个独立点击区域', () => {
  assert.match(TABLE_SRC, /className="th-sort"[\s\S]*?onClick=\{\(\) => toggleSort\(i\)\}/, '排序按钮绑定 toggleSort')
  assert.match(
    TABLE_SRC,
    /'th-filter'[\s\S]*?onClick=\{[^}]*stopPropagation/,
    '筛选按钮独立绑定 onClick（且 stopPropagation，不触发排序）',
  )
})

// ===== E. aria 状态 =====
test('E. 筛选按钮暴露 aria-pressed 与稳定 testid', () => {
  assert.match(TABLE_SRC, /aria-pressed=\{!!filters\[i\]\}/, '筛选按钮必须暴露 aria-pressed')
  assert.match(TABLE_SRC, /data-testid=\{`th-filter-\$\{col\.key\}`\}/, '筛选按钮需要稳定 testid')
})

// ===== F. active filter chips =====
test('F. meta bar 用 chips 展示具体条件', () => {
  assert.ok(!TABLE_CODE.includes('个列筛选'), '模糊的「N 个列筛选」必须被 chips 取代')
  assert.match(TABLE_SRC, /data-testid="table-filter-chips"/)
  assert.match(TABLE_SRC, /className="filter-chip"/)
  assert.match(TABLE_SRC, /aria-label=\{`清除筛选 \$\{label\}`\}/, 'chip 需要可访问的清除名')
  assert.match(TABLE_SRC, /title=\{`清除「\$\{label\}」`\}/)
  assert.match(TABLE_SRC, /formatFilterChipLabel\(column, filter\)/, 'chip 文案必须走统一 formatter')
})

// ===== G. formatFilterChipLabel 行为 =====
test('G. formatFilterChipLabel：enum 中文 label / 区间 / 无值操作符 / shortTitle / 多选', () => {
  const enumCol = {
    title: '趋势',
    enumOptions: [
      { label: '上升', value: 'up' },
      { label: '下降', value: 'down' },
    ],
  }
  const eq = formatFilterChipLabel<unknown>(enumCol, { key: 'trend', operator: 'eq', value: 'up' })
  assert.ok(eq.includes('趋势') && eq.includes('上升'), `enum 必须映射为中文 label，实际 ${eq}`)
  assert.ok(!/\bup\b/.test(eq), `不得泄露 canonical code，实际 ${eq}`)

  const numericCol = { title: '量比' }
  const between = formatFilterChipLabel<unknown>(numericCol, {
    key: 'volume_ratio',
    operator: 'between',
    value: '1',
    value2: '2',
  })
  // 视觉基线：chip 文案为「量比 1–2」（en dash 连接上下界，不加「区间」二字）
  assert.equal(between, '量比 1\u20132', `区间条件必须显示上下界，实际 ${between}`)
  assert.ok(!between.includes('区间'), `chip 文案不再出现「区间」二字，实际 ${between}`)

  const isEmpty = formatFilterChipLabel<unknown>(numericCol, { key: 'volume_ratio', operator: 'empty' })
  assert.ok(isEmpty.includes('为空'), `无值操作符只显示操作符，实际 ${isEmpty}`)
  assert.ok(!isEmpty.includes('undefined'), `不得渲染 undefined，实际 ${isEmpty}`)

  const short = formatFilterChipLabel<unknown>(
    { title: '第一金字塔结构事件类型', shortTitle: '结构事件' },
    { key: 'k', operator: 'eq', value: 'BOS' },
  )
  assert.ok(short.startsWith('结构事件'), `应优先使用 shortTitle，实际 ${short}`)

  const multi = formatFilterChipLabel<unknown>(enumCol, {
    key: 'trend',
    operator: 'in',
    value: 'up,down',
  })
  assert.ok(multi.includes('上升') && multi.includes('下降'), `多选应逐一映射，实际 ${multi}`)
  assert.ok(!multi.includes('、undefined'), `多选映射不得产生 undefined，实际 ${multi}`)
})

// ===== H. 单条清除 =====
test('H. chip 的 × 只清对应列筛选', () => {
  assert.match(TABLE_SRC, /onClick=\{\(\) => clearFilter\(index\)\}/, 'chip 点击应清除该列')
  assert.match(
    TABLE_SRC,
    /const clearFilter = useCallback\(\(index: number\) => \{[\s\S]*?delete next\[index\]/,
    'clearFilter 只删除该列（保留其余列）',
  )
})
