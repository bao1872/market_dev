// K 线右侧留白契约测试（CHANGE-20260713-008）
// 验证 StrategyChart 右侧 18%-22% 留白实现：
// 1. RIGHT_PADDING_RATIO 常量存在且在 0.18-0.22 范围内
// 2. step 使用 effectivePlotW（plotW * (1 - RIGHT_PADDING_RATIO)）而非 plotW
// 3. 所有交互坐标映射使用 step（自动同步到压缩后的 bar 分布）
// 4. 网格线和十字线水平线仍延伸到 g.plotRight（保持全宽）
// 5. 不修改 Node/Profile/POC 算法
// 6. 时间轴标签跟随 bar 分布（使用 effectivePlotW）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const CHART_PATH = join(__dirname, '..', 'StrategyChart.tsx')

function readSource(): string {
  return readFileSync(CHART_PATH, 'utf-8')
}

// ===== 1. RIGHT_PADDING_RATIO 常量存在且在范围内 =====
test('RIGHT_PADDING_RATIO 常量存在且在 0.18-0.22 范围内', () => {
  const src = readSource()
  const match = src.match(/const RIGHT_PADDING_RATIO\s*=\s*([\d.]+)/)
  assert.ok(match, '必须定义 RIGHT_PADDING_RATIO 常量')
  const ratio = parseFloat(match[1])
  assert.ok(ratio >= 0.18 && ratio <= 0.22, `RIGHT_PADDING_RATIO 必须在 0.18-0.22 范围内，当前为 ${ratio}`)
})

// ===== 2. step 使用 effectivePlotW =====
test('step 使用 effectivePlotW（压缩 bar 分布宽度）', () => {
  const src = readSource()
  assert.ok(
    src.includes('const effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)'),
    '必须计算 effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)',
  )
  assert.ok(
    src.includes('const step = effectivePlotW / display.length'),
    'step 必须使用 effectivePlotW 而非 plotW',
  )
})

// ===== 3. 所有交互坐标映射使用 step =====
test('十字线/滚轮锚点/Pointer 拖拽/命中坐标统一使用 step', () => {
  const src = readSource()
  assert.ok(
    src.includes('Math.floor((mx - g.l) / step)'),
    '十字线 mouse X → bar index 必须使用 step',
  )
  assert.ok(
    src.includes('Math.round(deltaPx / step)'),
    'Pointer 拖拽 deltaBars 必须使用 step',
  )
  assert.ok(
    src.includes('g.l + (i + 0.5) * step'),
    'bar 位置坐标必须使用 step',
  )
})

// ===== 4. 网格线和十字线水平线保持全宽 =====
// [PROMPT.md §5.3.4 V2] drawLine 增加 scale.strokes.grid 参数后，
//   测试断言改为正则匹配（接受可选的 scale 参数后缀）
test('网格线和十字线水平线仍延伸到 g.plotRight（保持全宽）', () => {
  const src = readSource()
  assert.ok(
    /drawLine\(ctx, g\.l, y, g\.plotRight, y, C\.grid(?:,\s*scale\.strokes\.grid)?\)/.test(src),
    '水平网格线必须延伸到 g.plotRight（可附加 scale.strokes.grid 参数）',
  )
  assert.ok(
    src.includes('drawLine(s2.ctx, g.l, my, g.plotRight, my,'),
    '十字线水平线必须延伸到 g.plotRight',
  )
})

// ===== 5. 时间轴标签跟随 bar 分布 =====
test('时间轴标签使用 effectivePlotW（跟随 bar 分布）', () => {
  const src = readSource()
  assert.ok(
    src.includes('g.l + effectivePlotW * i / (labels.length - 1)'),
    '时间轴标签必须使用 effectivePlotW 而非 plotW',
  )
})

// ===== 6. 不修改 Node/Profile/POC 算法 =====
test('不修改 Node/Profile/POC 算法（extractBackendProfile 保留）', () => {
  const src = readSource()
  assert.ok(
    src.includes('function extractBackendProfile'),
    'extractBackendProfile 函数必须保留（Node/Profile/POC 算法不变）',
  )
  assert.ok(
    src.includes('profile_rows'),
    'profile_rows 解析必须保留',
  )
  assert.ok(
    src.includes('peak_rows'),
    'peak_rows 解析必须保留',
  )
  assert.ok(
    src.includes('pocPrice'),
    'POC 价格解析必须保留',
  )
})

// ===== 7. state.step 保存压缩后的 step（交互命中检测使用） =====
test('state.step 保存压缩后的 step（交互命中检测使用）', () => {
  const src = readSource()
  assert.ok(
    src.includes('state.step = step'),
    'state.step 必须保存压缩后的 step',
  )
})
