// GENERATED from frontend/src/components/zigzag.ts; use tools/build_strategy_zigzag_bridge.mjs
"use strict";
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// frontend/src/components/zigzag.ts
var zigzag_exports = {};
__export(zigzag_exports, {
  ZIGZAG_FIXED: () => ZIGZAG_FIXED,
  computeZigZag: () => computeZigZag
});
module.exports = __toCommonJS(zigzag_exports);
var ZIGZAG_FIXED = Object.freeze({
  deviationPercent: 5,
  pivotLegs: 10,
  leftRight: 5,
  lineColor: "#2962FF",
  projectionPivots: true,
  displayReversalPrice: true,
  displayCumulativeVolume: true,
  displayReversalPriceChange: true,
  differencePriceMode: "Absolute",
  draw: true,
  allowZigZagOnOneBar: true
});
var HALF = ZIGZAG_FIXED.leftRight;
var THRESHOLD = ZIGZAG_FIXED.deviationPercent;
function validBar(bar) {
  return Number.isFinite(bar.high) && Number.isFinite(bar.low) && Number.isFinite(bar.volume) && bar.high >= bar.low && bar.low > 0 && bar.volume >= 0;
}
function pivotAt(bars, index, kind, right) {
  if (index < HALF || index + right >= bars.length) return false;
  const value = kind === "high" ? bars[index].high : bars[index].low;
  for (let d = 1; d <= HALF; d++) {
    const older = kind === "high" ? bars[index - d].high : bars[index - d].low;
    if (kind === "high" ? older >= value : older <= value) return false;
  }
  for (let d = 1; d <= right; d++) {
    const newer = kind === "high" ? bars[index + d].high : bars[index + d].low;
    if (kind === "high" ? newer > value : newer < value) return false;
  }
  return true;
}
function deviation(base, candidate) {
  return Math.abs((candidate - base) / base) * 100;
}
function addOrUpdate(pivots, candidate) {
  const last = pivots[pivots.length - 1];
  if (!last) {
    pivots.push(candidate);
  } else if (last.kind === candidate.kind) {
    const moreExtreme = candidate.kind === "high" ? candidate.price > last.price : candidate.price < last.price;
    if (moreExtreme) pivots[pivots.length - 1] = candidate;
  } else if (deviation(last.price, candidate.price) >= THRESHOLD) {
    pivots.push(candidate);
  }
}
function volumePrefix(bars) {
  const prefix = new Float64Array(bars.length + 1);
  for (let i = 0; i < bars.length; i++) prefix[i + 1] = prefix[i] + bars[i].volume;
  return prefix;
}
function makeSegment(from, to, prefix) {
  const volume = prefix[to.index + 1] - prefix[from.index + 1];
  return { from, to, volume: Math.max(0, volume), priceChange: to.price - from.price };
}
function invalidateLast(bars, pivots) {
  while (pivots.length > 1) {
    const last = pivots[pivots.length - 1];
    let invalid = false;
    for (let i = last.index + 1; i < bars.length; i++) {
      const bar = bars[i];
      const reversed = last.kind === "high" ? bar.low <= last.price * (1 - THRESHOLD / 100) : bar.high >= last.price * (1 + THRESHOLD / 100);
      if (reversed) break;
      if (last.kind === "high" ? bar.high > last.price : bar.low < last.price) {
        invalid = true;
        break;
      }
    }
    if (!invalid) return;
    pivots.pop();
  }
}
function findProjection(bars, last) {
  const kind = last.kind === "high" ? "low" : "high";
  let candidate = null;
  const from = Math.max(last.index + 1, HALF, bars.length - HALF);
  for (let i = from; i < bars.length; i++) {
    const partialRight = bars.length - 1 - i;
    if (!pivotAt(bars, i, kind, partialRight)) continue;
    const price = kind === "high" ? bars[i].high : bars[i].low;
    if (deviation(last.price, price) < THRESHOLD) continue;
    if (!candidate || (kind === "high" ? price > candidate.price : price < candidate.price)) {
      candidate = { index: i, price, kind, confirmedAt: -1 };
    }
  }
  return candidate;
}
function computeZigZag(bars) {
  if (bars.length < HALF * 2 + 1 || bars.some((bar) => !validBar(bar))) {
    return { pivots: [], segments: [], projected: null };
  }
  const prefix = volumePrefix(bars);
  const pivots = [];
  for (let confirmedAt = HALF * 2; confirmedAt < bars.length; confirmedAt++) {
    const i = confirmedAt - HALF;
    if (pivotAt(bars, i, "high", HALF)) {
      addOrUpdate(pivots, { index: i, price: bars[i].high, kind: "high", confirmedAt });
    }
    if (pivotAt(bars, i, "low", HALF)) {
      addOrUpdate(pivots, { index: i, price: bars[i].low, kind: "low", confirmedAt });
    }
  }
  if (ZIGZAG_FIXED.projectionPivots) invalidateLast(bars, pivots);
  const segments = [];
  for (let i = 1; i < pivots.length; i++) segments.push(makeSegment(pivots[i - 1], pivots[i], prefix));
  let projected = null;
  const last = pivots[pivots.length - 1];
  if (ZIGZAG_FIXED.projectionPivots && last) {
    const next = findProjection(bars, last);
    if (next) projected = makeSegment(last, next, prefix);
  }
  return { pivots, segments, projected };
}
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  ZIGZAG_FIXED,
  computeZigZag
});
