// Marketing Site 独立入口（Lightweight Deployment Lane）
//
// 设计约束（见 M0_ARCHITECTURE_CONTRACT.md §Public Marketing Site Lightweight Lane）：
// - 该入口只渲染 MarketingPage，且 MarketingPage 为 fully deterministic 静态页；
// - 不挂载 RouterProvider / QueryClientProvider / Toast / App.tsx；
// - 不依赖任何产品运行时，因此无需整站 SPA runtime。
// 产物经 scripts/ops/panji-marketing-site-deploy 同步到
// /opt/panji-live/frontend/dist/portal/index.html（根路径），由 Nginx live mount 直接可见，
// 资源落在 /opt/panji-live/frontend/dist/marketing-assets/，无需 restart frontend / backend / 改 nginx。

import React from 'react'
import ReactDOM from 'react-dom/client'

import MarketingPage from './MarketingPage'
import '@/styles/global.scss'

ReactDOM
  .createRoot(document.getElementById('root')!)
  .render(
    <React.StrictMode>
      <MarketingPage />
    </React.StrictMode>,
  )
