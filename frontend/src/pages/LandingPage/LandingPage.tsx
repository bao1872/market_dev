// [门户] - 描述: 公开根路径 / 的 SPA 内部导航兜底
// 首次访问 / 由 Nginx 精确分流直接返回 /portal/index.html，不进入 React SPA。
// 本组件仅处理 SPA 内部导航到 / 的场景：mount 后触发完整页面请求 window.location.replace('/')，
// 让浏览器重新请求 /，由 Nginx 返回静态门户首页。
// 不使用 react-router 的 Navigate 跳转到静态页（Navigate 只在 SPA 内切换，无法离开 SPA 进入静态 HTML）。
import { useEffect } from 'react'

export default function LandingPage() {
  useEffect(() => {
    window.location.replace('/')
  }, [])

  return <div style={{ minHeight: '100vh', background: '#07110c' }} />
}
