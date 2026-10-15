// [PresentationSemanticRegistry] - 描述: Commit C / P0 展示语义中枢契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/presentationSemantics.test.ts
//
// 覆盖用户裁决验收点：
//   1. BOS+swing+bullish → 主要·多头突破
//   2. CHoCH+internal+bearish → 短线·转弱拐点
//   3. EQH → 双顶压力
//   4. EQL → 双底支撑
//   5. bullish → 多头
//   6. internal → 短线级别
//   7. 扩张 → 偏多
//   8. 收缩 → 偏空
//   9. 平缓 → 中性
//   10. unknown code 不原样输出（P0-7 铁律）
//   + 动量事件 / 结构对齐 / 节点事件 / 布尔 / 趋势方向 映射

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  formatStructureEvent,
  formatStructureDirection,
  formatStructureLevel,
  formatEventDirection,
  formatMomentumDirection,
  formatMomentumEvent,
  formatSqueezeState,
  formatAlignment,
  formatNodeEventType,
  formatBoolean,
  formatTrendDirection,
  STRUCTURE_EVENT_TYPE_OPTIONS,
  EVENT_DIRECTION_OPTIONS,
} from '../presentationSemantics.ts'

const UNKNOWN_CODE = '__INTERNAL_UNKNOWN_CODE__'

test('1. BOS+swing+bullish → 主要·多头突破', () => {
  assert.equal(
    formatStructureEvent({ type: 'BOS', level: 'swing', direction: 'bullish' }),
    '主要·多头突破',
  )
})

test('2. CHoCH+internal+bearish → 短线·转弱拐点', () => {
  assert.equal(
    formatStructureEvent({ type: 'CHoCH', level: 'internal', direction: 'bearish' }),
    '短线·转弱拐点',
  )
})

test('3. EQH → 双顶压力', () => {
  assert.equal(formatStructureEvent({ type: 'EQH' }), '双顶压力')
})

test('4. EQL → 双底支撑', () => {
  assert.equal(formatStructureEvent({ type: 'EQL' }), '双底支撑')
})

test('5. bullish → 多头', () => {
  assert.equal(formatStructureDirection('bullish'), '多头')
  assert.equal(formatStructureDirection('up'), '多头')
})

test('6. internal → 短线级别', () => {
  assert.equal(formatStructureLevel('internal'), '短线级别')
  assert.equal(formatStructureLevel('swing'), '主要级别')
})

test('7. 扩张 → 偏多', () => {
  assert.equal(formatMomentumDirection('扩张'), '偏多')
})

test('8. 收缩 → 偏空', () => {
  assert.equal(formatMomentumDirection('收缩'), '偏空')
})

test('9. 平缓 → 中性', () => {
  assert.equal(formatMomentumDirection('平缓'), '中性')
})

test('10. unknown code 绝不原样输出（P0-7 铁律）', () => {
  const formatters: Array<(v: unknown) => string> = [
    formatStructureDirection,
    formatStructureLevel,
    formatMomentumDirection,
    formatMomentumEvent,
    formatSqueezeState,
    formatAlignment,
    formatNodeEventType,
    formatBoolean,
    formatTrendDirection,
  ]
  for (const fn of formatters) {
    const out = fn(UNKNOWN_CODE)
    assert.ok(
      !out.includes(UNKNOWN_CODE),
      `formatter ${fn.name} 不得原样输出未知 code，实际得到：${out}`,
    )
  }
  // formatStructureEvent 接收对象，单独验证
  const evOut = formatStructureEvent({ type: UNKNOWN_CODE, level: UNKNOWN_CODE, direction: UNKNOWN_CODE })
  assert.ok(!evOut.includes(UNKNOWN_CODE), `formatStructureEvent 不得原样输出未知 code，实际：${evOut}`)
})

test('动量事件类型映射', () => {
  assert.equal(formatMomentumEvent('SQZ_OFF'), '挤压释放')
  assert.equal(formatMomentumEvent('MOMENTUM_DIFFUSION'), '动量扩散')
})

test('结构对齐映射（仅 fp_structure_alignment）', () => {
  assert.equal(formatAlignment('共振'), '长短结构同向')
  assert.equal(formatAlignment('背离'), '长短结构分歧')
})

test('筹码节点事件类型映射（P0 corrective：节点簇→成交密集区）', () => {
  assert.equal(formatNodeEventType('node_cluster_touch'), '触及成交密集区')
})

// ===== P0 corrective / Blocker 1：OB 生命周期 allowlist（禁止 startsWith('OB') 一刀切）=====
test('OB_CREATED + bullish + swing → 主要·多头承接区形成', () => {
  assert.equal(
    formatStructureEvent({ type: 'OB_CREATED', level: 'swing', direction: 'bullish' }),
    '主要·多头承接区形成',
  )
})

test('OB_ENTERED + bullish + swing → 主要·多头承接区首次回踩', () => {
  assert.equal(
    formatStructureEvent({ type: 'OB_ENTERED', level: 'swing', direction: 'bullish' }),
    '主要·多头承接区首次回踩',
  )
})

test('OB_MITIGATED + bearish + internal → 短线·空头压制区失效', () => {
  assert.equal(
    formatStructureEvent({ type: 'OB_MITIGATED', level: 'internal', direction: 'bearish' }),
    '短线·空头压制区失效',
  )
})

test('OB_ENTRY（历史兼容）→ 主要·多头承接区进入', () => {
  assert.equal(
    formatStructureEvent({ type: 'OB_ENTRY', level: 'swing', direction: 'bullish' }),
    '主要·多头承接区进入',
  )
})

test('OB 未知子码 → 结构未知（禁止误判为合法 OB）', () => {
  assert.equal(
    formatStructureEvent({ type: 'OB_SOME_UNKNOWN_INTERNAL_CODE', level: 'swing', direction: 'bullish' }),
    '结构未知',
  )
})

// ===== P0 corrective / Blocker 2：通用事件方向 formatter =====
test('formatEventDirection：bullish/bearish → 多头/空头，up/down 历史兼容', () => {
  assert.equal(formatEventDirection('bullish'), '多头')
  assert.equal(formatEventDirection('bearish'), '空头')
  assert.equal(formatEventDirection('up'), '多头')
  assert.equal(formatEventDirection('down'), '空头')
  assert.equal(formatEventDirection(UNKNOWN_CODE), '方向未知')
})

test('formatStructureDirection 复用 formatEventDirection（多头/空头一致）', () => {
  assert.equal(formatStructureDirection('bullish'), formatEventDirection('bullish'))
  assert.equal(formatStructureDirection('bearish'), formatEventDirection('bearish'))
  assert.equal(formatStructureDirection('up'), '多头')
})

// ===== P0 corrective / Blocker 3：方向下拉选项只暴露正式值 =====
test('EVENT_DIRECTION_OPTIONS 仅 bullish/bearish（up/down 历史兼容不进用户 UI）', () => {
  assert.deepEqual(
    EVENT_DIRECTION_OPTIONS.map((o) => o.value),
    ['bullish', 'bearish'],
  )
})

// ===== [STRUCT-EVENT-SPLIT] 结构事件筛选 label：承接/压制区方向拆分为 6 个独立选项 =====
test('结构事件筛选 label：6 个承接/压制独立选项 + BOS/CHoCH/EQH/EQL', () => {
  const map = Object.fromEntries(STRUCTURE_EVENT_TYPE_OPTIONS.map((o) => [o.value, o.label]))
  // 6 个复合选项：type#direction 编码承接(bullish)/压制(bearish)
  assert.equal(map['OB_CREATED#bullish'], '承接区形成')
  assert.equal(map['OB_CREATED#bearish'], '压制区形成')
  assert.equal(map['OB_ENTERED#bullish'], '承接区首次回踩')
  assert.equal(map['OB_ENTERED#bearish'], '压制区首次回踩')
  assert.equal(map['OB_MITIGATED#bullish'], '承接区失效')
  assert.equal(map['OB_MITIGATED#bearish'], '压制区失效')
  // 保留项
  assert.equal(map['BOS'], '结构突破')
  assert.equal(map['CHoCH'], '结构转折')
  assert.equal(map['EQH'], '双顶压力')
  assert.equal(map['EQL'], '双底支撑')
  // 旧合并值已从 UI 选项移除（历史 URL/preset 由后端按合并语义兼容，不悄悄变成 6 个之一）
  assert.equal(map['OB_CREATED'], undefined)
  assert.equal(map['OB_ENTERED'], undefined)
  assert.equal(map['OB_MITIGATED'], undefined)
  // 承接/压制方向严格互斥：同一 type 的两个选项方向不同
  assert.notEqual(map['OB_CREATED#bullish'], map['OB_CREATED#bearish'])
})

test('布尔映射', () => {
  assert.equal(formatBoolean(true), '是')
  assert.equal(formatBoolean(false), '否')
})

test('趋势方向映射 + fail-closed', () => {
  assert.equal(formatTrendDirection('上行'), '上行')
  assert.equal(formatTrendDirection('down'), '下行')
  assert.equal(formatTrendDirection('sideways'), '震荡')
  assert.equal(formatTrendDirection(UNKNOWN_CODE), '方向未知')
})

test('挤压状态 passthrough + fail-closed', () => {
  assert.equal(formatSqueezeState('挤压中'), '挤压中')
  assert.equal(formatSqueezeState('已释放'), '已释放')
  assert.equal(formatSqueezeState('无挤压'), '无挤压')
  assert.equal(formatSqueezeState(UNKNOWN_CODE), '未知状态')
})
