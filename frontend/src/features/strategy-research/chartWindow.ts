/** Display-only viewport: never invent data or crop a selected event below 90 bars. */
export const MIN_VISIBLE_BARS = 90
export function chartWindow(count: number, focusIndex?: number, requested = MIN_VISIBLE_BARS) {
  if (!Number.isInteger(count) || count < 1) return null
  const width = Math.min(count, Math.max(MIN_VISIBLE_BARS, requested))
  const focus = focusIndex == null ? count - 1 : Math.min(count - 1, Math.max(0, focusIndex))
  const from = Math.min(Math.max(0, focus - Math.floor(width * .7)), count - width)
  return {from, to: from + width - 1}
}
