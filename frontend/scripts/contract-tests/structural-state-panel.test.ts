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
test('Panel includes V1.8 core fields in CARDS', () => {
  const src = readSource(PANEL_PATH)

  // V1.8 新增字段必须出现在 CARDS 配置中（key 字符串）
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
    // Swing V1.8
    'swing_range',
    'price_position_in_swing_0_1',
    'distance_to_swing_high_atr',
    'distance_to_swing_low_atr',
    'retracement_from_high_0_1',
    'rebound_from_low_0_1',
    // Cost position V1.8
    'price_vs_poc_atr',
    'value_area_position_0_1',
    'nearest_node_above_price',
    'nearest_node_below_price',
    'distance_to_node_above_atr',
    'distance_to_node_below_atr',
    'node_above_strength',
    'node_below_strength',
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
