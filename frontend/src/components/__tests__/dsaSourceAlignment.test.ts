// [DSA Source Alignment] - 描述: DSA overlay source 对齐前端契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/dsaSourceAlignment.test.ts
//   覆盖：
//     1. normalizeChartTime 对 naive / aware (+08:00) ISO 时间戳规范化一致
//     2. timeTicks 15m 时间轴刻度按 Asia/Shanghai 显示（不显示 03:00 等错误时间）
//     3. 15m source_bar_times（无时区）与 K线 trade_time（+08:00）canonical 匹配
//     4. 1d 仍按日期粒度匹配，向后兼容
//
//   修复根因：
//     - 后端 15m/1h trade_time 之前返回 naive datetime，前端 new Date("2026-07-06T15:00:00")
//       在非亚洲时区浏览器中当作本地时间，转 Asia/Shanghai 后显示 2026-07-07 03:00
//     - 后端 source_bar_times 之前永远用日线日期格式，15m/1h 无法与 K线时间对齐
//     - 修复后：后端返回 aware datetime(+08:00)，source_bar_times 按 timeframe 格式化

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { normalizeChartTime, timeTicks } from '../../utils/chartTime.ts'
import type { BarData } from '@/components/StrategyChart'

// ===== 1. normalizeChartTime：15m/1h 规范化 =====

test('normalizeChartTime: 15m aware ISO (+08:00) 提取 date + HH:MM', () => {
  // 后端修复后返回 aware datetime，序列化为 +08:00 后缀
  const key = normalizeChartTime('2026-07-06T15:00:00+08:00', '15m')
  assert.equal(key, '2026-07-06 15:00')
})

test('normalizeChartTime: 15m naive ISO 提取 date + HH:MM', () => {
  // 后端 source_bar_times 永远是 naive（无时区后缀）
  const key = normalizeChartTime('2026-07-06T15:00:00', '15m')
  assert.equal(key, '2026-07-06 15:00')
})

test('normalizeChartTime: 15m naive 与 aware 产生相同 canonical key', () => {
  // 关键不变量：DSA source mismatch 比较不依赖时区后缀
  const naive = normalizeChartTime('2026-07-06T15:00:00', '15m')
  const aware = normalizeChartTime('2026-07-06T15:00:00+08:00', '15m')
  assert.equal(naive, aware, '15m naive 与 aware 必须产生相同 canonical key')
})

test('normalizeChartTime: 1h aware ISO 提取 date + HH:MM', () => {
  const key = normalizeChartTime('2026-07-06T14:00:00+08:00', '1h')
  assert.equal(key, '2026-07-06 14:00')
})

test('normalizeChartTime: 1d trade_date 仅返回日期', () => {
  const key = normalizeChartTime('2026-07-06', '1d')
  assert.equal(key, '2026-07-06')
})

test('normalizeChartTime: 1d 带时间也能提取日期（向后兼容）', () => {
  // 后端 1d source_bar_times 是 YYYY-MM-DD；K线 1d trade_date 也是 YYYY-MM-DD
  // 即使误传带时间，仍提取日期，避免空 canonical key 导致 mismatch
  const key = normalizeChartTime('2026-07-06T15:00:00', '1d')
  assert.equal(key, '2026-07-06')
})

test('normalizeChartTime: 15m 无时间部分返回 null（避免误匹配日线）', () => {
  // 防御性：若误把日线日期作为 15m 时间传入，不返回日期键，避免与 1d 匹配混淆
  const key = normalizeChartTime('2026-07-06', '15m')
  assert.equal(key, null)
})

test('normalizeChartTime: 无效格式返回 null', () => {
  assert.equal(normalizeChartTime('', '15m'), null)
  assert.equal(normalizeChartTime(null, '15m'), null)
  assert.equal(normalizeChartTime(undefined, '15m'), null)
  assert.equal(normalizeChartTime('invalid', '15m'), null)
})

// ===== 2. DSA mismatch 场景：15m 不应误报 =====

test('DSA mismatch: 15m K线 aware 与 source_bar_times naive 全部匹配', () => {
  // 模拟后端修复后的实际场景：
  //   K线 trade_time = "2026-07-06T15:00:00+08:00"（aware）
  //   source_bar_times = ["2026-07-06T14:45:00", "2026-07-06T15:00:00"]（naive）
  const klineTimes = [
    '2026-07-06T14:45:00+08:00',
    '2026-07-06T15:00:00+08:00',
  ]
  const sourceBarTimes = [
    '2026-07-06T14:45:00',
    '2026-07-06T15:00:00',
  ]

  const klineKeys = new Set<string>()
  klineTimes.forEach(t => {
    const k = normalizeChartTime(t, '15m')
    if (k != null) klineKeys.add(k)
  })
  const indicatorKeys = new Set<string>()
  sourceBarTimes.forEach(t => {
    const k = normalizeChartTime(t, '15m')
    if (k != null) indicatorKeys.add(k)
  })

  let matched = 0
  klineKeys.forEach(k => { if (indicatorKeys.has(k)) matched++ })
  const ratio = klineKeys.size > 0 ? matched / klineKeys.size : 0

  assert.equal(klineKeys.size, 2, 'K线 canonical keys 应为 2 个')
  assert.equal(matched, 2, '所有 K线 时间应在 source_bar_times 中匹配')
  assert.equal(ratio, 1.0, '匹配率应为 100%，不应触发 DSA source mismatch')
})

test('DSA mismatch: 真实 source mismatch（日线日期作为 15m source）仍触发暂停', () => {
  // 防御性：若后端 bug 导致 source_bar_times 仍是日线日期格式，
  // 15m normalizeChartTime 返回 null，indicatorKeys 为空，matched=0，触发 mismatch
  const klineTimes = ['2026-07-06T14:45:00+08:00', '2026-07-06T15:00:00+08:00']
  const wrongSourceBarTimes = ['2026-07-05', '2026-07-06'] // 日线日期格式

  const klineKeys = new Set<string>()
  klineTimes.forEach(t => {
    const k = normalizeChartTime(t, '15m')
    if (k != null) klineKeys.add(k)
  })
  const indicatorKeys = new Set<string>()
  wrongSourceBarTimes.forEach(t => {
    const k = normalizeChartTime(t, '15m')
    if (k != null) indicatorKeys.add(k)
  })

  let matched = 0
  klineKeys.forEach(k => { if (indicatorKeys.has(k)) matched++ })
  const ratio = klineKeys.size > 0 ? matched / klineKeys.size : 0

  assert.equal(indicatorKeys.size, 0, '日线日期作为 15m source 应无法规范化（返回 null）')
  assert.equal(matched, 0, '不应匹配任何 K线 时间')
  assert.ok(ratio < 0.5, `匹配率 ${(ratio * 100).toFixed(1)}% 应 < 50%，触发 DSA source mismatch`)
})

test('DSA mismatch: 1d K线 trade_date 与 source_bar_times 全部匹配', () => {
  // 1d 场景：K线 trade_date="2026-07-06"，source_bar_times=["2026-07-06"]
  const klineTimes = ['2026-07-06', '2026-07-05']
  const sourceBarTimes = ['2026-07-05', '2026-07-06']

  const klineKeys = new Set<string>()
  klineTimes.forEach(t => {
    const k = normalizeChartTime(t, '1d')
    if (k != null) klineKeys.add(k)
  })
  const indicatorKeys = new Set<string>()
  sourceBarTimes.forEach(t => {
    const k = normalizeChartTime(t, '1d')
    if (k != null) indicatorKeys.add(k)
  })

  let matched = 0
  klineKeys.forEach(k => { if (indicatorKeys.has(k)) matched++ })
  const ratio = klineKeys.size > 0 ? matched / klineKeys.size : 0

  assert.equal(ratio, 1.0, '1d 应 100% 匹配，不触发 mismatch')
})

// ===== 3. timeTicks：15m 时间轴刻度按 Asia/Shanghai 显示 =====

test('timeTicks: 15m aware 时间 (+08:00) 显示北京交易时间，不显示 03:00', () => {
  // 后端修复后返回 aware datetime，前端 new Date(...) 正确解析为 UTC 时刻
  // 再 Intl.DateTimeFormat(timeZone: 'Asia/Shanghai') 显示北京交易时间
  const bars: BarData[] = [
    {
      time: '2026-07-06T14:45:00+08:00',
      open: 10, high: 11, low: 9, close: 10.5, volume: 100,
    },
    {
      time: '2026-07-06T15:00:00+08:00',
      open: 10.5, high: 11, low: 10, close: 10.8, volume: 200,
    },
  ]
  const ticks = timeTicks(bars, 2, '15m')
  assert.equal(ticks.length, 2)
  // 第一根：14:45 北京时间
  assert.match(ticks[0].label, /14:45/, `应显示 14:45 北京交易时间，实际: ${ticks[0].label}`)
  // 第二根：15:00 北京时间
  assert.match(ticks[1].label, /15:00/, `应显示 15:00 北京交易时间，实际: ${ticks[1].label}`)
  // 关键不变量：不显示 03:00 错误时间
  assert.doesNotMatch(ticks[0].label, /03:00/, '不应显示 03:00（naive datetime 时区误判）')
  assert.doesNotMatch(ticks[1].label, /03:00/, '不应显示 03:00（naive datetime 时区误判）')
})

test('timeTicks: 15m naive 时间（旧后端兼容）能生成刻度不崩溃', () => {
  // 注意：此测试在非亚洲时区 CI 环境中验证 naive datetime 会显示错误时间
  // 在 Asia/Shanghai CI 环境中 naive 时间会"碰巧正确"，需跨时区 CI 验证
  // 但生产真实浏览器多为非亚洲时区，naive 必然错误
  // 此处仅断言不崩溃，具体时间显示依赖 CI 时区（已知 Asia/Shanghai CI 显示 15:00，
  // 非亚洲时区 CI 显示错误时间如 03:00/02:00/04:00 - 这正是后端修复后必须返回 +08:00 的原因）
  const bars: BarData[] = [
    {
      time: '2026-07-06T14:45:00', // naive，无时区后缀
      open: 10, high: 11, low: 9, close: 10.5, volume: 100,
    },
    {
      time: '2026-07-06T15:00:00',
      open: 10.5, high: 11, low: 10, close: 10.8, volume: 200,
    },
  ]
  const ticks = timeTicks(bars, 2, '15m')
  assert.equal(ticks.length, 2)
  assert.ok(ticks[0].label.length > 0, '应生成非空 label')
  assert.ok(ticks[1].label.length > 0, '应生成非空 label')
})

test('timeTicks: 1d 仅显示月-日', () => {
  const bars: BarData[] = [
    { time: '2026-07-05', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
    { time: '2026-07-06', open: 10.5, high: 11, low: 10, close: 10.8, volume: 200 },
  ]
  const ticks = timeTicks(bars, 2, '1d')
  assert.equal(ticks.length, 2)
  assert.match(ticks[0].label, /07-05/, `1d 应显示 07-05，实际: ${ticks[0].label}`)
  assert.match(ticks[1].label, /07-06/, `1d 应显示 07-06，实际: ${ticks[1].label}`)
})

// ===== 4. DSA Overlay Policy：全周期支持 + title 按周期区分 =====

import {
  DSA_TITLE_HINT,
  shouldAllowDsaOverlay,
  shouldCheckDsaMismatch,
} from '../../utils/dsaOverlayPolicy.ts'

test('shouldAllowDsaOverlay: 1d/15m/1h/1w/1mo 全部允许 DSA overlay', () => {
  // [PR #32] - DSA VWAP 支持全周期，不再 1d-only
  assert.equal(shouldAllowDsaOverlay('1d'), true, '1d 应允许 DSA overlay')
  assert.equal(shouldAllowDsaOverlay('15m'), true, '15m 应允许 DSA overlay')
  assert.equal(shouldAllowDsaOverlay('1h'), true, '1h 应允许 DSA overlay')
  assert.equal(shouldAllowDsaOverlay('1w'), true, '1w 应允许 DSA overlay')
  assert.equal(shouldAllowDsaOverlay('1mo'), true, '1mo 应允许 DSA overlay')
})

test('shouldCheckDsaMismatch: 1d/15m/1h/1w/1mo 全部校验 mismatch', () => {
  // [PR #32] - DSA 全周期渲染，全部需要校验 source mismatch
  assert.equal(shouldCheckDsaMismatch('1d'), true, '1d 应校验 DSA mismatch')
  assert.equal(shouldCheckDsaMismatch('15m'), true, '15m 应校验 DSA mismatch')
  assert.equal(shouldCheckDsaMismatch('1h'), true, '1h 应校验 DSA mismatch')
  assert.equal(shouldCheckDsaMismatch('1w'), true, '1w 应校验 DSA mismatch')
  assert.equal(shouldCheckDsaMismatch('1mo'), true, '1mo 应校验 DSA mismatch')
})

test('DSA_TITLE_HINT: 1d 含"日线结构锚"', () => {
  const hint = DSA_TITLE_HINT('1d')
  assert.match(
    hint,
    /日线结构锚/,
    `1d DSA title 应含"日线结构锚"，实际: ${hint}`,
  )
})

test('DSA_TITLE_HINT: 非 1d 含"当前周期验证图层"', () => {
  // [PR #32] - 非 1d 周期 DSA 是验证图层，不作为主趋势锚
  for (const tf of ['15m', '1h', '1w', '1mo']) {
    const hint = DSA_TITLE_HINT(tf)
    assert.match(
      hint,
      /当前周期验证图层/,
      `${tf} DSA title 应含"当前周期验证图层"，实际: ${hint}`,
    )
    assert.doesNotMatch(
      hint,
      /日线结构锚/,
      `${tf} DSA title 不应含"日线结构锚"，实际: ${hint}`,
    )
  }
})

// ===== 5. Overlay Render/Toggle/Y-Axis Decisions：彻底移除 1d-only / 1w-1mo skip 硬编码 =====
//
// [PR #33] - 修 PR #32 遗留：StrategyChart 仍有 4 处硬编码 skip
//   L1661: if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return
//   L1666: if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return
//   L2226: if (groupId === 'dsa' && timeframe !== '1d') return
//   L1503: if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')
//
// 修复：提取为纯函数 shouldRenderDsaLayer / shouldRenderBbLayer / shouldToggleDsa / shouldIncludeDsaInPriceRange
// 决策只受 layers / mismatch / capture 模式控制，不受 timeframe 跳过

import {
  shouldAllowBbOverlay,
  shouldIncludeDsaInPriceRange,
  shouldRenderBbLayer,
  shouldRenderDsaLayer,
  shouldToggleDsa,
} from '../../utils/dsaOverlayPolicy.ts'

const FEISHU_CAPTURE_LAYERS = ['dsa', 'bb', 'profile', 'node', 'poc'] as const

// --- shouldRenderDsaLayer ---

test('shouldRenderDsaLayer: layer_id 非 dsa_vwap 返回 false', () => {
  assert.equal(
    shouldRenderDsaLayer('bb', { dsa: true }, false, '1d'),
    false,
    '非 dsa_vwap layer 不应渲染 DSA',
  )
})

test('shouldRenderDsaLayer: layers.dsa=false 时全周期 false（开关关闭）', () => {
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldRenderDsaLayer('dsa_vwap', { dsa: false }, false, tf),
      false,
      `${tf} layers.dsa=false 不应渲染 DSA`,
    )
  }
})

test('shouldRenderDsaLayer: dsaSourceMismatch=true 时全周期 false（source 不对齐时跳过）', () => {
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldRenderDsaLayer('dsa_vwap', { dsa: true }, true, tf),
      false,
      `${tf} dsaSourceMismatch=true 不应渲染 DSA（保留 source mismatch 保护）`,
    )
  }
})

test('shouldRenderDsaLayer: layers.dsa=true + dsaSourceMismatch=false 时全周期 true（不再 1d-only）', () => {
  // [PR #33] - 修复 L1661: 之前 if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return
  // 现在非 1d 周期 DSA 也可渲染
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldRenderDsaLayer('dsa_vwap', { dsa: true }, false, tf),
      true,
      `${tf} layers.dsa=true + matched 应渲染 DSA`,
    )
  }
})

// --- shouldRenderBbLayer ---

test('shouldAllowBbOverlay: 1d/15m/1h/1w/1mo 全部允许 BB overlay', () => {
  // [PR #33] - BB 全周期支持，1w/1mo 不再被 skip
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldAllowBbOverlay(tf),
      true,
      `${tf} 应允许 BB overlay`,
    )
  }
})

test('shouldRenderBbLayer: layer_id 非 bb 返回 false', () => {
  assert.equal(
    shouldRenderBbLayer('dsa_vwap', { bb: true }, '1d'),
    false,
    '非 bb layer 不应渲染 BB',
  )
})

test('shouldRenderBbLayer: layers.bb=false 时全周期 false（开关关闭）', () => {
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldRenderBbLayer('bb', { bb: false }, tf),
      false,
      `${tf} layers.bb=false 不应渲染 BB`,
    )
  }
})

test('shouldRenderBbLayer: layers.bb=true 时 1w/1mo 也 true（不再 skip）', () => {
  // [PR #33] - 修复 L1666: 之前 if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return
  // 现在 1w/1mo BB 正常渲染
  assert.equal(shouldRenderBbLayer('bb', { bb: true }, '1w'), true, '1w BB 应渲染')
  assert.equal(shouldRenderBbLayer('bb', { bb: true }, '1mo'), true, '1mo BB 应渲染')
  assert.equal(shouldRenderBbLayer('bb', { bb: true }, '1d'), true, '1d BB 应渲染')
  assert.equal(shouldRenderBbLayer('bb', { bb: true }, '15m'), true, '15m BB 应渲染')
  assert.equal(shouldRenderBbLayer('bb', { bb: true }, '1h'), true, '1h BB 应渲染')
})

// --- shouldToggleDsa ---

test('shouldToggleDsa: capture 模式锁定 dsa 时返回 false（保留 capture 锁定）', () => {
  // [feishu-capture] - 截图模式下 DSA 不可关闭
  assert.equal(
    shouldToggleDsa('dsa', true, FEISHU_CAPTURE_LAYERS),
    false,
    'capture 模式 DSA 不可 toggle',
  )
})

test('shouldToggleDsa: 非 capture 模式 groupId 非 dsa 时返回 true（不归此函数管）', () => {
  // 非 dsa group 的 toggle 由其他逻辑控制（保留 bb/profile/node/poc 的 toggle）
  assert.equal(
    shouldToggleDsa('bb', false, FEISHU_CAPTURE_LAYERS),
    true,
    '非 dsa group 不归此函数管，应返回 true 不阻塞',
  )
})

test('shouldToggleDsa: 非 capture 模式 + groupId=dsa 返回 true（不再 1d-only）', () => {
  // [PR #33] - 修复 L2226: 之前 if (groupId === 'dsa' && timeframe !== '1d') return
  // 现在 DSA toggle 全周期可切换（timeframe 不再参与决策）
  assert.equal(
    shouldToggleDsa('dsa', false, FEISHU_CAPTURE_LAYERS),
    true,
    '非 capture 模式 DSA toggle 应可切换',
  )
})

// --- shouldIncludeDsaInPriceRange ---

test('shouldIncludeDsaInPriceRange: layer_id 非 dsa_vwap 返回 false', () => {
  assert.equal(
    shouldIncludeDsaInPriceRange('bb', { dsa: true }, '1d'),
    false,
    '非 dsa_vwap layer 不参与 y-axis range',
  )
})

test('shouldIncludeDsaInPriceRange: layers.dsa=false 时全周期 false（开关关闭）', () => {
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldIncludeDsaInPriceRange('dsa_vwap', { dsa: false }, tf),
      false,
      `${tf} layers.dsa=false 不参与 y-axis range`,
    )
  }
})

test('shouldIncludeDsaInPriceRange: layers.dsa=true 时全周期 true（不再仅 1d 纳入）', () => {
  // [PR #33] - 修复 L1503: 之前 if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')
  // 现在 DSA 全周期参与 y-axis range，避免非 1d DSA 被轴范围挤掉
  for (const tf of ['1d', '15m', '1h', '1w', '1mo']) {
    assert.equal(
      shouldIncludeDsaInPriceRange('dsa_vwap', { dsa: true }, tf),
      true,
      `${tf} DSA 应参与 y-axis range`,
    )
  }
})
