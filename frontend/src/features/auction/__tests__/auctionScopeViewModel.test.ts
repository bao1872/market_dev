import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  toScopeRow,
  compareNullableNumber,
  buildAuctionScopeView,
  AUCTION_PRESETS,
  type AuctionScopeSortField,
} from '../auctionScopeViewModel'
import type { AuctionScopeListItemOut } from '../types'

function makeItem(over: Partial<AuctionScopeListItemOut>): AuctionScopeListItemOut {
  return {
    scope_key: over.scope_key ?? 'K',
    scope_name: over.scope_name ?? '板块',
    equal_weight_gap: over.equal_weight_gap ?? null,
    amount_weighted_gap: over.amount_weighted_gap ?? null,
    capital_tilt: over.capital_tilt ?? null,
    positive_gap_breadth: over.positive_gap_breadth ?? null,
    negative_gap_breadth: over.negative_gap_breadth ?? null,
    unchanged_gap_breadth: over.unchanged_gap_breadth ?? null,
    gap_dispersion: over.gap_dispersion ?? null,
    price_normalized_hhi: over.price_normalized_hhi ?? null,
    ew_position: over.ew_position ?? null,
    ew_velocity: over.ew_velocity ?? null,
    ew_acceleration: over.ew_acceleration ?? null,
    amount_historical_position: over.amount_historical_position ?? null,
    amount_multiple: over.amount_multiple ?? null,
    amount_abnormal_breadth: over.amount_abnormal_breadth ?? null,
    total_auction_amount: over.total_auction_amount ?? null,
    normalized_hhi: over.normalized_hhi ?? null,
    cross_sectional: over.cross_sectional ?? {
      repricing: null,
      breadth: null,
      participation: null,
      concentration: null,
    },
    leadership_migration: over.leadership_migration ?? null,
    price_valid_count: over.price_valid_count ?? null,
  }
}

test('toScopeRow maps snake_case DTO to camelCase row', () => {
  const row = toScopeRow(
    makeItem({
      scope_key: 'SW',
      equal_weight_gap: 0.023,
      ew_position: 82.5,
      cross_sectional: {
        repricing: 90,
        breadth: 40,
        participation: 60,
        concentration: null,
      },
    }),
  )
  assert.equal(row.scopeKey, 'SW')
  assert.equal(row.equalWeightGap, 0.023)
  assert.equal(row.ewPosition, 82.5)
  assert.equal(row.crossSectional.repricing, 90)
  assert.equal(row.crossSectional.concentration, null)
})

test('compareNullableNumber keeps null last in both directions', () => {
  assert.equal(compareNullableNumber(null, 1, 'desc'), 1)
  assert.equal(compareNullableNumber(1, null, 'desc'), -1)
  assert.equal(compareNullableNumber(null, null, 'desc'), 0)
  assert.equal(compareNullableNumber(2, 1, 'desc'), -1)
  assert.equal(compareNullableNumber(1, 2, 'asc'), -1)
  assert.equal(compareNullableNumber(null, 2, 'asc'), 1)
})

test('preset sorts by equalWeightGap desc with null last', () => {
  const rows = [
    toScopeRow(makeItem({ scope_key: 'A', scope_name: 'A', equal_weight_gap: 0.05 })),
    toScopeRow(makeItem({ scope_key: 'B', scope_name: 'B', equal_weight_gap: null })),
    toScopeRow(makeItem({ scope_key: 'C', scope_name: 'C', equal_weight_gap: 0.09 })),
  ]
  const view = buildAuctionScopeView(rows, { presetId: 'high-open', direction: 'desc' })
  assert.deepEqual(view.rows.map((r) => r.scopeKey), ['C', 'A', 'B'])
})

test('pagination slices the full family snapshot', () => {
  const rows = Array.from({ length: 5 }, (_, i) =>
    toScopeRow(makeItem({ scope_key: `S${i}`, scope_name: `S${i}`, ew_position: i })),
  )
  const p1 = buildAuctionScopeView(rows, { sort: 'ewPosition', direction: 'asc', page: 1, pageSize: 2 })
  const p2 = buildAuctionScopeView(rows, { sort: 'ewPosition', direction: 'asc', page: 2, pageSize: 2 })
  assert.equal(p1.rows.length, 2)
  assert.equal(p1.rows[0].scopeKey, 'S0')
  assert.equal(p2.rows[0].scopeKey, 'S2')
  assert.equal(p1.pageCount, 3)
})

test('search filters by scopeName case-insensitively', () => {
  const rows = [
    toScopeRow(makeItem({ scope_key: 'X', scope_name: '半导体' })),
    toScopeRow(makeItem({ scope_key: 'Y', scope_name: '锂电池' })),
  ]
  const view = buildAuctionScopeView(rows, { search: '电池' })
  assert.equal(view.total, 1)
  assert.equal(view.rows[0].scopeKey, 'Y')
})

test('AUCTION_PRESETS are 6 and reference real sort fields', () => {
  assert.equal(AUCTION_PRESETS.length, 6)
  const valid: AuctionScopeSortField[] = [
    'scopeName',
    'equalWeightGap',
    'amountWeightedGap',
    'capitalTilt',
    'positiveGapBreadth',
    'negativeGapBreadth',
    'unchangedGapBreadth',
    'gapDispersion',
    'priceNormalizedHhi',
    'ewPosition',
    'ewVelocity',
    'ewAcceleration',
    'amountHistoricalPosition',
    'amountMultiple',
    'amountAbnormalBreadth',
    'totalAuctionAmount',
    'normalizedHhi',
    'leadershipMigration',
    'priceValidCount',
    'crossRepricing',
    'crossBreadth',
    'crossParticipation',
    'crossConcentration',
  ]
  for (const p of AUCTION_PRESETS) {
    assert.ok(valid.includes(p.sort), `preset ${p.id} sort ${p.sort} is a real field`)
    assert.ok(valid.includes(p.secondary), `preset ${p.id} secondary ${p.secondary} is a real field`)
  }
})
