// [MobileIndicatorStage] - 描述: 飞书消息图片移动舞台（1440×2560 9:16）
//
// 用法：CaptureStockPage 在 capture 模式下包裹 StrategyChart，替代旧 1920×1200 PC 布局。
//   <MobileIndicatorStage
//     stockName="京东方A"
//     stockSymbol="000725"
//     indicatorView="smc"
//     currentPrice={4.32}
//     changePercent={2.18}
//     chartDate="2026-07-20"
//   >
//     <StrategyChart isCaptureMode indicatorView="smc" ... />
//   </MobileIndicatorStage>
//
// 设计参考：ref/panji_short_video_integrated_studio_v1_15_event_flash_fix
//   - styles/tokens.css    视觉令牌（#02090c / #00e2b8 / 1440×2560）
//   - styles/scenes.css    scene-main 布局参数（main-header / chart-card / flat-progress / risk-notice）
//   - styles/studio.css    stage 容器（texture / safe-area / risk-notice）
//   - index.html           DOM 结构（main-header + chart-card + flat-progress + risk-notice）
//
// 改造差异（与原 panji 短视频舞台不同）：
//   1. 只渲染静态截图，无 cover/end scene、无 flat-progress、无 countdown
//   2. iframe 改为直接子节点（StrategyChart），保留 chart-viewport 容器
//   3. 主图头部 module-label 显示 INDICATOR_VIEW_LABELS[indicatorView]
//   4. 累计涨跌幅取自 URL/source_bar_time 截止根 bar 的 close vs 首根可见 bar 的 close
//      （后端 snapshot 已按 adjustment_as_of 截止；前端只展示，不重算）
//   5. 三行风险提示常驻底部，与 panji stage-risk-notice 一致
//
// [CHANGE-20260720-Phase4 §四] 配合 advice.md v6：每张截图只渲染一个 indicator_view 对应的图层

import type { ReactNode } from 'react'
import type { IndicatorView } from '../api/endpoints'
import { INDICATOR_VIEW_LABELS } from '../features/stock-research/stockResearchTypes'

export interface MobileIndicatorStageProps {
  /** 股票名称（如 "京东方A"） */
  stockName: string
  /** 股票代码（如 "000725"） */
  stockSymbol: string
  /** 指标视图（决定 module-label 文案与色相） */
  indicatorView: IndicatorView
  /** 现价；缺失时显示 "—" */
  currentPrice: number | null
  /** 累计涨跌幅（百分比）；正数红、负数绿；缺失时显示 "—" */
  changePercent: number | null
  /** 当前 K 线日期（YYYY-MM-DD 或 ISO 字符串） */
  chartDate: string | null
  /** 图表区域子节点（StrategyChart with isCaptureMode + indicatorView） */
  children: ReactNode
  /** 品牌名（默认 "盘迹"） */
  brandName?: string
  /** 品牌副标题（默认 "用数据拆解市场和价格背后的结构"） */
  brandTagline?: string
  /** 测试用 data-testid 后缀，便于 Playwright 选择 */
  testId?: string
}

/**
 * 飞书消息图片移动舞台组件。
 *
 * 视觉令牌与场景布局完全移植自 panji_short_video_integrated_studio_v1_15_event_flash_fix，
 * 通过 CSS 变量（--stage-w / --stage-h / --bg / --accent 等）实现单一真源；
 * 截图时 Playwright 设置 device_scale_factor=1，viewport=1440×2560 即可获得 1:1 PNG。
 */
export default function MobileIndicatorStage({
  stockName,
  stockSymbol,
  indicatorView,
  currentPrice,
  changePercent,
  chartDate,
  children,
  brandName = '盘迹',
  brandTagline = '用数据拆解市场和价格背后的结构',
  testId,
}: MobileIndicatorStageProps) {
  const isUp = changePercent === null ? true : changePercent >= 0
  const moduleLabel = INDICATOR_VIEW_LABELS[indicatorView]
  const priceText = currentPrice !== null ? currentPrice.toFixed(2) : '—'
  const changeText =
    changePercent !== null
      ? `${isUp ? '+' : ''}${changePercent.toFixed(2)}%`
      : '—'
  const chartDateText = chartDate || '—'

  return (
    <div
      className="mobile-stage"
      data-testid={testId ? `mobile-stage-${testId}` : 'mobile-stage'}
      data-indicator-view={indicatorView}
      data-render-ready="true"
    >
      {/* 舞台背景纹理（移植自 .stage::before + .stage-texture） */}
      <div className="mobile-stage-texture" aria-hidden="true" />

      {/* ===== 顶部 Header：品牌行 + 股票摘要 ===== */}
      <header className="mobile-stage-header">
        <div className="mobile-stage-brand-row">
          <div className="mobile-stage-brand">
            <div className="mobile-stage-brand-mark" aria-hidden="true">
              <span />
            </div>
            <div className="mobile-stage-brand-text">
              <strong>{brandName}</strong>
              <span>{brandTagline}</span>
            </div>
          </div>
          <div className="mobile-stage-module-label">{moduleLabel}</div>
        </div>

        <div className="mobile-stage-stock-summary">
          <div className="mobile-stage-stock-identity">
            <strong>{stockName}</strong>
            <span>{stockSymbol}</span>
          </div>
          <div className="mobile-stage-market-summary">
            <div
              className={`mobile-stage-return-summary${isUp ? '' : ' down'}`}
            >
              <span>累计涨跌幅</span>
              <b>{changeText}</b>
            </div>
            <div className="mobile-stage-price-summary">
              <span>现价</span>
              <b>{priceText}</b>
            </div>
          </div>
        </div>
      </header>

      {/* ===== 主图卡片：图表头部 + 图表视口 ===== */}
      <section className="mobile-stage-chart-card">
        <header className="mobile-stage-chart-head">
          <div className="mobile-stage-chart-title">
            <i aria-hidden="true" />
            <span>K线图（日线）</span>
          </div>
          <time>{chartDateText}</time>
        </header>
        <div className="mobile-stage-chart-viewport">
          {children}
        </div>
      </section>

      {/* ===== 底部三行风险提示（移植自 .stage-risk-notice） ===== */}
      <div className="mobile-stage-risk-notice" aria-label="内容声明">
        <span>随机历史数据复盘</span>
        <span>内容仅做科普，不构成投资建议</span>
        <span>投资有风险，入市需谨慎</span>
      </div>
    </div>
  )
}
