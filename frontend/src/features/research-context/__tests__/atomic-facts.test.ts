// [AtomicFactsContract] - 描述: Atomic Fact Contract V1 前端契约测试
// 用法：node --experimental-strip-types --test src/features/research-context/__tests__/atomic-facts.test.ts
// 覆盖：
// 1. 后端 Canonical Registry 14/10/1、顺序、ID 唯一
// 2. V1 永久缺席（rejected 且不在 core/aux）
// 3. T3/T6 默认隐藏（auxiliary default_ui_enabled=false）
// 4. 前端 endpoints.ts 定义 AtomicFactsContextResponse 且含合同字段
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
// __dirname = frontend/src/features/research-context/__tests__
// FRONTEND_ROOT = frontend/src（上 3 级）
const FRONTEND_ROOT = join(__dirname, '..', '..', '..')
// BACKEND_ROOT = /root/web_dev/backend（再上 2 级）
const BACKEND_ROOT = join(FRONTEND_ROOT, '..', '..', 'backend')
const CONTRACT_PATH = join(BACKEND_ROOT, 'app', 'contracts', 'atomic_fact_contract_v1.json')
const ENDPOINTS_PATH = join(FRONTEND_ROOT, 'api', 'endpoints.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

const contract = JSON.parse(readSource(CONTRACT_PATH))

test('Canonical Registry: 14 core / 10 auxiliary / 1 rejected', () => {
  assert.equal(contract.core_facts.length, 14, 'core 必须 14 项')
  assert.equal(contract.auxiliary_facts.length, 10, 'auxiliary 必须 10 项')
  assert.equal(contract.rejected_facts.length, 1, 'rejected 必须 1 项')
})

test('fact ID 唯一且 core 顺序固定', () => {
  const coreIds = contract.core_facts.map((f: { id: string }) => f.id)
  const auxIds = contract.auxiliary_facts.map((f: { id: string }) => f.id)
  const rejIds = contract.rejected_facts.map((f: { id: string }) => f.id)
  const all = [...coreIds, ...auxIds, ...rejIds]
  assert.equal(new Set(all).size, 25, 'fact ID 必须唯一（共 25）')

  const expectedOrder = [
    'T1_trend_direction', 'T2_aligned_slope', 'T4_trend_age', 'T5_slope_ratio',
    'M1_momentum_alignment', 'M2_aligned_momentum', 'M3_aligned_momentum_delta', 'M5_squeeze_state',
    'S1_confirmed_boundary_relation', 'S2_active_dir_relation', 'S3_active_position',
    'S7_dist_favorable_boundary', 'S8_dist_adverse_boundary',
    'V3_avg_volume_ratio',
  ]
  assert.deepEqual(coreIds, expectedOrder, 'core ID 顺序必须固定（趋势4/动量4/结构5/成交1）')
})

test('V1 永久缺席（rejected 且不在 core/aux）', () => {
  const rej = contract.rejected_facts[0]
  assert.equal(rej.id, 'V1_cumulative_volume_ratio', 'rejected 必须是 V1 累计成交量比')
  const coreIds = contract.core_facts.map((f: { id: string }) => f.id)
  const auxIds = contract.auxiliary_facts.map((f: { id: string }) => f.id)
  assert.ok(
    !coreIds.includes(rej.id) && !auxIds.includes(rej.id),
    'V1 不得出现在 core/aux（永不进入 UI/摘要）',
  )
})

test('T3/T6 默认隐藏（default_ui_enabled=false）', () => {
  const t3 = contract.auxiliary_facts.find((f: { id: string }) => f.id === 'T3_trend_efficiency')
  const t6 = contract.auxiliary_facts.find((f: { id: string }) => f.id === 'T6_efficiency_delta')
  assert.ok(t3, 'contract 必须含 T3')
  assert.ok(t6, 'contract 必须含 T6')
  assert.equal(t3.default_ui_enabled, false, 'T3 默认隐藏')
  assert.equal(t6.default_ui_enabled, false, 'T6 默认隐藏')
})

test('前端 endpoints.ts 定义 AtomicFactsContextResponse 契约字段', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export interface AtomicFactsContextResponse/.test(src),
    'endpoints.ts 必须定义 AtomicFactsContextResponse',
  )
  for (const field of [
    'contractVersion', 'asOf', 'core', 'auxiliary', 'availability', 'recentChanges', 'dataQuality',
  ]) {
    assert.ok(new RegExp(`\\b${field}\\b`).test(src), `endpoints.ts 必须包含字段 ${field}`)
  }
  assert.ok(/coreDenominator/.test(src), 'availability 必须含 coreDenominator（固定 14）')
})

// ===== 双合同分离：frozen research contract vs presentation product contract =====

const PRESENTATION_PATH = join(BACKEND_ROOT, 'app', 'contracts', 'atomic_fact_presentation_v1.json')
const presentation = JSON.parse(readSource(PRESENTATION_PATH))

test('Presentation 合同：恰好 14 core + 8 auxiliary，排除 T3/T6/V1', () => {
  const core = presentation.facts.filter((f: { level: string }) => f.level === 'core')
  const aux = presentation.facts.filter((f: { level: string }) => f.level === 'auxiliary')
  assert.equal(core.length, 14, 'presentation core 必须 14 项')
  assert.equal(aux.length, 8, 'presentation auxiliary 必须 8 项（排除 T3/T6/V1）')
  const ids = presentation.facts.map((f: { id: string }) => f.id)
  for (const excluded of ['T3_trend_efficiency', 'T6_efficiency_delta', 'V1_cumulative_volume_ratio']) {
    assert.ok(!ids.includes(excluded), `presentation 不得包含 ${excluded}`)
  }
})

test('Frozen 研究合同不得混入产品层字段（public_key/public_label）', () => {
  const ALL_KEYS = new Set<string>()
  for (const f of [...contract.core_facts, ...contract.auxiliary_facts, ...contract.rejected_facts]) {
    for (const k of Object.keys(f)) ALL_KEYS.add(k)
  }
  for (const prodField of ['public_key', 'public_label', 'publicKey', 'publicLabel', 'visualKind', 'valuePrecision', 'secondaryLabel']) {
    assert.ok(!ALL_KEYS.has(prodField), `frozen contract 不得包含产品字段 ${prodField}`)
  }
})

// ===== 普通用户面板源码不得出现内部术语（DSA/SQZMOM/Segment/Active/Developing/factId/rawValue/sourcePath/bar/raw）=====

const PANEL_PATH = join(FRONTEND_ROOT, 'features', 'research-context', 'AtomicFactsPanel.tsx')
const DRAWER_PATH = join(FRONTEND_ROOT, 'features', 'research-context', 'AtomicFactsDrawer.tsx')
// 整词匹配（避免 Drawer/sidebar 等合法词误伤），区分大小写
const FORBIDDEN_TERMS = ['DSA', 'SQZMOM', 'Segment', 'Active', 'Developing', 'factId', 'rawValue', 'sourcePath', 'raw', 'bar']

test('普通用户面板源码不含内部术语', () => {
  for (const p of [PANEL_PATH, DRAWER_PATH]) {
    const src = readSource(p)
    for (const term of FORBIDDEN_TERMS) {
      const re = new RegExp(`\\b${term}\\b`)
      assert.ok(
        !re.test(src),
        `${p} 不得包含内部术语 "${term}"（普通用户 DOM 泄露）`,
      )
    }
  }
})

// ===== AFC V1 原子值 UI 改造契约（CHANGE-20260716-004）=====

test('visualKind 使用新枚举（metric/value_with_category/relation/position/distance/ratio）', () => {
  const src = readSource(ENDPOINTS_PATH)
  // 禁止旧枚举 value/category
  assert.ok(!/'value'/.test(src) || !/visualKind.*'value'/.test(src), 'endpoints visualKind 不得保留旧 value 枚举')
  assert.ok(!/'category'/.test(src) || !/visualKind.*'category'/.test(src), 'endpoints visualKind 不得保留旧 category 枚举')
  // 必须含新枚举
  for (const kind of ['metric', 'value_with_category', 'relation', 'position', 'distance', 'ratio']) {
    assert.ok(src.includes(kind), `endpoints visualKind 必须含 ${kind}`)
  }
})

test('valueText 可空（关系类事实为 null，仅 categoryLabel 承载）', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(/valueText:\s*string\s*\|\s*null/.test(src), 'valueText 必须为 string | null')
})

test('AtomicFactChange 含 label（前端禁止显示 publicKey）', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(/interface AtomicFactChange/.test(src), '必须定义 AtomicFactChange')
  assert.ok(/label:\s*string/.test(src), 'AtomicFactChange 必须含 label: string')
})

test('presentation visualKind 与 valuePrecision 对齐短原子值', () => {
  for (const f of presentation.facts) {
    assert.ok(
      ['metric', 'value_with_category', 'relation', 'position', 'distance', 'ratio'].includes(f.visualKind),
      `presentation ${f.id} visualKind 必须为新枚举，实际 ${f.visualKind}`,
    )
  }
  // T5/S3/S7/S8/V3 valuePrecision=2（1.23× / 0.63 / 1.34 ATR）
  const prec2 = ['T5_slope_ratio', 'S3_active_position', 'S7_dist_favorable_boundary', 'S8_dist_adverse_boundary', 'V3_avg_volume_ratio']
  for (const id of prec2) {
    const f = presentation.facts.find((x: { id: string }) => x.id === id)
    assert.ok(f, `presentation 必须含 ${id}`)
    assert.equal(f.valuePrecision, 2, `presentation ${id} valuePrecision 必须 2`)
  }
})

test('FactRow 按 visualKind 渲染（无重复 label/状态）', () => {
  const src = readSource(PANEL_PATH)
  // relation 只渲染 categoryLabel 一次（不得同时显 valueText）
  assert.ok(/visualKind === 'relation'/.test(src), 'FactRow 必须按 relation 分支渲染')
  // distance 渲染 badge + valueText 各一次
  assert.ok(/visualKind === 'distance'/.test(src), 'FactRow 必须按 distance 分支渲染')
  // ratio 的「分类未启用」由 secondaryText 承载（不在 valueText）
  assert.ok(!/分类未启用/.test(src) || /secondaryText/.test(src), 'ratio 未启用文案应由 secondaryText 承载')
})

test('ratio 未分类文案仅出现一次（secondaryText，非 valueText）', () => {
  const src = readSource(PANEL_PATH)
  // 不得在 FactRow 内硬编码「分类未启用」（由后端 secondaryText 提供）
  const matches = src.match(/分类未启用/g)
  assert.ok(matches === null || matches.length === 0, 'Panel 不得硬编码「分类未启用」（由后端 secondaryText 承载）')
})

test('distance 状态（尚未到达/已越过）仅出现一次（categoryLabel，非 valueText 解析）', () => {
  const src = readSource(PANEL_PATH)
  // 不得用 valueText.includes('已越过') 解析状态
  assert.ok(!/valueText\.includes/.test(src), '不得解析 valueText 推断状态（应用 categoryLabel）')
})

test('RecentChanges 显示中文 label，不显示 publicKey', () => {
  const src = readSource(PANEL_PATH)
  // changeLabel 使用 c.label
  assert.ok(/changeLabel/.test(src), 'RecentChanges 必须使用 changeLabel class')
  assert.ok(/\{c\.label\}/.test(src), 'RecentChanges 必须渲染 c.label（中文标签）')
  // 不得渲染 c.publicKey
  assert.ok(!/\{c\.publicKey\}/.test(src), 'RecentChanges 不得渲染 publicKey（内部键泄露）')
})

test('factRow 为 CSS Grid 透明行（非嵌套卡片背景）', () => {
  const scssPath = join(FRONTEND_ROOT, 'features', 'research-context', 'AtomicFactsPanel.module.scss')
  const src = readSource(scssPath)
  assert.ok(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto/.test(src), '.factRow 必须为 CSS Grid minmax(0,1fr) auto')
  assert.ok(/grid-template-areas/.test(src), '.factRow 必须定义 grid-template-areas')
  // 事实行背景透明
  const factRowBlock = src.match(/\.factRow\s*\{[^}]*\}/s)
  assert.ok(factRowBlock, '必须定义 .factRow')
  assert.ok(/background:\s*transparent/.test(factRowBlock[0]), '.factRow 背景必须透明')
})

test('S3 轨道含 低位/高位/0.33/0.67 刻度 + 圆点 + `数值 · 分类`', () => {
  const src = readSource(PANEL_PATH)
  assert.ok(/低位/.test(src), 'S3 轨道必须含「低位」')
  assert.ok(/高位/.test(src), 'S3 轨道必须含「高位」')
  assert.ok(/0\.33/.test(src), 'S3 轨道必须含 0.33 刻度')
  assert.ok(/0\.67/.test(src), 'S3 轨道必须含 0.67 刻度')
  assert.ok(/railKnob/.test(src), 'S3 轨道必须含当前位置圆点')
  assert.ok(/·\s*\$\{fact\.categoryLabel\}/.test(src) || /·\s*\$\{fact\.categoryLabel \?\?/.test(src), 'S3 必须显示 `数值 · 分类`')
})

test('Auxiliary 按 动量补充/结构补充/成交补充 分组', () => {
  const src = readSource(PANEL_PATH)
  assert.ok(/动量补充/.test(src), 'Auxiliary 必须含「动量补充」分组')
  assert.ok(/结构补充/.test(src), 'Auxiliary 必须含「结构补充」分组')
  assert.ok(/成交补充/.test(src), 'Auxiliary 必须含「成交补充」分组')
})

test('Drawer 焦点管理：打开聚焦关闭按钮、焦点 trap、关闭恢复焦点、body 滚动锁定', () => {
  const src = readSource(DRAWER_PATH)
  assert.ok(/closeBtnRef\.current\?\.focus/.test(src), 'Drawer 打开必须聚焦关闭按钮')
  assert.ok(/previouslyFocused/.test(src), 'Drawer 必须记录打开前焦点')
  assert.ok(/previouslyFocused\.current\?\.focus/.test(src), 'Drawer 关闭必须恢复焦点')
  assert.ok(/document\.body\.style\.overflow/.test(src), 'Drawer 必须 body 滚动锁定')
  assert.ok(/Escape/.test(src), 'Drawer 必须支持 Escape 关闭')
  assert.ok(/FOCUSABLE/.test(src) && /Tab/.test(src), 'Drawer 必须实现焦点 trap（Tab 限制）')
})

test('面板收起时不请求 context（enabled=false → 0 请求）', () => {
  const panelSrc = readSource(PANEL_PATH)
  // useStockContext 调用带 enabled 参数（由父组件控制挂载/卸载实现 0 请求）
  assert.ok(/useStockContext/.test(panelSrc), 'Panel 必须使用 useStockContext')
  assert.ok(/enabled/.test(panelSrc), 'useStockContext 必须支持 enabled 门控')
})

// ===== CHANGE-20260716-005：AFC V1 终审修正契约测试 =====

const SCSS_PATH = join(FRONTEND_ROOT, 'features', 'research-context', 'AtomicFactsPanel.module.scss')

test('factRow secondary 位于第二行右列（grid-template-areas ". secondary" + text-align right）', () => {
  const scss = readSource(SCSS_PATH)
  // grid-template-areas 必须为 "label value" ". secondary"（secondary 仅占右列）
  assert.ok(
    /"label value"\s*"\.\s*secondary"/.test(scss),
    '.factRow grid-template-areas 必须为 "label value" / ". secondary"（secondary 仅右列）',
  )
  // .factSecondary 必须右对齐
  const secBlock = scss.match(/\.factSecondary\s*\{[^}]*\}/s)
  assert.ok(secBlock, '必须定义 .factSecondary')
  assert.ok(/text-align:\s*right/.test(secBlock[0]), '.factSecondary 必须 text-align: right')
})

test('PositionRow 使用独立布局：第一行 label/caption，第二行轨道横跨整组宽度', () => {
  const panelSrc = readSource(PANEL_PATH)
  const scss = readSource(SCSS_PATH)
  // Panel 必须使用 positionRow class（非 factRow）
  assert.ok(/positionRow/.test(panelSrc), 'PositionRail 必须使用 positionRow class（独立布局）')
  // SCSS 必须定义 .positionRow 且 grid-template-areas 第二行 track track（横跨整组）
  const posBlock = scss.match(/\.positionRow\s*\{[^}]*\}/s)
  assert.ok(posBlock, '必须定义 .positionRow')
  assert.ok(
    /"label caption"\s*"track track"/.test(posBlock[0]),
    '.positionRow grid-template-areas 必须为 "label caption" / "track track"（轨道全宽）',
  )
  // 必须有 railScale（四刻度 低位/0.33/0.67/高位）和 railTrackWrap（预留刻度高度）
  assert.ok(/railScale/.test(panelSrc), 'PositionRail 必须含 railScale（四刻度）')
  assert.ok(/railTrackWrap/.test(panelSrc), 'PositionRail 必须含 railTrackWrap')
  const wrapBlock = scss.match(/\.railTrackWrap\s*\{[^}]*\}/s)
  assert.ok(wrapBlock, '必须定义 .railTrackWrap')
  assert.ok(/min-height/.test(wrapBlock[0]), '.railTrackWrap 必须预留 min-height 防止刻度与 caption 重叠')
  // railScale 必须用 space-between 均匀分布四刻度
  const scaleBlock = scss.match(/\.railScale\s*\{[^}]*\}/s)
  assert.ok(scaleBlock, '必须定义 .railScale')
  assert.ok(/justify-content:\s*space-between/.test(scaleBlock[0]), '.railScale 必须 space-between 均匀分布')
})

test('RecentChanges 显示 deltaText（变化类型文案）', () => {
  const src = readSource(PANEL_PATH)
  assert.ok(/c\.deltaText/.test(src), 'RecentChanges 必须渲染 c.deltaText')
  assert.ok(/changeDelta/.test(src), 'RecentChanges 必须使用 changeDelta class')
  // 不得渲染 publicKey（已有测试覆盖，此处再次断言确保 deltaText 改造未引入回退）
  assert.ok(!/\{c\.publicKey\}/.test(src), 'RecentChanges 不得渲染 publicKey')
})

test('Header 从 API meta 读取 researchFreezeVersion，禁止硬编码 V4.13', () => {
  const panelSrc = readSource(PANEL_PATH)
  const endpointsSrc = readSource(ENDPOINTS_PATH)
  // Panel 不得保留硬编码常量
  assert.ok(!/AFC_RESEARCH_VERSION/.test(panelSrc), 'Panel 不得硬编码 AFC_RESEARCH_VERSION 常量')
  assert.ok(!/V4\.13/.test(panelSrc), 'Panel 不得硬编码 V4.13 字面量')
  // 必须从 data.meta.researchFreezeVersion 读取
  assert.ok(
    /data\.meta\.researchFreezeVersion/.test(panelSrc),
    'Header 必须从 data.meta.researchFreezeVersion 读取研究版本',
  )
  // endpoints.ts 必须定义 AtomicFactsMeta 且 AtomicFactsContextResponse 含 meta 字段
  assert.ok(/export interface AtomicFactsMeta/.test(endpointsSrc), 'endpoints.ts 必须定义 AtomicFactsMeta')
  assert.ok(/meta:\s*AtomicFactsMeta/.test(endpointsSrc), 'AtomicFactsContextResponse 必须含 meta: AtomicFactsMeta')
})

test('Drawer 正向 Tab 也处理焦点离开 drawer 的情况（!drawer.contains(active) 双向）', () => {
  const src = readSource(DRAWER_PATH)
  // 非_shift 分支（正向 Tab）必须也包含 !drawer.contains(active) 条件
  // 提取 else 分支后断言
  const elseBranch = src.match(/else\s*\{[^}]*active === last[^}]*\}/s)
  assert.ok(elseBranch, 'Drawer 必须有正向 Tab 的 else 分支')
  assert.ok(
    /!drawer\.contains\(active\)/.test(elseBranch[0]),
    '正向 Tab 分支必须处理焦点离开 drawer（!drawer.contains(active)）',
  )
  // shift 分支也必须有（已有，但再次断言确保未回退）
  const shiftBranch = src.match(/if\s*\(e\.shiftKey\)[^{]*\{[^}]*!drawer\.contains\(active\)[^}]*\}/s)
  assert.ok(shiftBranch, 'Shift+Tab 分支必须处理焦点离开 drawer')
})
