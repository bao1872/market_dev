// [结构状态因子层] - 描述: StockStructuralStatePanel 契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/structural-state-panel.test.ts
// 覆盖：
// 1. 面板文件存在且为 React 组件（export default 或 export function）
// 2. 面板使用 useStructuralFactors hook（禁止重新计算因子）
// 3. 面板包含双周期 tabs（1d / 15m）
// 4. 面板包含 5 张卡片（DSA 段/Swing 结构/成本节点/动量波动/成交参与）
// 5. 面板处理 null 值（渲染 '-' 占位）
// 6. 面板处理 degraded_reasons（渲染降级提示）
// 7. 面板处理 API 失败（渲染 '暂无数据'）
// 8. 前端不重新计算因子（禁止出现 ATR/BB/Node/POC/Swing 计算标识符）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')
const PANEL_PATH = join(FRONTEND_ROOT, 'src', 'components', 'StockStructuralStatePanel.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 面板文件存在且为 React 组件 =====
test('StockStructuralStatePanel is a React component', () => {
  const src = readSource(PANEL_PATH)

  // 必须使用 React hook 或 JSX（说明是 React 组件）
  assert.ok(
    /export\s+(default\s+)?(function|const)\s+StockStructuralStatePanel/.test(src),
    'StockStructuralStatePanel.tsx 必须导出名为 StockStructuralStatePanel 的 React 组件',
  )
})

// ===== 2. 面板使用 useStructuralFactors hook =====
test('Panel uses useStructuralFactors hook (no recalculation)', () => {
  const src = readSource(PANEL_PATH)

  assert.ok(
    /useStructuralFactors\s*\(/.test(src),
    'StockStructuralStatePanel 必须调用 useStructuralFactors hook 获取后端数据（禁止重新计算因子）',
  )
})

// ===== 3. 面板包含双周期 tabs（1d / 15m）=====
test('Panel has dual-period tabs (1d / 15m)', () => {
  const src = readSource(PANEL_PATH)

  // 必须含 primary / secondary 周期引用
  assert.ok(
    /primary/.test(src) && /secondary/.test(src),
    'Panel 必须引用 primary / secondary 双周期数据',
  )

  // 必须含周期切换 tab（button + 1d 或 15m 标签）
  // 接受两种实现：硬编码 1d/15m 文本，或从 primary_timeframe/secondary_timeframe 读取
  const hasTabSwitch =
    /button[^>]*>.*1d/.test(src) ||
    /'1d'/.test(src) ||
    /"1d"/.test(src) ||
    /primary_timeframe/.test(src)
  assert.ok(hasTabSwitch, 'Panel 必须含周期切换 tab（1d / 15m）')
})

// ===== 4. 面板包含 5 张卡片 =====
test('Panel renders 5 factor cards', () => {
  const src = readSource(PANEL_PATH)

  // 5 张卡片标题关键词（任选中文或英文）
  const requiredCards = [
    /dsa/i,           // DSA 段质量
    /swing/i,         // Swing 结构位置
    /cost|node|poc/i, // 成本/节点
    /volatil|momentum|bb|sqzmom/i, // 动量/波动
    /participat|volume/i,          // 成交参与
  ]

  for (const pattern of requiredCards) {
    assert.ok(
      pattern.test(src),
      `Panel 必须包含因子卡片（匹配模式 ${pattern}）。5 张卡片：DSA段/Swing结构/成本节点/动量波动/成交参与`,
    )
  }
})

// ===== 5. 面板处理 null 值（渲染 '-'）=====
test('Panel renders placeholder for null values', () => {
  const src = readSource(PANEL_PATH)

  // 必须有 null/undefined 处理逻辑，渲染 '-' 占位
  // 接受：value ?? '-' 或 value || '-' 或 if (!value) return '-'
  const hasNullHandler =
    /\?\?\s*['"]-['"]/.test(src) ||
    /\|\|\s*['"]-['"]/.test(src) ||
    /if\s*\(\s*!\w+\s*\)\s*return\s*['"]-['"]/.test(src)
  assert.ok(hasNullHandler, 'Panel 必须对 null 值渲染 "-" 占位')
})

// ===== 6. 面板处理 degraded_reasons =====
test('Panel handles degraded_reasons', () => {
  const src = readSource(PANEL_PATH)

  // 必须引用 degraded_reasons 字段
  assert.ok(
    /degraded_reasons/.test(src),
    'Panel 必须引用 meta.degraded_reasons 字段（渲染降级提示）',
  )
})

// ===== 7. 面板处理 API 失败（渲染 '暂无数据'）=====
test('Panel handles API failure (renders placeholder)', () => {
  const src = readSource(PANEL_PATH)

  // 必须处理 isError / isLoading 状态
  const hasErrorHandling =
    /isError/.test(src) ||
    /isLoading/.test(src) ||
    /isPending/.test(src) ||
    /status\s*===?\s*['"]error['"]/.test(src) ||
    /暂无数据/.test(src) ||
    /加载中/.test(src)
  assert.ok(
    hasErrorHandling,
    'Panel 必须处理 API 失败/加载状态（isError/isLoading 或 "暂无数据"/"加载中" 占位）',
  )
})

// ===== 8. 前端不重新计算因子 =====
test('Frontend does not recompute factors', () => {
  const src = readSource(PANEL_PATH)

  // 禁止出现后端算法标识符（前端不得重新实现因子计算）
  const forbiddenIdentifiers = [
    'compute_atr',
    'compute_true_range',
    'bollinger',
    'compute_unified_volume_profile',
    '_tv_pivots_confirmed',
    'compute_sqzmom_lb',
    'compute_dsa_bundle',
    'percentile_rank',
    'compute_structural_factors',
    'pine_rma',
    'atr_pine',
  ]
  for (const id of forbiddenIdentifiers) {
    const re = new RegExp(`\\b${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`)
    assert.ok(
      !re.test(src),
      `StockStructuralStatePanel.tsx 禁止出现算法标识符 '${id}'（前端不得重新计算因子）`,
    )
  }
})

// ===== 9. V1.8 核心字段存在性检查 =====
// [V1.10] - Swing 摘要卡改用 developing 字段。
// active major leg / confirmed pivot 字段在"结构因子明细"折叠卡片 JSON 中查看。
// 测试 12/14 已单独验证 V1.10 Developing 字段在 CARDS 中。
test('Panel includes V1.8 core fields in CARDS', () => {
  const src = readSource(PANEL_PATH)

  // V1.8 新增字段必须出现在 CARDS 配置中（key 字符串）
  // V1.10: Swing 摘要卡改用 developing 字段，active/confirmed 在明细 JSON
  const v18Keys = [
    // DSA segment V1.8
    'dsa_value',
    'price_vs_dsa_atr',
    'current_dsa_segment_age_bars',
    'current_dsa_segment_return_pct',
    'current_dsa_segment_slope_atr_per_bar',
    'current_dsa_segment_efficiency_0_1',
    'current_segment_volume_sum',
    'prev_dsa_segment_dir',
    'segment_return_abs_ratio',
    'current_vs_prev_volume_ratio',
    'return_per_volume_ratio',
    // Cost position V1.8
    'price_vs_poc_atr',
    'value_area_position_0_1',
    'nearest_node_above_price',
    'nearest_node_below_price',
    'distance_to_node_above_atr',
    'distance_to_node_below_atr',
    'node_above_strength',
    'node_below_strength',
    // V1.8 位置语义修复字段（区分 VP 全区间 / 节点区间 / VA 区间）
    'node_interval_position_0_1',
    'node_interval_position_raw',
    'cost_position_zone',
    'value_area_zone',
    'val_price',
    'vah_price',
    // Volatility V1.8
    'distance_to_bb_upper_atr',
    'distance_to_bb_lower_atr',
    'sqzmom_abs_percentile',
    'sqz_on',
    'sqz_off',
    // Participation V1.8 (shared from dsa_segment)
    'prev_segment_volume_sum',
    'current_segment_return_per_volume',
    'prev_segment_return_per_volume',
  ]
  for (const key of v18Keys) {
    assert.ok(
      src.includes(`'${key}'`),
      `Panel CARDS 必须包含 V1.8 字段 '${key}'`,
    )
  }
})

// ===== 10. V1.8 Relation 区块字段检查 =====
test('Panel relation section uses V1.8 objective fields', () => {
  const src = readSource(PANEL_PATH)

  // V1.8 relation 必须包含客观关系字段
  const v18RelationKeys = [
    'primary_dir',
    'secondary_dir',
    'primary_swing_position',
    'secondary_swing_position',
    'primary_slope_atr',
    'secondary_slope_atr',
    'secondary_vs_primary_position_delta',
  ]
  for (const key of v18RelationKeys) {
    assert.ok(
      src.includes(`data.relation.${key}`),
      `Panel relation 区块必须引用 V1.8 字段 'data.relation.${key}'`,
    )
  }

  // V1.8 移除 momentum_alignment
  assert.ok(
    !/momentum_alignment/.test(src),
    'Panel V1.8 必须移除 momentum_alignment 引用',
  )
})

// ===== 11. V1.8 成本/节点卡片位置语义标签修复 =====
test('Cost/Node card uses unambiguous position labels (V1.8 semantic fix)', () => {
  const src = readSource(PANEL_PATH)

  // 提取成本/节点卡片配置范围（title: '成本/节点' 到下一个 title: 之间）
  const costCardStart = src.indexOf("title: '成本/节点'")
  assert.ok(costCardStart > 0, '必须存在「成本/节点」卡片')
  // 找到下一个 title: 'xxx' 作为成本卡结束
  const nextTitleMatch = src.slice(costCardStart + 20).match(/\n\s*title:\s*'/)
  const costCardEnd = nextTitleMatch
    ? costCardStart + 20 + (nextTitleMatch.index ?? 0)
    : src.length
  const costCardSrc = src.slice(costCardStart, costCardEnd)

  // 成本卡内禁止含糊的「位置 [0,1]」标签（与节点区间位置混淆）
  assert.ok(
    !/label:\s*'位置\s*\[0,1\]'/.test(costCardSrc),
    '成本/节点卡片禁止使用含糊的「位置 [0,1]」标签（与节点区间位置混淆）',
  )

  // 必须出现明确的 VP 全区间位置标签
  assert.ok(
    costCardSrc.includes("'VP全区间位置[0,1]'"),
    "成本/节点卡片必须含「VP全区间位置[0,1]」标签（原 position_0_1 改名，保持 VP 全区间语义）",
  )

  // 必须出现节点区间位置标签
  assert.ok(
    costCardSrc.includes("'节点区间位置[0,1]'"),
    "成本/节点卡片必须含「节点区间位置[0,1]」标签（新增 node_interval_position_0_1）",
  )

  // 必须出现 VA 状态标签
  assert.ok(
    costCardSrc.includes("'VA状态'"),
    "成本/节点卡片必须含「VA状态」标签（新增 value_area_zone 分类）",
  )

  // VA 位置标签改为 raw（避免误读为已 clip）
  assert.ok(
    costCardSrc.includes("'VA位置raw'"),
    "成本/节点卡片必须含「VA位置raw」标签（value_area_position_0_1 不 clip）",
  )

  // 必须显示 VAL / VAH 原值
  assert.ok(
    costCardSrc.includes("'VAL'") && costCardSrc.includes("'VAH'"),
    "成本/节点卡片必须显示 VAL / VAH 原值",
  )
})

// ===== 12. Swing 摘要卡使用 developing 标签（V1.10 developing swing 语义）=====
test('test_swing_summary_uses_developing_labels', () => {
  const src = readSource(PANEL_PATH)

  // 摘要卡必须使用 developing 标签（当前正在发生的回落/反弹结构）
  assert.ok(
    src.includes('developing_swing_high') || src.includes('Developing high'),
    'Swing 摘要卡必须使用 developing_swing_high 或 Developing high 标签（V1.10 developing swing 语义）',
  )
  assert.ok(
    src.includes('developing_swing_low') || src.includes('Developing low'),
    'Swing 摘要卡必须使用 developing_swing_low 或 Developing low 标签（V1.10 developing swing 语义）',
  )
  assert.ok(
    src.includes('price_position_in_developing_swing_0_1') || src.includes('Developing 位置'),
    'Swing 摘要卡必须使用 price_position_in_developing_swing_0_1 或 Developing 位置 标签（V1.10 developing swing 位置）',
  )

  // 提取 Swing 摘要卡（CARDS 配置中的 swing_position 部分）
  const swingStart = src.indexOf("title: 'Swing 结构位置'")
  assert.ok(swingStart > 0, '必须存在 Swing 结构位置 卡片')
  const nextTitleMatch = src.slice(swingStart + 20).match(/\n\s*title:\s*'/)
  const swingEnd = nextTitleMatch
    ? swingStart + 20 + (nextTitleMatch.index ?? 0)
    : src.length
  const swingCardSrc = src.slice(swingStart, swingEnd)

  // 摘要卡不得包含 Active 字段作为主字段（V1.10 改用 developing，active 在明细 JSON）
  assert.ok(
    !/'Active high'/.test(swingCardSrc) && !/'Active low'/.test(swingCardSrc),
    'Swing 摘要卡不得出现 Active high/Active low 作为主字段（V1.10 改用 developing）',
  )
  assert.ok(
    !/'Active 位置/.test(swingCardSrc),
    'Swing 摘要卡不得出现 Active 位置 作为主字段（V1.10 改用 developing）',
  )

  // 禁止模糊标签（与 confirmed / active 混淆）
  assert.ok(
    !src.includes('最近 swing high'),
    'Swing 摘要卡不得使用模糊标签「最近 swing high」（应使用 Developing high）',
  )
  assert.ok(
    !src.includes('最近 swing low'),
    'Swing 摘要卡不得使用模糊标签「最近 swing low」（应使用 Developing low）',
  )
})

// ===== 13. active major leg / confirmed pivot 只在明细（摘要卡不作为显示标签）=====
test('test_active_confirmed_only_in_detail', () => {
  const src = readSource(PANEL_PATH)

  // 摘要卡片（CARDS 配置）不得包含 confirmed_swing_high 或 active_swing_high 作为显示标签/键
  // active major leg / confirmed pivot 字段只在「结构因子明细」折叠卡片的 JSON 中可见
  assert.ok(
    !src.includes("'confirmed_swing_high'") && !src.includes('"confirmed_swing_high"'),
    'Swing 摘要卡不得包含 confirmed_swing_high 作为显示标签（confirmed pivot 只在 JSON 明细中）',
  )
  assert.ok(
    !src.includes("'active_swing_high'") && !src.includes('"active_swing_high"'),
    'Swing 摘要卡不得包含 active_swing_high 作为显示标签（active major leg 只在 JSON 明细中）',
  )
  assert.ok(
    !src.includes("'active_swing_low'") && !src.includes('"active_swing_low"'),
    'Swing 摘要卡不得包含 active_swing_low 作为显示标签（active major leg 只在 JSON 明细中）',
  )

  // 源码必须包含 confirmed / active 字样（在明细注释/标签中可见）
  assert.ok(
    /confirmed/i.test(src),
    '源码必须包含 confirmed 字样（confirmed pivot 在明细部分可见，不在摘要卡）',
  )
})

// ===== 14. 时序卡使用 developing 标签（V1.10 developing swing 时序位置）=====
test('test_temporal_rows_use_developing_labels', () => {
  const src = readSource(PANEL_PATH)

  // 时序卡必须使用 developing swing 标签
  assert.ok(
    src.includes('daily_price_position_in_developing_swing_0_1') || src.includes('daily_developing_swing'),
    '时序卡必须使用 daily_price_position_in_developing_swing_0_1 或 daily_developing_swing 标签（日线上下文 developing swing）',
  )
  assert.ok(
    src.includes('m15_price_position_in_developing_swing_0_1') || src.includes('m15_developing_swing'),
    '时序卡必须使用 m15_price_position_in_developing_swing_0_1 或 m15_developing_swing 标签（15 分钟响应 developing swing）',
  )

  // 提取时序行配置区间（TEMPORAL_DAILY_ROWS 到 TEMPORAL_DERIVED_ROWS）
  const temporalStart = src.indexOf('TEMPORAL_DAILY_ROWS')
  const temporalEnd = src.indexOf('TEMPORAL_DERIVED_ROWS')
  assert.ok(
    temporalStart > 0 && temporalEnd > temporalStart,
    '必须存在 TEMPORAL_DAILY_ROWS 到 TEMPORAL_DERIVED_ROWS 时序行配置区间',
  )
  const temporalSrc = src.slice(temporalStart, temporalEnd)

  // 时序标签必须包含 developing / confirmed（使用明确的 developing swing / confirmed anchor 标签）
  assert.ok(
    /developing|confirmed/i.test(temporalSrc),
    '时序行标签必须包含 developing 或 confirmed（使用明确的 developing swing / confirmed anchor 标签）',
  )
  // 时序行不得使用模糊的「Swing 位置」（应使用 Developing 位置）
  assert.ok(
    !/Swing 位置/.test(temporalSrc),
    '时序行标签不得使用模糊的「Swing 位置」（应使用 Developing 位置[0,1]）',
  )
  // 时序行不得使用 Active 标签作为主字段（V1.10 改用 developing）
  assert.ok(
    !/'Active high'/.test(temporalSrc) && !/'Active low'/.test(temporalSrc),
    '时序行标签不得使用 Active high/Active low 作为主字段（V1.10 改用 developing）',
  )
})
