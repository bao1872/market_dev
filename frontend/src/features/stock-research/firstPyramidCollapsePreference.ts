// [P0 2026-07-30] 第一金字塔折叠偏好（纯函数，可单元测试）
// 持久化键：panji:first-pyramid-detail-collapsed:v1
// 默认：展开（false）
// localStorage 不可用时静默降级到内存状态

export const FIRST_PYRAMID_COLLAPSE_STORAGE_KEY = 'panji:first-pyramid-detail-collapsed:v1'

export function loadFirstPyramidCollapsed(): boolean {
  try {
    return localStorage.getItem(FIRST_PYRAMID_COLLAPSE_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

export function saveFirstPyramidCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(FIRST_PYRAMID_COLLAPSE_STORAGE_KEY, String(collapsed))
  } catch {
    // localStorage 不可用时静默降级（保持内存状态）
  }
}
