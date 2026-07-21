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
  /** 图表区域子节点（StrategyChart with isCaptureMode + indicatorView）。
   *    loading/error/mismatch 三态可不传（只渲染舞台外壳 + 居中文案） */
  children?: ReactNode
  /** 品牌名（默认 "盘迹"） */
  brandName?: string
  /** 品牌副标题（默认 "用数据拆解市场和价格背后的结构"） */
  brandTagline?: string
  /** 测试用 data-testid 后缀，便于 Playwright 选择 */
  testId?: string
  /** [PROMPT.md §5.3.1 V2] 截图根选择器：true 时根节点 data-testid="stock-detail-capture"，
   *    Playwright 截取完整舞台（含品牌/股票名/风险提示），不再只截图表。
   *    默认 true（CaptureStockPage 正常态）；loading/error 态由 CaptureStockPage 自行设置。
   */
  captureRoot?: boolean
  /** [PROMPT.md §5.3.1 V2] 动态 Ready 状态：false 时根节点 data-render-ready="false"，
   *    Playwright 等待该属性为 "true" 才截图；类型特定 Ready 由 CaptureStockPage 计算。
   */
  renderReady?: boolean
  /** [PROMPT.md §5.3.3 V2] 发送时间（后端 snapshot_time UTC ISO 字符串），
   *    组件内部转 Asia/Shanghai 显示，禁止浏览器本地时间猜测。
   *    缺失时不渲染发送时间行。
   */
  snapshotTime?: string | null
  /** [PROMPT.md §5.3.1 V2] 错误/加载态标记：'loading' | 'error' | 'mismatch' | null
   *    非 null 时根节点附加 data-state 属性，便于 Playwright 统一选择器处理三态。
   */
  state?: 'loading' | 'error' | 'mismatch' | null
  /** [PROMPT.md §5.3.1 V2] 错误/加载态文案（state 非 null 时显示） */
  stateMessage?: ReactNode
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
  captureRoot = true,
  renderReady = true,
  snapshotTime,
  state = null,
  stateMessage,
}: MobileIndicatorStageProps) {
  const isUp = changePercent === null ? true : changePercent >= 0
  const moduleLabel = INDICATOR_VIEW_LABELS[indicatorView]
  const priceText = currentPrice !== null ? currentPrice.toFixed(2) : '—'
  const changeText =
    changePercent !== null
      ? `${isUp ? '+' : ''}${changePercent.toFixed(2)}%`
      : '—'
  const chartDateText = chartDate || '—'

  // [PROMPT.md §5.3.3 V2] 发送时间：后端 snapshot_time 转 Asia/Shanghai
  //   禁止浏览器本地时间猜测；缺失时不渲染发送时间行
  const sendTimeText = formatSendTime(snapshotTime)

  // [PROMPT.md §5.3.1 V2] 截图根选择器 + 动态 Ready
  //   - captureRoot=true 时根节点 data-testid="stock-detail-capture"（Playwright 截完整舞台）
  //   - renderReady 驱动 data-render-ready，类型特定 Ready 由 CaptureStockPage 计算
  //   - state 非 null 时附加 data-state，loading/error/mismatch 三态统一根选择器
  const rootTestId = captureRoot ? 'stock-detail-capture' : (testId ? `mobile-stage-${testId}` : 'mobile-stage')

  // 错误/加载态：只渲染舞台外壳 + 居中文案，不渲染 header/chart/risk-notice
  if (state !== null) {
    return (
      <div
        className={`mobile-stage mobile-stage-${state}`}
        data-testid={rootTestId}
        data-render-ready="false"
        data-indicator-view={indicatorView}
        data-state={state}
      >
        <div className="mobile-stage-texture" aria-hidden="true" />
        <div className={`mobile-stage-${state}-text`}>
          {stateMessage}
        </div>
      </div>
    )
  }

  return (
    <div
      className="mobile-stage"
      data-testid={rootTestId}
      data-indicator-view={indicatorView}
      data-render-ready={renderReady ? 'true' : 'false'}
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

      {/* ===== 底部发送时间 + 三行风险提示 ===== */}
      <div className="mobile-stage-footer">
        {sendTimeText && (
          <div className="mobile-stage-send-time" data-testid="mobile-stage-send-time">
            <span>发送时间</span>
            <time>{sendTimeText}</time>
          </div>
        )}
        <div className="mobile-stage-risk-notice" aria-label="内容声明">
          <span>随机历史数据复盘</span>
          <span>内容仅做科普，不构成投资建议</span>
          <span>投资有风险，入市需谨慎</span>
        </div>
      </div>
    </div>
  )
}

/**
 * [PROMPT.md §5.3.3 V2] 将后端 snapshot_time（UTC ISO）转为 Asia/Shanghai 显示。
 *
 * 禁止使用浏览器本地时间（new Date().toLocaleString()），
 * 因为截图 worker 可能运行在不同时区，导致发送时间与用户时区不一致。
 *
 * @param snapshotTimeUtc ISO 8601 字符串（含时区，如 "2026-07-20T15:30:00Z"）
 * @returns "YYYY-MM-DD HH:mm" 格式字符串（Asia/Shanghai），或 null（输入缺失/无效）
 */
function formatSendTime(snapshotTimeUtc: string | null | undefined): string | null {
  if (!snapshotTimeUtc) return null
  const d = new Date(snapshotTimeUtc)
  if (isNaN(d.getTime())) return null
  // 强制 Asia/Shanghai 时区显示（+08:00）
  const dateFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  // zh-CN 格式：2026/07/20 15:30 → 转 2026-07-20 15:30
  return dateFmt.format(d).replace(/\//g, '-')
}
