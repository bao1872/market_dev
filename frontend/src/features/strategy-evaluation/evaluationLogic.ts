// Pure display helpers. Numerical evidence is calculated by the backend owner.
export const MODEL_VISUALS: Readonly<Record<string, { color: string; shape: 'circle' | 'diamond' | 'triangle' | 'square' }>> = {
  joint_d1: { color: '#3882F6', shape: 'circle' },
  joint_d2: { color: '#F59E0B', shape: 'triangle' },
  joint_d3: { color: '#8B5CF6', shape: 'diamond' },
}
const FALLBACK = [
  { color: '#00F6C2', shape: 'square' as const },
  { color: '#9CA3AF', shape: 'circle' as const },
]
export function modelVisual(id: string, index: number) {
  return MODEL_VISUALS[id] ?? FALLBACK[index % FALLBACK.length]
}
export function fmtPct(n: number | null | undefined, signed = false): string {
  return n == null || !Number.isFinite(n) ? '—' : `${signed && n > 0 ? '+' : ''}${n.toFixed(2)}%`
}
export function fmtRate(n: number | null | undefined): string {
  return n == null || !Number.isFinite(n) ? '—' : `${(n * 100).toFixed(1)}%`
}
export function nonnegative(value: number): number { return Math.max(0, value) }
