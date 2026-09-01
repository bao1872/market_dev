/**
 * Contract tests for the Auction V3.2 list view-model.
 *
 * These pin BEHAVIOUR, not implementation details: no source-string matching,
 * no assertions on internal function names or CSS.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AUCTION_PRESETS,
  buildAuctionScopeView,
  compareNullableNumber,
  formatNumber,
  formatRatioPercent,
  resolveSelectedScope,
  resolveSortRules,
  type AuctionScopeRow,
} from "../auctionScopeViewModel";

function row(over: Partial<AuctionScopeRow> & { scopeKey: string }): AuctionScopeRow {
  return {
    scopeName: over.scopeKey,
    equalWeightGap: null,
    amountWeightedGap: null,
    capitalTilt: null,
    positiveGapBreadth: null,
    ewPosition: null,
    ewVelocity: null,
    amountPosition: null,
    amountMultiple: null,
    amountAbnormalBreadth: null,
    repricingPosition: null,
    breadthPosition: null,
    participationPosition: null,
    amountHhi: null,
    top3AmountShare: null,
    leadershipMigration: null,
    ...over,
  };
}

// ---------------------------------------------------------------------------
// ordering primitives
// ---------------------------------------------------------------------------
test("null is last in ASC", () => {
  assert.equal(compareNullableNumber(null, 10, "asc") > 0, true);
  assert.equal(compareNullableNumber(10, null, "asc") < 0, true);
});

test("null is last in DESC too", () => {
  assert.equal(compareNullableNumber(null, 10, "desc") > 0, true);
  assert.equal(compareNullableNumber(10, null, "desc") < 0, true);
});

test("two nulls compare equal", () => {
  assert.equal(compareNullableNumber(null, null, "desc"), 0);
});

// ---------------------------------------------------------------------------
// complete snapshot + search
// ---------------------------------------------------------------------------
test("complete family snapshot is preserved (25 rows in, 25 out)", () => {
  const rows = Array.from({ length: 25 }, (_, i) => row({ scopeKey: `IND_${i}` }));
  const view = buildAuctionScopeView(rows, { pageSize: 10 });
  assert.equal(view.totalCount, 25);
  assert.equal(view.pageCount, 3);
  assert.equal(view.pageRows.length, 10);
});

test("search filters over the whole snapshot, not the page", () => {
  const rows = [
    row({ scopeKey: "IND_BANK" }),
    row({ scopeKey: "IND_OIL" }),
    row({ scopeKey: "CPT_ROBOT" }),
  ];
  const view = buildAuctionScopeView(rows, { search: "IND" });
  assert.deepEqual(
    view.orderedRows.map((r) => r.scopeKey).sort(),
    ["IND_BANK", "IND_OIL"],
  );
});

test("search is case-insensitive", () => {
  const view = buildAuctionScopeView([row({ scopeKey: "IND_BANK" })], { search: "bank" });
  assert.equal(view.totalCount, 1);
});

// ---------------------------------------------------------------------------
// single-column sorting with null-last
// ---------------------------------------------------------------------------
test("single column DESC sorts values and keeps null last", () => {
  const rows = [
    row({ scopeKey: "A", ewPosition: 10 }),
    row({ scopeKey: "B", ewPosition: null }),
    row({ scopeKey: "C", ewPosition: 30 }),
    row({ scopeKey: "D", ewPosition: 20 }),
  ];
  const view = buildAuctionScopeView(rows, {
    sort: "ewPosition",
    direction: "desc",
  });
  assert.deepEqual(view.orderedRows.map((r) => r.ewPosition), [30, 20, 10, null]);
});

test("single column ASC also keeps null last", () => {
  const rows = [
    row({ scopeKey: "A", ewPosition: 10 }),
    row({ scopeKey: "B", ewPosition: null }),
    row({ scopeKey: "C", ewPosition: 30 }),
    row({ scopeKey: "D", ewPosition: 20 }),
  ];
  const view = buildAuctionScopeView(rows, {
    sort: "ewPosition",
    direction: "asc",
  });
  assert.deepEqual(view.orderedRows.map((r) => r.ewPosition), [10, 20, 30, null]);
});

// ---------------------------------------------------------------------------
// the six presets
// ---------------------------------------------------------------------------
test("strong preset orders by ewPosition desc then repricing desc", () => {
  const rows = [
    row({ scopeKey: "A", ewPosition: 50, repricingPosition: 90 }),
    row({ scopeKey: "B", ewPosition: 50, repricingPosition: 10 }),
    row({ scopeKey: "C", ewPosition: 90, repricingPosition: 10 }),
  ];
  const view = buildAuctionScopeView(rows, { preset: "strong" });
  assert.deepEqual(view.orderedRows.map((r) => r.scopeKey), ["C", "A", "B"]);
});

test("weak preset orders by ewPosition asc", () => {
  const rows = [
    row({ scopeKey: "A", ewPosition: 50, repricingPosition: 10 }),
    row({ scopeKey: "B", ewPosition: 5, repricingPosition: 90 }),
    row({ scopeKey: "C", ewPosition: 95, repricingPosition: 90 }),
  ];
  const view = buildAuctionScopeView(rows, { preset: "weak" });
  assert.deepEqual(view.orderedRows.map((r) => r.scopeKey), ["B", "A", "C"]);
});

test("amount preset orders by amountPosition desc then abnormal breadth desc", () => {
  const rows = [
    row({ scopeKey: "A", amountPosition: 50, amountAbnormalBreadth: 0.9 }),
    row({ scopeKey: "B", amountPosition: 50, amountAbnormalBreadth: 0.1 }),
    row({ scopeKey: "C", amountPosition: 80, amountAbnormalBreadth: 0.1 }),
  ];
  const view = buildAuctionScopeView(rows, { preset: "amount" });
  assert.deepEqual(view.orderedRows.map((r) => r.scopeKey), ["C", "A", "B"]);
});

test("breadth preset orders by advance ratio desc then breadth position desc", () => {
  const rows = [
    row({ scopeKey: "A", positiveGapBreadth: 0.5, breadthPosition: 90 }),
    row({ scopeKey: "B", positiveGapBreadth: 0.5, breadthPosition: 10 }),
    row({ scopeKey: "C", positiveGapBreadth: 0.9, breadthPosition: 10 }),
  ];
  const view = buildAuctionScopeView(rows, { preset: "breadth" });
  assert.deepEqual(view.orderedRows.map((r) => r.scopeKey), ["C", "A", "B"]);
});

test("concentration preset orders by amount HHI desc then top3 share desc", () => {
  const rows = [
    row({ scopeKey: "A", amountHhi: 0.4, top3AmountShare: 0.9 }),
    row({ scopeKey: "B", amountHhi: 0.4, top3AmountShare: 0.2 }),
    row({ scopeKey: "C", amountHhi: 0.8, top3AmountShare: 0.2 }),
  ];
  const view = buildAuctionScopeView(rows, { preset: "concentration" });
  assert.deepEqual(view.orderedRows.map((r) => r.scopeKey), ["C", "A", "B"]);
});

test("migration preset orders by leadership migration desc", () => {
  const rows = [
    row({ scopeKey: "A", leadershipMigration: 0.2 }),
    row({ scopeKey: "B", leadershipMigration: 0.9 }),
  ];
  const view = buildAuctionScopeView(rows, { preset: "migration" });
  assert.deepEqual(view.orderedRows.map((r) => r.scopeKey), ["B", "A"]);
});

test("presets are transparent sort rules, never a score", () => {
  for (const preset of Object.values(AUCTION_PRESETS)) {
    assert.ok(preset.rules.length > 0, `${preset.id} must expose rules`);
    for (const rule of preset.rules) {
      assert.ok(rule.field);
      assert.ok(rule.direction === "asc" || rule.direction === "desc");
    }
    // no score field is produced anywhere
    assert.equal("score" in preset, false);
    assert.equal("rankScore" in preset, false);
  }
});

test("explicit column sort overrides the preset", () => {
  const rules = resolveSortRules({ preset: "strong", sort: "amountHhi", direction: "asc" });
  assert.deepEqual(rules, [{ field: "amountHhi", direction: "asc" }]);
});

// ---------------------------------------------------------------------------
// pipeline order: filter -> sort -> paginate
// ---------------------------------------------------------------------------
test("sorting happens before pagination across the whole set", () => {
  // values arranged so that per-page sorting would give a different answer
  const rows = Array.from({ length: 6 }, (_, i) =>
    row({ scopeKey: `S${i}`, ewPosition: [5, 1, 4, 2, 6, 3][i] }),
  );
  const view = buildAuctionScopeView(rows, {
    sort: "ewPosition",
    direction: "desc",
    pageSize: 3,
    page: 1,
  });
  // page 1 must contain the three highest OVERALL, not the highest of slice 1
  assert.deepEqual(view.pageRows.map((r) => r.ewPosition), [6, 5, 4]);
  const second = buildAuctionScopeView(rows, {
    sort: "ewPosition",
    direction: "desc",
    pageSize: 3,
    page: 2,
  });
  assert.deepEqual(second.pageRows.map((r) => r.ewPosition), [3, 2, 1]);
});

test("out-of-range page is clamped, never empty", () => {
  const rows = Array.from({ length: 5 }, (_, i) => row({ scopeKey: `S${i}` }));
  const view = buildAuctionScopeView(rows, { pageSize: 2, page: 99 });
  assert.equal(view.page, 3);
  assert.ok(view.pageRows.length > 0);
});

// ---------------------------------------------------------------------------
// selection + unavailable
// ---------------------------------------------------------------------------
test("unknown scope falls back to the first result row", () => {
  const rows = [row({ scopeKey: "A" }), row({ scopeKey: "B" })];
  const view = buildAuctionScopeView(rows);
  const selected = resolveSelectedScope(view.orderedRows, "DOES_NOT_EXIST");
  assert.equal(selected?.scopeKey, "A");
});

test("empty result set yields null selection without throwing", () => {
  assert.equal(resolveSelectedScope([], "ANY"), null);
});

test("known scope is selected as-is", () => {
  const rows = [row({ scopeKey: "A" }), row({ scopeKey: "B" })];
  const view = buildAuctionScopeView(rows);
  assert.equal(resolveSelectedScope(view.orderedRows, "B")?.scopeKey, "B");
});

test("unavailable renders as a dash, never zero", () => {
  assert.equal(formatNumber(null), "—");
  assert.equal(formatRatioPercent(null), "—");
  assert.notEqual(formatNumber(null), "0");
  assert.notEqual(formatRatioPercent(null), "0%");
});

// ---------------------------------------------------------------------------
// formatting only — never re-derives business values
// ---------------------------------------------------------------------------
test("ratio formatting: 0.023 renders as 2.30%", () => {
  assert.equal(formatRatioPercent(0.023), "2.30%");
});

test("a genuine zero is displayed, not hidden", () => {
  assert.equal(formatRatioPercent(0), "0.00%");
  assert.equal(formatNumber(0), "0.00");
});
