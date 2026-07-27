// [门户] - 描述: 公开根路径 / 的 SPA 内部导航兜底
// 首次访问 / 由 Nginx 精确分流直接返回 /portal/index.html，不进入 React SPA。
// 本组件处理 SPA 内部导航到 / 的场景：
//   - 开发环境（Vite）：一次性跳转 /portal/index.html（Vite 静态服务 public 目录）
//   - 生产环境：Nginx 已正常分流，理论上不进入本组件；若 Nginx 误配置进入，
//     显示稳定门户入口链接，禁止自跳转到当前 URL（避免无限刷新）
// [Phase 5B-1] 修复：原 window.location.replace('/') 在 Vite 下触发无限刷新
import { useEffect } from 'react'

const PORTAL_PATH = '/portal/index.html'

export default function LandingPage() {
  useEffect(() => {
    // 开发环境：Vite 已将 frontend/public 映射到根路径，/portal/index.html 可直接访问
    // 一次性跳转到静态门户页，离开 SPA 进入静态 HTML
    if (import.meta.env.DEV) {
      window.location.replace(PORTAL_PATH)
    }
    // 生产环境不跳转：Nginx 应直接返回门户，进入本组件属于异常兜底场景，
    // 由下方 JSX 渲染稳定入口链接，避免无限刷新
  }, [])

  // 生产兜底（开发环境跳转后此 JSX 不会显示，仅作为 mount 到 replace 之间的瞬态占位）
  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#07110c',
        color: '#e6f0ff',
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '16px',
        padding: '24px',
        textAlign: 'center',
      }}
    >
      <h1 style={{ fontSize: '24px', margin: 0 }}>盘迹</h1>
      <p style={{ margin: 0, opacity: 0.8 }}>
        门户加载异常，请通过以下入口访问：
      </p>
      <nav style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', justifyContent: 'center' }}>
        <a
          href={PORTAL_PATH}
          style={{
            color: '#7fb3ff',
            padding: '8px 16px',
            border: '1px solid #2a4365',
            borderRadius: '4px',
            textDecoration: 'none',
          }}
        >
          使用说明首页
        </a>
        <a
          href="/login"
          style={{
            color: '#7fb3ff',
            padding: '8px 16px',
            border: '1px solid #2a4365',
            borderRadius: '4px',
            textDecoration: 'none',
          }}
        >
          登录盘迹
        </a>
        <a
          href="/market"
          style={{
            color: '#7fb3ff',
            padding: '8px 16px',
            border: '1px solid #2a4365',
            borderRadius: '4px',
            textDecoration: 'none',
          }}
        >
          进入行情
        </a>
      </nav>
    </div>
  )
}
