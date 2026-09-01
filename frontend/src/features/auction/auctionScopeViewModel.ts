/**
 * Auction V3.2 scope list view-model (pure, no React, no network).
 *
 * This module is the SINGLE owner of the list data pipeline:
 *
 *   complete family snapshot -> search -> preset/filter -> sort -> paginate
 *
 * The order is a hard contract: sorting ALWAYS happens on the full filtered
 * set, before pagination.  Sorting a page would silently reorder only the
 * visible slice and is therefore forbidden.
 *
 * It never computes a business metric.  EW / AW / Capital Tilt / HHI /
 * Position / Velocity / Contribution / Migration all come from the API; this
 * module only formats-independent selects, orders and slices rows.
 */

export type SortDirection = "asc" | "desc";

export type AuctionSortField =
  | "equalWeightGap"
  | "amountWeightedGap"
  | "capitalTilt"
  | "positiveGapBreadth"
  | "ewPosition"
  | "ewVelocity"
  | "amountPosition"
  | "amountMultiple"
  | "amountAbnormalBreadth"
  | "repricingPosition"
  | "breadthPosition"
  | "participationPosition"
  | "amountHhi"
  | "top3AmountShare"
  | "leadershipMigration";

export interface SortRule {
  field: AuctionSortField;
  direction: SortDirection;
}

export type AuctionPresetId =
  | "strong"
  | "weak"
  | "amount"
  | "breadth"
  | "concentration"
  | "migration";

export interface AuctionPreset {
  id: AuctionPresetId;
  /** Chinese UI label. */
  label: string;
  /** Transparent, inspectable ordering rules — never a hidden score. */
  rules: SortRule[];
}

/**
 * The six quick filters.  Each is purely a list of sort rules so the user can
 * see exactly which fields drive the ordering.  No composite score exists.
 */
export const AUCTION_PRESETS: Record<AuctionPresetId, AuctionPreset> = {
  strong: {
    id: "strong",
    label: "强势异动",
    rules: [
      { field: "ewPosition", direction: "desc" },
      { field: "repricingPosition", direction: "desc" },
      { field: "amountPosition", direction: "desc" },
    ],
  },
  weak: {
    id: "weak",
    label: "弱势异动",
    rules: [
      { field: "ewPosition", direction: "asc" },
      { field: "repricingPosition", direction: "asc" },
      { field: "amountPosition", direction: "desc" },
    ],
  },
  amount: {
    id: "amount",
    label: "成交异动",
    rules: [
      { field: "amountPosition", direction: "desc" },
      { field: "amountAbnormalBreadth", direction: "desc" },
    ],
  },
  breadth: {
    id: "breadth",
    label: "扩散确认",
    rules: [
      { field: "positiveGapBreadth", direction: "desc" },
      { field: "breadthPosition", direction: "desc" },
      { field: "amountAbnormalBreadth", direction: "desc" },
    ],
  },
  concentration: {
    id: "concentration",
    label: "核心集中",
    rules: [
      { field: "amountHhi", direction: "desc" },
      { field: "top3AmountShare", direction: "desc" },
    ],
  },
  migration: {
    id: "migration",
    label: "龙头迁移",
    rules: [{ field: "leadershipMigration", direction: "desc" }],
  },
};

export const AUCTION_PRESET_IDS = Object.keys(AUCTION_PRESETS) as AuctionPresetId[];

/** One row of the complete family snapshot returned by GET /v1/auction/scopes. */
export interface AuctionScopeRow {
  scopeKey: string;
  scopeName: string | null;
  equalWeightGap: number | null;
  amountWeightedGap: number | null;
  capitalTilt: number | null;
  positiveGapBreadth: number | null;
  ewPosition: number | null;
  ewVelocity: number | null;
  amountPosition: number | null;
  amountMultiple: number | null;
  amountAbnormalBreadth: number | null;
  repricingPosition: number | null;
  breadthPosition: number | null;
  participationPosition: number | null;
  amountHhi: number | null;
  top3AmountShare: number | null;
  leadershipMigration: number | null;
}

export interface AuctionScopeViewQuery {
  search?: string;
  preset?: AuctionPresetId | null;
  sort?: AuctionSortField | null;
  direction?: SortDirection;
  page?: number;
  pageSize?: number;
}

export interface AuctionScopeViewResult<T extends AuctionScopeRow> {
  /** Rows for the requested page, in final display order. */
  pageRows: T[];
  /** Every row that survived search + preset (the full ordered set). */
  orderedRows: T[];
  totalCount: number;
  page: number;
  pageSize: number;
  pageCount: number;
  /** The rules actually applied — surfaced so the UI can show them. */
  appliedRules: SortRule[];
}

/**
 * Compare two nullable numbers with null ALWAYS last, in BOTH directions.
 *
 * Ascending example: 10, 20, 30, null  (never null, 10, 20, 30)
 */
export function compareNullableNumber(
  a: number | null | undefined,
  b: number | null | undefined,
  direction: SortDirection,
): number {
  const left = a ?? null;
  const right = b ?? null;
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return direction === "asc" ? left - right : right - left;
}

function matchesSearch(row: AuctionScopeRow, needle: string): boolean {
  if (!needle) return true;
  const target = needle.trim().toLowerCase();
  if (!target) return true;
  return `${row.scopeKey} ${row.scopeName ?? ""}`.toLowerCase().includes(target);
}

export function resolveSortRules(
  query: Pick<AuctionScopeViewQuery, "preset" | "sort" | "direction">,
): SortRule[] {
  // An explicit column sort wins over the preset: the user clicked a header.
  if (query.sort) {
    return [{ field: query.sort, direction: query.direction ?? "desc" }];
  }
  if (query.preset) {
    return AUCTION_PRESETS[query.preset]?.rules ?? [];
  }
  return [];
}

export function buildAuctionScopeView<T extends AuctionScopeRow>(
  /** The COMPLETE family snapshot — never a pre-truncated page. */
  rows: readonly T[],
  query: AuctionScopeViewQuery = {},
): AuctionScopeViewResult<T> {
  const pageSize = Math.max(1, query.pageSize ?? 50);

  // 1) search (over the complete snapshot)
  const searched = rows.filter((row) => matchesSearch(row, query.search ?? ""));

  // 2) preset / sort rules
  const rules = resolveSortRules(query);

  // 3) sort the WHOLE filtered set (never just the current page)
  const ordered = [...searched].sort((a, b) => {
    for (const rule of rules) {
      const delta = compareNullableNumber(a[rule.field], b[rule.field], rule.direction);
      if (delta !== 0) return delta;
    }
    // deterministic tie-break so pagination stays stable
    return a.scopeKey.localeCompare(b.scopeKey);
  });

  // 4) paginate last
  const totalCount = ordered.length;
  const pageCount = Math.max(1, Math.ceil(totalCount / pageSize));
  const page = Math.min(Math.max(1, query.page ?? 1), pageCount);
  const start = (page - 1) * pageSize;

  return {
    pageRows: ordered.slice(start, start + pageSize),
    orderedRows: ordered,
    totalCount,
    page,
    pageSize,
    pageCount,
    appliedRules: rules,
  };
}

/**
 * Pick the scope to select.  When the URL points at a scope that is not in the
 * current family, fall back to the first row of the current RESULT set and let
 * the caller sync the URL — never throw.
 */
export function resolveSelectedScope<T extends AuctionScopeRow>(
  orderedRows: readonly T[],
  requested: string | null | undefined,
): T | null {
  if (!orderedRows.length) return null;
  if (requested) {
    const hit = orderedRows.find((row) => row.scopeKey === requested);
    if (hit) return hit;
  }
  return orderedRows[0];
}

/** 0.023 -> "2.30%".  Formatting only; the source value is never re-derived. */
export function formatRatioPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

/** Generic unavailable marker: missing != zero. */
export function formatNumber(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(fractionDigits);
}
