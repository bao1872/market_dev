// [Slice C] Scope Explorer 排序契约：全量 sortable key 的 asc/desc、null-last、
// deterministic tie-break、URL parse/build/toggle 与旧值兼容、以及
// filter → sort(full family) → paginate 管线顺序。
//
// 表驱动：所有 sortable key 由 REVIEW_SORT_KEYS 统一遍历，
// 不为每个 key 复制粘贴一份测试；新增 key 自动被覆盖。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import {
  REVIEW_SORT_KEYS,
  parseReviewSort,
  buildReviewSort,
  reviewSortToggle,
  normalizeSort,
  DEFAULT_REVIEW_SORT,
  type ReviewSort,
  type ReviewSortKey,
} from '../urlState'
import {
  sortValueFor,
  sortScopes,
  filterScopes,
  buildScopeExplorerQuery,
  applyScopeExplorerPipeline,
  technicalTop5Ratio,
} from '../scopeExplorerViewModel'
import { formatPercentNullable, NULL_DISPLAY } from '../reviewFormat'
import type {
  ReviewScopeListItem,
  ReviewScopeSummary,
  ReviewScopeObservationSummary,
  ReviewDynamicsPhase,
} from '../types'

const PHASES: ReviewDynamicsPhase[] = [
  'Early Lift',
  'Strengthening',
  'Sustained',
  'Decelerating',
  'Weakening',
  'Repairing',
]

function emptySummary(): ReviewScopeSummary {
  return {
    dynamicsStatus: 'ready', phase: null, position: null, velocity: null,
    acceleration: null, upperOccupancy: null, lowerOccupancy: null,
    equalWeightReturn: null, amountWeightedReturn: null, capitalTilt: null,
    advanceRatio: null, declineRatio: null, unchangedRatio: null,
    returnDispersion: null, priceNormalizedHhi: null, amountNormalizedHhi: null,
    leadershipStatus: null, jaccardStability: null, migration: null,
  }
}

function emptyObs(): ReviewScopeObservationSummary {
  return {
    freshnessTodayCount: null, freshnessDecayWeightedDensity: null,
    technicalHhi: null, technicalTop5Numerator: null,
    technicalTop5Denominator: null, technicalLeaderMedianGap: null,
    technicalLeaderSymbol: null, technicalMemberCount: null,
  }
}

/** 构造「只有 key 对应字段有值」的 item，用于表驱动排序验证 */
/** [SLICE 5 / Explorer] 空 compareFacts（10 个 visible 排序字段的注入载体） */
function emptyCompare(): NonNullable<ReviewScopeListItem['compareFacts']> {
  return {
    dsa: { regimeStrength: null, regimeStrengthPeerPercentile: null, durationBars: null, vwapDevPct: null },
    smc: { eventType: null, structureLevel: null, direction: null, memberRatio: null, availability: 'ready', reason: null },
    momentum: { enhancingRatio: null, weakeningRatio: null, denominator: null },
    volume: { ratio20: null },
    price: { equalWeightReturn: null, equalWeightReturnPeerPercentile: null, advanceRatio: null },
    composition: { capitalTilt: null, migration: null },
  }
}

/**
 * 构造「只有 key 对应字段有值」的 item，用于表驱动排序验证。
 *
 * [SLICE 5 / Explorer] 10 个 visible compare 键注入到 compareFacts
 * （与 sortValueFor / ScopeExplorerTable 的读取 owner 一致）；
 * legacy 键继续注入 summary / observationSummary。
 */
function itemWith(key: ReviewSortKey, value: number | null, scopeKey = 'k'): ReviewScopeListItem {
  const s = emptySummary()
  const o = emptyObs()
  const c = emptyCompare()
  const item: ReviewScopeListItem = {
    scopeType: 'industry_l1', scopeKey, scopeName: `N-${scopeKey}`,
    readiness: 'ready', status: 'ready', eligibleCount: 10, providedCount: 10,
    coverageRatio: null, summary: s, observationSummary: o, compareFacts: c,
  }
  switch (key) {
    // ---- visible compare keys：注入 compareFacts ----
    case 'dsa_strength': if (c.dsa) c.dsa.regimeStrength = value; break
    case 'dsa_duration': if (c.dsa) c.dsa.durationBars = value; break
    case 'dsa_vwap_dev': if (c.dsa) c.dsa.vwapDevPct = value; break
    case 'smc_member_ratio':
      if (c.smc) {
        c.smc.memberRatio = value
        c.smc.availability = value === null ? 'ready' : 'ready'
        c.smc.eventType = value === null ? null : 'BOS'
      }
      break
    case 'momentum_enhancing': if (c.momentum) c.momentum.enhancingRatio = value; break
    case 'volume_ratio20': if (c.volume) c.volume.ratio20 = value; break
    case 'equal_weight_return': if (c.price) c.price.equalWeightReturn = value; break
    case 'advance_ratio': if (c.price) c.price.advanceRatio = value; break
    case 'capital_tilt': if (c.composition) c.composition.capitalTilt = value; break
    case 'migration': if (c.composition) c.composition.migration = value; break
    // ---- legacy keys ----
    case 'position': s.position = value; break
    case 'velocity': s.velocity = value; break
    case 'acceleration': s.acceleration = value; break
    case 'phase': s.phase = value === null ? null : PHASES[value]; break
    // equal_weight_return / capital_tilt / advance_ratio / migration 已上移到
    // visible compare 分支（compareFacts），此处不再注入 summary。
    case 'decline_ratio': s.declineRatio = value; break
    case 'unchanged_ratio': s.unchangedRatio = value; break
    case 'coverage': item.coverageRatio = value; break
    case 'freshness_density': o.freshnessDecayWeightedDensity = value; break
    case 'freshness_today': o.freshnessTodayCount = value; break
    case 'technical_hhi': o.technicalHhi = value; break
    case 'technical_top5_ratio':
      o.technicalTop5Numerator = value === null ? null : value * 100
      o.technicalTop5Denominator = 100
      break
    case 'leader_median_gap': o.technicalLeaderMedianGap = value; break
    default: break
  }
  return item
}

// ============================================================
// 1. 表驱动：每个 sortable key 的 desc / asc / null-last / tie
// ============================================================

for (const key of REVIEW_SORT_KEYS) {
  test(`sort[${key}] desc / asc / null-last / deterministic tie`, () => {
    const hi = itemWith(key, 3, 'a')
    const lo = itemWith(key, 1, 'b')
    const nil = itemWith(key, null, 'n')
    const input = [lo, nil, hi]

    const desc = sortScopes(input, buildReviewSort(key, 'desc'))
    assert.deepEqual(desc.map((i) => i.scopeKey), ['a', 'b', 'n'], `${key}: desc 且 null 最后`)

    const asc = sortScopes(input, buildReviewSort(key, 'asc'))
    assert.deepEqual(asc.map((i) => i.scopeKey), ['b', 'a', 'n'], `${key}: asc 且 null 最后`)

    const allNull = sortScopes(
      [itemWith(key, null, 'z'), itemWith(key, null, 'a')],
      buildReviewSort(key, 'desc'),
    )
    assert.deepEqual(allNull.map((i) => i.scopeKey), ['a', 'z'], `${key}: 全 null 仍确定性`)

    const tZ: ReviewScopeListItem = { ...itemWith(key, 5, 'z'), scopeName: 'Zeta' }
    const tA: ReviewScopeListItem = { ...itemWith(key, 5, 'a'), scopeName: 'Alpha' }
    const tie = sortScopes([tZ, tA], buildReviewSort(key, 'desc'))
    assert.deepEqual(tie.map((i) => i.scopeKey), ['a', 'z'], `${key}: tie 确定性`)
  })
}

// ============================================================
// 2. sortValueFor 直接取值（persisted 字段，绝不重算）
// ============================================================

test('sortValueFor: 每个 key 都能取出注入值', () => {
  for (const key of REVIEW_SORT_KEYS) {
    if (key === 'phase') continue // phase 取 canonical 顺序索引
    assert.equal(sortValueFor(itemWith(key, 0.42), key), 0.42, `${key} 取值`)
    assert.equal(sortValueFor(itemWith(key, null), key), null, `${key} null`)
  }
  assert.equal(sortValueFor(itemWith('phase', 2), 'phase'), 2)
  assert.equal(sortValueFor(itemWith('phase', null), 'phase'), null)
})

// ============================================================
// 3. Top5 ratio：唯一 ViewModel owner（显示与排序共用）
// ============================================================

test('technicalTop5Ratio: denominator>0 → ratio；否则 null（绝不 0/1 冒充）', () => {
  assert.equal(technicalTop5Ratio({ ...emptyObs(), technicalTop5Numerator: 25, technicalTop5Denominator: 100 }), 0.25)
  // 真实 0 是有效值
  assert.equal(technicalTop5Ratio({ ...emptyObs(), technicalTop5Numerator: 0, technicalTop5Denominator: 100 }), 0)
  assert.equal(technicalTop5Ratio({ ...emptyObs(), technicalTop5Numerator: 25, technicalTop5Denominator: 0 }), null)
  assert.equal(technicalTop5Ratio({ ...emptyObs(), technicalTop5Numerator: null, technicalTop5Denominator: 100 }), null)
  assert.equal(technicalTop5Ratio({ ...emptyObs(), technicalTop5Numerator: 25, technicalTop5Denominator: null }), null)
  assert.equal(technicalTop5Ratio(null), null)
})

test('Top5 显示与排序共用同一 owner（不是两套算法）', () => {
  const obs: ReviewScopeObservationSummary = {
    ...emptyObs(), technicalTop5Numerator: 30, technicalTop5Denominator: 100,
  }
  const item: ReviewScopeListItem = { ...itemWith('technical_hhi', null, 'k'), observationSummary: obs }
  assert.equal(sortValueFor(item, 'technical_top5_ratio'), 0.3, '排序取值 = ratio')
  assert.equal(formatPercentNullable(technicalTop5Ratio(obs), 2), '30.00%', '显示 = 同一 ratio')
  assert.equal(formatPercentNullable(technicalTop5Ratio(emptyObs()), 2), NULL_DISPLAY)
})

// ============================================================
// 4. URL：parse / build / toggle / 旧值兼容 / 无半状态
// ============================================================

test('URL: 每个 key 的 asc 与 desc 都可 build→parse 往返', () => {
  for (const key of REVIEW_SORT_KEYS) {
    for (const dir of ['asc', 'desc'] as const) {
      const s = buildReviewSort(key, dir)
      assert.deepEqual(parseReviewSort(s), { key, dir }, `${s} 解析`)
      assert.equal(normalizeSort(s), s, `${s} 必须是合法 URL 值`)
    }
  }
})

test('URL: 不存在「只有 _desc」的半状态（每个 key 都有 asc）', () => {
  for (const key of REVIEW_SORT_KEYS) {
    assert.equal(normalizeSort(`${key}_desc`), `${key}_desc`, `${key}_desc 合法`)
    assert.equal(normalizeSort(`${key}_asc`), `${key}_asc`, `${key}_asc 合法`)
  }
})

test('URL: toggle desc→asc→desc，换列回到该列 desc', () => {
  for (const key of REVIEW_SORT_KEYS) {
    const d = buildReviewSort(key, 'desc')
    const a = buildReviewSort(key, 'asc')
    assert.equal(reviewSortToggle(key, d), a, `${key} desc→asc`)
    assert.equal(reviewSortToggle(key, a), d, `${key} asc→desc`)
    const other: ReviewSortKey = key === 'velocity' ? 'position' : 'velocity'
    assert.equal(reviewSortToggle(other, d), buildReviewSort(other, 'desc'), '换列 → 该列 desc')
  }
})

test('URL: 旧 sort 值向后兼容（已有分享链接不得失效）', () => {
  const legacy: ReviewSort[] = [
    'velocity_desc', 'velocity_asc', 'acceleration_desc', 'acceleration_asc',
    'position_desc', 'position_asc', 'phase_desc', 'phase_asc',
    'equal_weight_return_desc', 'capital_tilt_desc', 'migration_desc', 'coverage_desc',
    'freshness_density_desc', 'freshness_today_desc', 'technical_hhi_desc', 'leader_median_gap_desc',
  ]
  for (const s of legacy) {
    assert.equal(normalizeSort(s), s, `${s} 必须仍可解析`)
    assert.ok(parseReviewSort(s).key !== null, `${s} 必须解析出 key`)
  }
})

test('URL: 非法 sort 回退默认；含下划线 key 不得被切错', () => {
  // [SLICE 5 / Explorer] 默认排序改为 dsa_strength_desc（不再是不可见的 Velocity）。
  // legacy URL velocity_desc 仍必须可解析（见上方 legacy 兼容测试）。
  assert.equal(normalizeSort('bogus_sort_value'), DEFAULT_REVIEW_SORT)
  assert.equal(normalizeSort(''), DEFAULT_REVIEW_SORT)
  assert.equal(normalizeSort(null), DEFAULT_REVIEW_SORT)
  assert.equal(DEFAULT_REVIEW_SORT, 'dsa_strength_desc')
  assert.deepEqual(parseReviewSort('equal_weight_return_desc'), { key: 'equal_weight_return', dir: 'desc' })
})

test('URL: REVIEW_SORT_KEYS 无重复且与合法集合无漂移', () => {
  // 16 legacy + 6 个新 visible compare key
  assert.equal(REVIEW_SORT_KEYS.length, 22, 'sortable key 数量')
  assert.equal(new Set(REVIEW_SORT_KEYS).size, REVIEW_SORT_KEYS.length, 'key 不得重复')
})

// ============================================================
// 5. Pipeline：filter → sort(完整 filtered family) → paginate
// ============================================================

test('pipeline: 排序作用于完整 filtered 集合，再分页（不是按页排序）', () => {
  const items: ReviewScopeListItem[] = Array.from({ length: 12 }, (_, i) => {
    const it = itemWith('velocity', i, `k${i}`)
    return { ...it, scopeName: `N-${String(i).padStart(2, '0')}` }
  })
  const query = buildScopeExplorerQuery('N-', null)
  assert.equal(filterScopes(items, query).length, 12, 'q="N-" 命中全部')

  const page2 = applyScopeExplorerPipeline(items, query, 2, 5, 'velocity_desc')
  assert.deepEqual(page2.items.map((i) => i.scopeKey), ['k6', 'k5', 'k4', 'k3', 'k2'],
    '整组降序后再取第 2 页')
  const page1 = applyScopeExplorerPipeline(items, query, 1, 5, 'velocity_desc')
  assert.deepEqual(page1.items.map((i) => i.scopeKey), ['k11', 'k10', 'k9', 'k8', 'k7'])
})

test('pipeline: q 过滤后再排序（过滤生效且排序不被破坏）', () => {
  const items: ReviewScopeListItem[] = [
    { ...itemWith('velocity', 9, 'hit-a'), scopeName: 'Alpha' },
    { ...itemWith('velocity', 1, 'hit-b'), scopeName: 'Beta' },
    { ...itemWith('velocity', 99, 'miss'), scopeName: 'Gamma' },
  ]
  const out = applyScopeExplorerPipeline(items, buildScopeExplorerQuery('Alph', null), 1, 10, 'velocity_desc')
  assert.deepEqual(out.items.map((i) => i.scopeKey), ['hit-a'], '只保留命中项')
  assert.equal(out.total, 1)
})

test('pipeline: null 在整组排序中恒最后（asc 亦成立）', () => {
  const items = [itemWith('velocity', null, 'nil'), itemWith('velocity', 5, 'a'), itemWith('velocity', 3, 'b')]
  assert.deepEqual(sortScopes(items, 'velocity_desc').map((i) => i.scopeKey), ['a', 'b', 'nil'])
  assert.deepEqual(sortScopes(items, 'velocity_asc').map((i) => i.scopeKey), ['b', 'a', 'nil'])
})
