// Marketing Preview 独立入口（Lightweight Deployment Lane）
//
// 设计约束（见 M0_ARCHITECTURE_CONTRACT.md §Preview Lane）：
// - 该入口只渲染 MarketingPage，且 MarketingPage 为 fully deterministic 静态页；
// - 不挂载 RouterProvider / QueryClientProvider / Toast / App.tsx；
// - 不依赖任何产品运行时，因此无需整站 SPA runtime。
// 产物经 scripts/ops/panji-marketing-preview-deploy 同步到
// /opt/panji-live/frontend/dist/marketing-preview/，由 Nginx live mount 直接可见，
// 无需 restart frontend / backend / 改 nginx。

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
