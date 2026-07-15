// [ChartLabels] - 描述: StrategyChart 用户文案契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/chartLabels.test.ts
//
// 覆盖：
// 1. POC 峰标签显示"核心共识价"（非"POC 峰"）
// 2. 普通峰标签显示"共识价"（非"峰"）
// 3. POC 中心线标签显示"核心共识价"（非"POC"）
// 4. tooltip 中 POC → "核心共识价"，PEAK → "共识价"
// 5. 缺失提示为"筹码共识价暂不可用"（非"筹码分布暂不可用"）
// 6. 内部字段名 n.poc / profile.pocPrice / row.is_poc 不变（不改 DTO/算法）
//
// [文案契约] - 描述: 仅改用户可见文案，不改内部 id/DTO/算法
// "筹码共识价"是基于历史成交量分布的估算代理，不是股东真实持仓成本

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const CHART_PATH = join(__dirname, '..', 'StrategyChart.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. POC 峰标签显示"核心共识价" =====
test('节点价格标签：POC 峰 → "核心共识价"，普通峰 → "共识价"', () => {
  const src = readSource(CHART_PATH)
  // 验证新文案存在
  assert.ok(
    src.includes('核心共识价'),
    'StrategyChart 必须显示"核心共识价"（POC 峰标签）',
  )
  assert.ok(
    src.includes('共识价'),
    'StrategyChart 必须显示"共识价"（普通峰标签）',
  )
  // 验证旧文案已移除
  assert.ok(
    !src.includes('POC 峰'),
    '不应保留旧文案"POC 峰"（已改为"核心共识价"）',
  )
  // 注意：单独的"峰"字可能出现在其他上下文，不强制检查
})

// ===== 2. POC 中心线标签 =====
test('POC 中心线标签显示"核心共识价"（非裸"POC"）', () => {
  const src = readSource(CHART_PATH)
  // 验证 POC 中心线 drawText 使用"核心共识价"
  // 查找 profile.pocPrice 附近的 drawText 调用（包括参数）
  const pocLineSection = src.match(/profile\.pocPrice[\s\S]{0,400}?drawText\([^)]*\)/)
  assert.ok(pocLineSection, '必须存在 POC 中心线 drawText 调用')
  assert.ok(
    pocLineSection![0].includes('核心共识价'),
    `POC 中心线标签必须显示"核心共识价"，实际: ${pocLineSection![0]}`,
  )
})

// ===== 3. tooltip 文案 =====
test('tooltip 中 POC → "核心共识价"，PEAK → "共识价"', () => {
  const src = readSource(CHART_PATH)
  // profileTooltip 函数中 is_poc 标签
  assert.ok(
    src.includes("is_poc ? ' · 核心共识价'"),
    'tooltip 中 is_poc 应显示"核心共识价"',
  )
  assert.ok(
    src.includes("is_peak ? ' · 共识价'"),
    'tooltip 中 is_peak 应显示"共识价"',
  )
  // 不应保留旧英文标签
  assert.ok(
    !src.includes("' · POC'"),
    '不应保留旧 tooltip 文案"POC"',
  )
  assert.ok(
    !src.includes("' · PEAK'"),
    '不应保留旧 tooltip 文案"PEAK"',
  )
})

// ===== 4. 缺失提示 =====
test('VP 数据缺失提示为"筹码共识价暂不可用"', () => {
  const src = readSource(CHART_PATH)
  assert.ok(
    src.includes('筹码共识价暂不可用'),
    '缺失提示必须为"筹码共识价暂不可用"',
  )
  assert.ok(
    !src.includes('筹码分布暂不可用'),
    '不应保留旧缺失提示"筹码分布暂不可用"',
  )
})

// ===== 5. 内部字段名不变 =====
test('内部字段名保留：n.poc / profile.pocPrice / row.is_poc 不变', () => {
  const src = readSource(CHART_PATH)
  // 内部 poc 字段名必须保留（不改 DTO/算法）
  assert.ok(src.includes('n.poc'), '内部字段 n.poc 必须保留')
  assert.ok(src.includes('profile.pocPrice'), '内部字段 profile.pocPrice 必须保留')
  assert.ok(src.includes('row.is_poc'), '内部字段 row.is_poc 必须保留')
  assert.ok(src.includes('is_poc'), '内部字段 is_poc 必须保留')
  // 内部 layer key 'poc' 不变
  assert.ok(src.includes("'poc'") || src.includes('poc:'), "内部 layer key 'poc' 必须保留")
})
