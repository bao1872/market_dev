// [REVIEW-UX-CN-01] Review 中文化展示合同测试（纯 TS，tsx --test 可跑）。
//
// 覆盖：
// - REVIEW_TERMS label / help 字典完整（关键术语 + 非空 + 无价值判断词）
// - PHASE_LABELS / READINESS_LABELS / SORT_LABELS / DETAIL_TAB_LABELS 精确中文
// - formatPhaseLabel / formatReadiness 中文展示 + fallback + null 语义
// - canonical 值不变（URL / API / enum 值原样保留）
// - 关键组件不再直接渲染英文用户标签（reviewCopy/ReviewTerm 统一映射）
// - null/unavailable 语义不变（绝不把 null 当 0）
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  REVIEW_TERMS,
  PHASE_LABELS,
  SORT_LABELS,
  DETAIL_TAB_LABELS,
  DETAIL_TAB_HELP,
  ATTRIBUTION_SUBTAB_LABELS,
  FACT_GROUP_ALIASES,
} from '../reviewCopy'
import { formatPhaseLabel, formatReadiness, NULL_DISPLAY, formatPercentNullable, formatNumberNullable } from '../reviewFormat'
import { normalizePhase, normalizeReadiness, normalizeSort, encodeReviewUrl, decodeReviewUrl, defaultReviewUrlState } from '../urlState'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

// 禁止的价值判断词（tooltip 只能中性解释）
const FORBIDDEN_TOOLTIP_WORDS = ['利好', '利空', '机会', '风险', '健康', '聪明钱', '看多', '看空', '强烈建议', '强推']

// ============================================================
// 1. 术语字典
// ============================================================

test('UX1. REVIEW_TERMS 关键术语齐全且 label/help 非空', () => {
  const required: Array<keyof typeof REVIEW_TERMS> = [
    'scope',
    'phase',
    'position',
    'velocity',
    'acceleration',
    'equalWeightReturn',
    'capitalTilt',
    'breadth',
    'leadershipMigration',
    'coverage',
    'freshness',
    'technical',
    'amountWeightedReturn',
    'totalVolume',
    'totalAmount',
    'priceConcentration',
    'amountConcentration',
    'rawHhi',
    'normalizedHhi',
    'sampleCount',
    'upperOccupancy',
    'lowerOccupancy',
    'returnDispersion',
    'jaccardStability',
    'migration',
  ]
  for (const k of required) {
    assert.ok(REVIEW_TERMS[k], `REVIEW_TERMS 必须含 ${k}`)
    assert.ok(REVIEW_TERMS[k].label.trim() !== '', `${k}.label 非空`)
    assert.ok(REVIEW_TERMS[k].help && REVIEW_TERMS[k].help!.trim() !== '', `${k}.help 非空`)
  }
})

test('UX2. tooltip 无价值判断词（中性事实措辞）', () => {
  for (const [k, t] of Object.entries(REVIEW_TERMS)) {
    if (!t.help) continue
    for (const w of FORBIDDEN_TOOLTIP_WORDS) {
      assert.ok(!t.help.includes(w), `REVIEW_TERMS.${k}.help 不得含 "${w}"`)
    }
  }
  for (const [k, h] of Object.entries(DETAIL_TAB_HELP)) {
    for (const w of FORBIDDEN_TOOLTIP_WORDS) {
      assert.ok(!h.includes(w), `DETAIL_TAB_HELP.${k} 不得含 "${w}"`)
    }
  }
})

// ============================================================
// 2. Phase / Readiness / Sort / Tab 精确中文
// ============================================================

test('UX3. formatPhaseLabel 精确中文映射（canonical 值不变）', () => {
  assert.equal(formatPhaseLabel('Early Lift'), '初步抬升')
  assert.equal(formatPhaseLabel('Strengthening'), '增强中')
  assert.equal(formatPhaseLabel('Sustained'), '持续中')
  assert.equal(formatPhaseLabel('Decelerating'), '动能放缓')
  assert.equal(formatPhaseLabel('Weakening'), '走弱中')
  assert.equal(formatPhaseLabel('Repairing'), '修复中')
  // canonical 值本身保持英文 raw（no translation of enum）
  assert.ok(PHASE_LABELS['Early Lift'] !== 'Early Lift', '展示 label 必须与 canonical 不同')
  assert.equal('Early Lift' in PHASE_LABELS, true)
  // 未知 fallback 如实返回原值
  assert.equal(formatPhaseLabel('Bogus' as string), 'Bogus')
  assert.equal(formatPhaseLabel(null), NULL_DISPLAY)
  assert.equal(formatPhaseLabel(undefined), NULL_DISPLAY)
})

test('UX4. formatReadiness 精确中文映射（canonical 值不变）', () => {
  assert.equal(formatReadiness('ready'), '可用')
  assert.equal(formatReadiness('insufficient_history'), '历史数据不足')
  assert.equal(formatReadiness('unavailable_current'), '当日数据不可用')
  assert.equal(formatReadiness('Bogus' as string), 'Bogus')
  assert.equal(formatReadiness(null), NULL_DISPLAY)
  assert.equal(formatReadiness(undefined), NULL_DISPLAY)
})

test('UX5. SORT_LABELS 覆盖全部 canonical 排序值且为中文', () => {
  const canonicalSorts = [
    'velocity_desc',
    'acceleration_desc',
    'position_desc',
    'equal_weight_return_desc',
    'capital_tilt_desc',
    'migration_desc',
    'coverage_desc',
    'freshness_density_desc',
    'freshness_today_desc',
    'technical_hhi_desc',
    'leader_median_gap_desc',
  ]
  // 每个 canonical 值都有中文 label（以 ↓ 结尾）
  for (const s of canonicalSorts) {
    assert.ok(SORT_LABELS[s], `SORT_LABELS 必须含 ${s}`)
    assert.ok(SORT_LABELS[s].includes('↓'), `${s} label 应为降序指示`)
    assert.ok(/[\u4e00-\u9fff]/.test(SORT_LABELS[s]), `${s} label 应为中文`)
  }
  // 不能有额外未知 key
  assert.deepEqual(Object.keys(SORT_LABELS).sort(), canonicalSorts.sort())
})

test('UX6. DETAIL_TAB_LABELS 精确映射六个 tab', () => {
  assert.equal(DETAIL_TAB_LABELS.current, '当日观察')
  assert.equal(DETAIL_TAB_LABELS.dynamics, '历史动态')
  assert.equal(DETAIL_TAB_LABELS.internal, '内部结构')
  assert.equal(DETAIL_TAB_LABELS.leadership, '龙头迁移')
  assert.equal(DETAIL_TAB_LABELS.attribution, '成员归因')
  assert.equal(DETAIL_TAB_LABELS.facts, '原始数据')
  assert.deepEqual(Object.keys(DETAIL_TAB_LABELS).sort(), ['attribution', 'current', 'dynamics', 'facts', 'internal', 'leadership'])
})

test('UX7. ATTRIBUTION_SUBTAB_LABELS 精确映射', () => {
  assert.equal(ATTRIBUTION_SUBTAB_LABELS.direction, '涨跌贡献')
  assert.equal(ATTRIBUTION_SUBTAB_LABELS.capital, '资金偏向贡献')
  assert.equal(ATTRIBUTION_SUBTAB_LABELS.breadth, '涨跌分布')
  assert.equal(ATTRIBUTION_SUBTAB_LABELS.concentration, '集中度贡献')
  assert.equal(ATTRIBUTION_SUBTAB_LABELS.leadership, '龙头贡献')
})

test('UX8. FACT_GROUP_ALIASES 覆盖 canonical 顶层分组（展示别名，key 不变）', () => {
  const canonicalGroups = ['scope', 'price', 'trend', 'structure', 'momentum', 'participation', 'chip', 'freshness']
  for (const g of canonicalGroups) {
    assert.ok(FACT_GROUP_ALIASES[g], `FACT_GROUP_ALIASES 必须含 ${g}`)
    assert.ok(FACT_GROUP_ALIASES[g].trim() !== '')
  }
})

// ============================================================
// 3. canonical 值不变（URL / API / enum）
// ============================================================

test('UX9. canonical phase/readiness/sort 值原样保留（URL SSOT）', () => {
  // normalize 接受并返回 canonical raw 值
  assert.equal(normalizePhase('Early Lift'), 'Early Lift')
  assert.equal(normalizeReadiness('ready'), 'ready')
  assert.equal(normalizeSort('velocity_desc'), 'velocity_desc')
  // URL 编解码往返保留 canonical raw 值（中文只在 UI label，不进 URL）
  const enc = encodeReviewUrl({
    ...defaultReviewUrlState(),
    phase: 'Early Lift' as never,
    readiness: 'ready' as never,
    sort: 'velocity_desc' as never,
  })
  const dec = decodeReviewUrl(enc)
  assert.equal(dec.phase, 'Early Lift')
  assert.equal(dec.readiness, 'ready')
  assert.equal(dec.sort, 'velocity_desc')
  // URL 中不得出现中文 phase label（canonical 必须保持英文）
  assert.ok(!enc.toString().includes('初步抬升'), 'URL 不得包含中文展示 label')
})

test('UX10. 前端不硬编码 canonical 8-group 中文 label map（R3B-FE-7 延续）', () => {
  const contractSrc = read('scopeObservationWorkspaceContract.ts')
  for (const p of [
    "price_capital: '价格与资金表现'",
    "trend_state: '趋势状态'",
    "momentum_squeeze_release: '动量与压缩释放'",
  ]) {
    assert.ok(!contractSrc.includes(p), `contract 不得硬编码 canonical label map (${p})`)
  }
})

// ============================================================
// 4. 关键组件不再直接渲染英文用户标签
// ============================================================

test('UX11. Toolbar 全面中文化（canonical 值不变；P0-1 已删除 Readiness 筛选）', () => {
  const src = read('ScopeExplorerToolbar.tsx')
  assert.ok(src.includes('搜索板块 / 概念名称'), '搜索 placeholder 中文')
  assert.ok(src.includes('阶段：全部'), 'Phase 过滤中文')
  assert.ok(src.includes('表格'), 'Table 视图中文')
  assert.ok(src.includes('轨迹图'), 'Trajectory 视图中文')
  // P0-1：Readiness 数据状态筛选控件已从工具栏删除（普通用户不再用它筛选）
  assert.doesNotMatch(src, /数据状态：全部/, '工具栏不得保留 Readiness 筛选')
  assert.doesNotMatch(src, /Readiness/, '工具栏不得出现 Readiness 英文标签')
  // 不得残留英文用户标签
  assert.doesNotMatch(src, /Phase: 全部|Readiness: 全部|搜索 scope \/ key|>Table<|>Trajectory</)
  // canonical 选项值不变（phase 触发值仍是英文 raw；readiness canonical 值保留在 types/urlState）
  assert.ok(src.includes("'Early Lift'"), 'phase canonical 值不变')
  const types = read('types.ts')
  assert.ok(types.includes("'ready'"), 'readiness canonical 值仍保留')
})

test('UX12. ExplorerTable 表头经 ReviewTerm 中文化，无英文硬编码', () => {
  const src = read('ScopeExplorerTable.tsx')
  assert.ok(src.includes('termKey="scope"'))
  assert.ok(src.includes('termKey="freshness"'))
  assert.ok(src.includes('termKey="technical"'))
  assert.doesNotMatch(src, />Scope</)
  assert.doesNotMatch(src, />EW Return</)
  assert.doesNotMatch(src, />Capital Tilt</)
  assert.doesNotMatch(src, />Leadership Migration</)
})

test('UX13. DetailTabs label 全部来自 DETAIL_TAB_LABELS（无英文硬编码）', () => {
  const src = read('ScopeDetailTabs.tsx')
  assert.ok(src.includes('DETAIL_TAB_LABELS.current'))
  assert.ok(src.includes('DETAIL_TAB_LABELS.facts'))
  assert.doesNotMatch(src, /label: 'Current'|label: 'Dynamics'|label: 'Facts'/)
})

test('UX14. DetailWorkspace readiness/algo 标签中文化', () => {
  const full = read('ScopeDetailWorkspace.tsx')
  assert.ok(full.includes('数据状态：') , 'readiness 标签中文')
  assert.ok(full.includes('算法版本：'), 'algo 标签中文')
  assert.ok(full.includes('formatReadiness('), 'readiness 走 formatReadiness')
  assert.doesNotMatch(full, />readiness: |algo: /)
})

test('UX15. 观察分区标题中文化、canonical groupKey 映射不变', () => {
  const src = read('scopeObservationWorkspaceContract.ts')
  assert.ok(reqAreaTitle(src, 'price'), '价格与资金')
  assert.ok(reqAreaTitle(src, 'trend'), '趋势')
  assert.ok(reqAreaTitle(src, 'structure'), '结构')
  assert.ok(reqAreaTitle(src, 'momentum'), '动量')
  assert.ok(reqAreaTitle(src, 'volume'), '成交量')
  assert.ok(reqAreaTitle(src, 'context'), '数据背景')
  // canonical group keys 不变
  assert.ok(src.includes("groupKeys: ['price_capital']"))
  assert.ok(src.includes("groupKeys: ['momentum_squeeze_release']"))
  assert.ok(src.includes("groupKeys: ['volume_anomaly']"))
})

function reqAreaTitle(src: string, key: string): boolean {
  const m = src.match(new RegExp(`areaKey: '${key}',[\\s\\S]*?areaTitle: '([^']+)'`))
  return !!m && /\p{Script=Han}/u.test(m[1])
}

// ============================================================
// 5. null/unavailable 语义不变
// ============================================================

test('UX16. null/unavailable 语义不变（不伪造 0）', () => {
  assert.equal(formatPercentNullable(null), NULL_DISPLAY)
  assert.equal(formatNumberNullable(undefined), NULL_DISPLAY)
  assert.equal(formatNumberNullable(0), '0.00', '0 仍是合法值')
  assert.equal(formatPercentNullable(0), '0.0%', '0 仍是合法值')
})

// ============================================================
// 6. ReviewTerm a11y — compact 必须保留 keyboard focus 路径
// ============================================================

const REVIEW_TERM_SRC = read('ReviewTerm.tsx')
const DETAIL_TABS_SRC = read('ScopeDetailTabs.tsx')

test('UX17. standalone ReviewTerm label 是 keyboard focus trigger（tabIndex + onFocus + onBlur + aria-describedby 同元素）', () => {
  // standalone card/label（focusable 默认 true）必须自身可聚焦并拥有 description
  assert.ok(REVIEW_TERM_SRC.includes('focusable = true'))
  assert.ok(REVIEW_TERM_SRC.includes("tabIndex: 0"))
  assert.ok(REVIEW_TERM_SRC.includes('onFocus: () => setOpen(true)'))
  assert.ok(REVIEW_TERM_SRC.includes('onBlur: () => setOpen(false)'))
  assert.ok(REVIEW_TERM_SRC.includes("'aria-describedby': tooltipId"))
})

test('UX18. ReviewTerm 支持 focusable={false}（嵌套交互 trigger 内不创建第二个 tab stop）', () => {
  // focusable=false 时 labelFocusProps 必须退化为空对象（不渲染 tabIndex/onFocus/aria-describedby）
  assert.ok(REVIEW_TERM_SRC.includes('focusable = true'))
  assert.ok(REVIEW_TERM_SRC.includes('const labelFocusProps = focusable'))
  assert.ok(REVIEW_TERM_SRC.includes('tabIndex: 0,'))
  // 关键负向：focusable=false 路径（: {}）不得含 tabIndex/onFocus
  const falseBranch = REVIEW_TERM_SRC.split('const labelFocusProps = focusable')[1] ?? ''
  assert.ok(falseBranch.includes(': {}'), 'focusable=false 时 labelFocusProps 为空对象')
})

test('UX19. Detail Tab button 是 sole focus owner（aria-describedby + 内部 ReviewTerm focusable=false）', () => {
  // 1. tab button 自己持有 aria-describedby（真实 focus owner 与 description owner 一致）
  assert.ok(DETAIL_TABS_SRC.includes('aria-describedby={tabTooltipId}'))
  // 2. 内部 ReviewTerm 关闭自身 focusability，避免嵌套第二个 tabIndex=0 stop
  assert.ok(DETAIL_TABS_SRC.includes('focusable={false}'))
  // 3. 共享 tooltipId，使 button 的 aria-describedby 指向 ReviewTerm 渲染的 tooltip
  assert.ok(DETAIL_TABS_SRC.includes('tooltipId={tabTooltipId}'))
  // 负向 gate：tab button 内部不得出现裸 tabIndex={0} 后代（即 ReviewTerm 自身不再注入）
  const buttonBlock = DETAIL_TABS_SRC.split('role="tab"')[1] ?? ''
  assert.doesNotMatch(
    buttonBlock,
    /<ReviewTerm[\s\S]*?tabIndex=\{0\}/,
    'tab 内 ReviewTerm 不得自带 tabIndex=0（否则破坏 tablist 键盘模型）',
  )
})
