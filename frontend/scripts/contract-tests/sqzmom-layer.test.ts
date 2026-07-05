// [SQZMOM_LB] - 描述: SQZMOM 副图层契约测试（前端只渲染后端返回序列，禁止重算指标）
// 用法：node --experimental-strip-types --test scripts/contract-tests/sqzmom-layer.test.ts
// 覆盖：
// 1. SQZMOM 开关默认关闭（LAYERS.sqzmom.defaultVisible=false, getDefaultLayers sqzmom:false, 三个策略 defaultLayers 不含 'sqzmom'）
// 2. SQZMOM renderer 已注册（LayerRenderer 类型含 'sqzmom', renderIndicatorLayer switch 含 case 'sqzmom'）
// 3. SQZMOM pane 在 geometry 中分配（geometry 函数源码含 sqzmomOn 和 panes.sqzmom）
// 4. API 缺少 sqzmom_lb 时页面不崩溃（renderIndicatorSqzmom 函数存在且含早返回 guard）
// 5. 前端不重新计算 SQZMOM 指标（源码不含 linreg/stdev/multKC/_linreg_pine/_stdev_biased 标识符）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')
const MANIFEST_PATH = join(FRONTEND_ROOT, 'src', 'lib', 'strategy-manifest.ts')
const CHART_PATH = join(FRONTEND_ROOT, 'src', 'components', 'StrategyChart.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. SQZMOM 开关默认关闭 =====
test('SQZMOM toggle default off', () => {
  const manifestSrc = readSource(MANIFEST_PATH)
  const chartSrc = readSource(CHART_PATH)

  // LAYERS.sqzmom 必须存在且 defaultVisible=false
  const sqzmomLayerMatch = manifestSrc.match(/sqzmom:\s*\{[^}]*defaultVisible:\s*(true|false)/)
  assert.ok(sqzmomLayerMatch, 'strategy-manifest.ts 必须定义 LAYERS.sqzmom 条目（含 defaultVisible 字段）')
  assert.equal(
    sqzmomLayerMatch[1],
    'false',
    'LAYERS.sqzmom.defaultVisible 必须为 false（SQZMOM 开关默认关闭）',
  )

  // getDefaultLayers 必须含 sqzmom: false
  const defaultLayersMatch = chartSrc.match(/function\s+getDefaultLayers[\s\S]*?\{([\s\S]*?)\}/)
  assert.ok(defaultLayersMatch, 'StrategyChart.tsx 必须定义 getDefaultLayers 函数')
  assert.ok(
    /sqzmom:\s*false/.test(defaultLayersMatch[1]),
    'getDefaultLayers 必须包含 sqzmom: false（默认关闭）',
  )

  // 三个策略的 defaultLayers 数组不应包含 'sqzmom'
  // 提取所有 defaultLayers: [...] 数组
  const defaultLayersArrays = manifestSrc.matchAll(/defaultLayers:\s*\[([^\]]*)\]/g)
  for (const m of defaultLayersArrays) {
    const arrContent = m[1]
    assert.ok(
      !/'sqzmom'/.test(arrContent) && !/"sqzmom"/.test(arrContent),
      `策略 defaultLayers 数组禁止包含 'sqzmom'（用户必须手动开启）。违规数组: [${arrContent}]`,
    )
  }
})

// ===== 2. SQZMOM renderer 已注册 =====
test('SQZMOM renderer registered', () => {
  const manifestSrc = readSource(MANIFEST_PATH)
  const chartSrc = readSource(CHART_PATH)

  // LayerRenderer 类型联合必须包含 'sqzmom'
  const rendererTypeMatch = manifestSrc.match(/export\s+type\s+LayerRenderer\s*=\s*([\s\S]*?)(?:\nexport|\n\/\/)/)
  assert.ok(rendererTypeMatch, 'strategy-manifest.ts 必须定义 LayerRenderer 类型')
  assert.ok(
    /'sqzmom'/.test(rendererTypeMatch[1]),
    "LayerRenderer 类型联合必须包含 'sqzmom'",
  )

  // renderIndicatorLayer switch 必须含 case 'sqzmom'
  const switchMatch = chartSrc.match(/function\s+renderIndicatorLayer[\s\S]*?switch\s*\([\s\S]*?\{([\s\S]*?)\n\s*\}/)
  assert.ok(switchMatch, 'StrategyChart.tsx 必须定义 renderIndicatorLayer 函数（含 switch）')
  assert.ok(
    /case\s+'sqzmom'/.test(switchMatch[1]),
    "renderIndicatorLayer switch 必须包含 case 'sqzmom' 分支",
  )
})

// ===== 3. SQZMOM pane 在 geometry 中分配 =====
test('SQZMOM pane allocated in geometry', () => {
  const chartSrc = readSource(CHART_PATH)

  // geometry 函数必须含 sqzmomOn 变量
  const geometryMatch = chartSrc.match(/function\s+geometry\s*\(([\s\S]*?)\{([\s\S]*?)\n\}/)
  assert.ok(geometryMatch, 'StrategyChart.tsx 必须定义 geometry 函数')
  const geometryBody = geometryMatch[2]
  assert.ok(
    /sqzmomOn/.test(geometryBody),
    'geometry 函数体必须包含 sqzmomOn 变量（用于判断 sqzmom pane 是否启用）',
  )
  assert.ok(
    /panes\.sqzmom/.test(geometryBody),
    'geometry 函数体必须包含 panes.sqzmom 分配（sqzmom pane 矩形）',
  )
})

// ===== 4. API 缺少 sqzmom_lb 时页面不崩溃 =====
test('API missing sqzmom_lb does not crash', () => {
  const chartSrc = readSource(CHART_PATH)

  // 必须存在 renderIndicatorSqzmom 函数
  assert.ok(
    /function\s+renderIndicatorSqzmom\s*\(/.test(chartSrc),
    'StrategyChart.tsx 必须定义 renderIndicatorSqzmom 函数',
  )

  // 提取 renderIndicatorSqzmom 函数体（从函数签名到下一个同级 function 或注释块）
  // 注意：参数类型含 Record<string, (number | string | null)[]>，括号嵌套，
  // 不能用 [^)]* 匹配参数列表；改用 [\s\S]*? 配合 ): void { 锚点
  const fnMatch = chartSrc.match(/function\s+renderIndicatorSqzmom\s*\([\s\S]*?\)\s*:\s*void\s*\{([\s\S]*?)\n\}/)
  assert.ok(fnMatch, 'renderIndicatorSqzmom 函数体必须存在且闭合')
  const fnBody = fnMatch[1]

  // 必须对 sqzmom_val 数据缺失做早返回 guard（不抛异常）
  // 接受以下任一形式的 guard：
  //   if (!vals?.length) return
  //   if (!vals || !vals.length) return
  //   if (!sqzmomVals?.length) return
  const hasEarlyReturnGuard =
    /if\s*\(\s*!\w*[Vv]als?\??\.length\s*\)\s*return/.test(fnBody) ||
    /if\s*\(\s*!\w*[Vv]als?\s*\|\|\s*!\w*[Vv]als?\.length\s*\)\s*return/.test(fnBody)
  assert.ok(
    hasEarlyReturnGuard,
    'renderIndicatorSqzmom 必须对 sqzmom_val 数据缺失做早返回 guard（API 缺失时不崩溃）',
  )

  // 必须引用 g.panes.sqzmom（而非 macd）
  assert.ok(
    /g\.panes\.sqzmom/.test(fnBody),
    'renderIndicatorSqzmom 必须使用 g.panes.sqzmom（sqzmom pane 矩形）',
  )
})

// ===== 5. 前端不重新计算 SQZMOM 指标 =====
test('frontend does not recompute SQZMOM', () => {
  const chartSrc = readSource(CHART_PATH)

  // 禁止出现 Pine 算法标识符（前端不应重新实现指标计算）
  const forbiddenIdentifiers = [
    'linreg',
    'stdev',
    'multKC',
    '_linreg_pine',
    '_stdev_biased',
    'compute_sqzmom_lb',
  ]
  for (const id of forbiddenIdentifiers) {
    const re = new RegExp(`\\b${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`)
    assert.ok(
      !re.test(chartSrc),
      `StrategyChart.tsx 禁止出现算法标识符 '${id}'（前端不得重新计算 SQZMOM 指标）`,
    )
  }

  // 必须从后端返回的 data 字段读取（不重新计算）
  assert.ok(
    /data\.sqzmom_val/.test(chartSrc),
    'StrategyChart.tsx 必须从 data.sqzmom_val 读取后端返回的 val 序列',
  )
  assert.ok(
    /data\.sqzmom_bcolor/.test(chartSrc),
    'StrategyChart.tsx 必须从 data.sqzmom_bcolor 读取后端返回的颜色序列',
  )
  assert.ok(
    /data\.sqzmom_scolor/.test(chartSrc),
    'StrategyChart.tsx 必须从 data.sqzmom_scolor 读取后端返回的 squeeze marker 颜色',
  )
})
