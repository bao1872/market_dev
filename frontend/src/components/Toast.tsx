// 全局 Toast 通知组件（对应原型 UI.toast）
// 用法：在 main.tsx 挂载 <Toast />，任意位置调用 useToast.getState().show('标题', '消息')
// [Phase 5B-2] 修复管理员登录"点击无反应"：Toast store 有 show() 调用但无组件渲染，
// 导致登录失败/Backend 不可达等错误不可见。本组件补全渲染层。
import { useToast } from '@/store/toast'

export default function Toast() {
  const visible = useToast((s) => s.visible)
  const title = useToast((s) => s.title)
  const message = useToast((s) => s.message)

  if (!visible) return null

  return (
    <div className={`toast show`} role="alert" aria-live="polite">
      <div className="toast-title">{title}</div>
      {message ? <div className="toast-msg">{message}</div> : null}
    </div>
  )
}
