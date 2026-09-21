// [R3A] 共享比较篮（compare basket）——跨 /review/industry、/review/concept、/review/compare 导航保持。
//
// 设计约束（R3A §3）：
//   - store **只**保存最小身份信息 { id, name, type }；
//   - API response / chart data / MA 数值 **绝不**进入 store（server state 归 React Query）；
//   - HARD CAP = 20，超出不再接收（不静默淘汰已有选择）；
//   - 重复 add = no-op（状态不变）；
//   - industry / concept 混合允许（前端不人为禁止）。
//
// 会话内保持即可（SPA 导航不丢）；不要求 localStorage 持久化。
import { create } from 'zustand'
import type { ScopeType } from '@/features/market-dashboard/types'

/** 与 backend compare endpoint 上限一致。 */
export const COMPARE_BASKET_MAX = 20

export interface CompareBasketItem {
  id: string
  name: string
  type: ScopeType
}

export type CompareBasketAddResult = 'added' | 'duplicate' | 'full'

interface CompareBasketState {
  items: CompareBasketItem[]
  add: (item: CompareBasketItem) => CompareBasketAddResult
  remove: (id: string) => void
  clear: () => void
  contains: (id: string) => boolean
}

export const useCompareBasketStore = create<CompareBasketState>((set, get) => ({
  items: [],
  add: (item) => {
    const { items } = get()
    // 重复 add no-op（先判重，避免满仓时把 duplicate 误报为 full）。
    if (items.some((i) => i.id === item.id)) return 'duplicate'
    if (items.length >= COMPARE_BASKET_MAX) return 'full'
    set({ items: [...items, item] })
    return 'added'
  },
  remove: (id) => set({ items: get().items.filter((i) => i.id !== id) }),
  clear: () => set({ items: [] }),
  contains: (id) => get().items.some((i) => i.id === id),
}))
