// [reviewAnchorScroll] - 描述: 轻量 anchor scroll 工具（R3B §10）
//
// Current 内部 sub-navigation 仅 presentational：用 anchor + scrollIntoView，
// 不创建 useState sub-tab / 不新增 URL state / 不新增 query 参数。
// 单一顶层 Review tab 仍是唯一 tab-state owner。

export function anchorScroll(targetId: string): void {
  if (typeof document === 'undefined') return
  const el = document.getElementById(targetId)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  // 给目标一个短暂高亮，便于用户感知定位（不改变任何状态）。
  el.setAttribute('data-scroll-active', 'true')
  window.setTimeout(() => el.removeAttribute('data-scroll-active'), 1200)
}
