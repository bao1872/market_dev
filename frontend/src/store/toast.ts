// 全局 Toast 通知 store（对应原型 UI.toast）
// 用法：import { useToast } from '@/store/toast'; useToast.getState().show('标题', '消息')
import { create } from 'zustand'

interface ToastState {
  visible: boolean
  title: string
  message: string
  show: (title: string, message?: string) => void
  hide: () => void
}

let toastTimer: ReturnType<typeof setTimeout> | null = null

export const useToast = create<ToastState>((set) => ({
  visible: false,
  title: '',
  message: '',
  show: (title: string, message: string = '操作已完成') => {
    if (toastTimer) clearTimeout(toastTimer)
    set({ visible: true, title, message })
    toastTimer = setTimeout(() => set({ visible: false }), 2500)
  },
  hide: () => set({ visible: false }),
}))
