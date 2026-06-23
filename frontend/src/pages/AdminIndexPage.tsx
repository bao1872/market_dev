// 管理后台首页（受保护路由，admin only）
// 对应原型：admin/index.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 卡片组（admin-accent 紫色左边框，4 列）：有效用户 / 去重监控股票 / 最近一分钟处理 / 今日投递成功率
// 3. split-3 布局：监控吞吐折线图（Canvas）/ DSA 最近运行 / 队列与任务
// 4. split-2 布局：服务健康状态表 / 最近异常列表
// 5. 操作：维护模式开关 / 暂停全局推送开关（本地状态 + toast 反馈）
//
// 依赖 hooks：
// - useMembers：获取会员总数（KPI 有效用户）
// - useStrategies：获取策略目录（策略数，显示在页头描述）
// - useStrategyRuns：获取 DSA 最近运行记录（DSA 卡片处理数/耗时）

import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { useToast } from '@/store/toast'
import { useMembers, useStrategies, useStrategyRuns } from '@/hooks/useApi'

// ===== 监控吞吐折线图（Canvas 2D 简单实现）=====
// 对应原型 chart-canvas data-chart="line" data-variant="green"
// 绘制最近 45 分钟的处理股票数折线图（绿色填充区域）

// 45 分钟吞吐量样本数据（每分钟一个点，模拟实时监控数据）
const THROUGHPUT_SAMPLES = [
  240, 252, 268, 275, 281, 286, 290, 283, 278, 285,
  291, 296, 288, 282, 279, 285, 290, 295, 287, 281,
  276, 282, 288, 293, 286, 280, 275, 281, 286, 290,
  285, 280, 278, 284, 289, 292, 286, 281, 277, 283,
  288, 291, 286, 282, 286,
]

function ThroughputChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  // 绘制折线图：网格 + 折线 + 渐变填充 + 轴标签
  const drawChart = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // 处理高 DPI 屏幕，确保线条清晰
    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * dpr
    canvas.height = rect.height * dpr
    ctx.scale(dpr, dpr)

    const w = rect.width
    const h = rect.height
    const padLeft = 36
    const padRight = 12
    const padTop = 14
    const padBottom = 22

    const chartW = w - padLeft - padRight
    const chartH = h - padTop - padBottom

    // 数据范围（上下留 10% 余量）
    const data = THROUGHPUT_SAMPLES
    const maxVal = Math.max(...data) * 1.1
    const minVal = Math.min(...data) * 0.9
    const range = maxVal - minVal || 1

    // 清空画布
    ctx.clearRect(0, 0, w, h)

    // 绘制水平网格线 + Y 轴标签
    ctx.strokeStyle = '#1b2230'
    ctx.lineWidth = 1
    ctx.font = '10px ui-monospace, monospace'
    ctx.fillStyle = '#778297'
    for (let i = 0; i <= 4; i++) {
      const y = padTop + (chartH / 4) * i
      ctx.beginPath()
      ctx.moveTo(padLeft, y)
      ctx.lineTo(w - padRight, y)
      ctx.stroke()
      const val = Math.round(maxVal - (range / 4) * i)
      ctx.fillText(String(val), 4, y + 3)
    }

    // 绘制渐变填充区域（折线下方）
    ctx.beginPath()
    data.forEach((v, i) => {
      const x = padLeft + (chartW / (data.length - 1)) * i
      const y = padTop + chartH - ((v - minVal) / range) * chartH
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    })
    ctx.lineTo(padLeft + chartW, padTop + chartH)
    ctx.lineTo(padLeft, padTop + chartH)
    ctx.closePath()
    const gradient = ctx.createLinearGradient(0, padTop, 0, padTop + chartH)
    gradient.addColorStop(0, 'rgba(38, 166, 154, 0.25)')
    gradient.addColorStop(1, 'rgba(38, 166, 154, 0)')
    ctx.fillStyle = gradient
    ctx.fill()

    // 绘制折线（绿色）
    ctx.strokeStyle = '#26a69a'
    ctx.lineWidth = 2
    ctx.beginPath()
    data.forEach((v, i) => {
      const x = padLeft + (chartW / (data.length - 1)) * i
      const y = padTop + chartH - ((v - minVal) / range) * chartH
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    })
    ctx.stroke()

    // 绘制 X 轴时间标签
    ctx.fillStyle = '#778297'
    ctx.font = '9px ui-monospace, monospace'
    const labels = ['-45m', '-30m', '-15m', '现在']
    labels.forEach((label, i) => {
      const x = padLeft + (chartW / 3) * i
      ctx.fillText(label, x - 12, h - 6)
    })
  }

  useEffect(() => {
    drawChart()
    // 窗口尺寸变化时重绘
    const handleResize = () => drawChart()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="chart-panel small">
      <canvas
        ref={canvasRef}
        className="chart-canvas"
        data-chart="line"
        data-variant="green"
      />
    </div>
  )
}

// ===== 服务健康状态行类型 =====
interface HealthRow {
  component: string
  latestTime: string
  latency: string
  pillClass: string // 'ok' | 'warn'
  statusText: string
}

// 服务健康状态数据（无直接 API，使用原型展示数据）
const HEALTH_ROWS: HealthRow[] = [
  { component: '分钟行情 · Tushare/备用源', latestTime: '10:32:00', latency: '8s', pillClass: 'ok', statusText: '正常' },
  { component: 'Node Monitor Worker', latestTime: '10:32:08', latency: '8s', pillClass: 'ok', statusText: '正常' },
  { component: '通知投递 Worker', latestTime: '10:32:11', latency: '3s', pillClass: 'warn', statusText: '7 次重试' },
  { component: 'DSA Daily Selector', latestTime: '昨日 15:12', latency: '-', pillClass: 'ok', statusText: '已完成' },
]

// 最近异常列表项类型
interface AnomalyItem {
  icon: string
  iconClass: string // 'danger' | 'warn' | 'info'
  title: string
  meta: string
  tagClass: string // 'bad' | 'warn' | 'info'
  tagText: string
}

// 最近异常数据（无直接 API，使用原型展示数据）
const ANOMALIES: AnomalyItem[] = [
  {
    icon: '!',
    iconClass: 'danger',
    title: '7 条飞书 Webhook 返回 429',
    meta: '已按 Retry-After 排队重试 · 影响 4 个用户',
    tagClass: 'bad',
    tagText: '待恢复',
  },
  {
    icon: '!',
    iconClass: 'warn',
    title: '6 只股票 DSA 数据不完整',
    meta: '数据源缺少最新交易日 · 已排除结果',
    tagClass: 'warn',
    tagText: '部分完成',
  },
  {
    icon: 'i',
    iconClass: 'info',
    title: 'Node Monitor v2.2.0 影子运行中',
    meta: '覆盖 10% 股票 · 与 v2.1.0 事件一致率 98.7%',
    tagClass: 'info',
    tagText: '观察',
  },
]

// ===== 工具函数 =====

/** 格式化耗时（从 started_at 到 finished_at，输出 "Xm Ys"）*/
function formatDuration(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt || !finishedAt) return '-'
  const start = new Date(startedAt).getTime()
  const end = new Date(finishedAt).getTime()
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '-'
  const seconds = Math.round((end - start) / 1000)
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

/** 格式化运行时间（ISO → "YYYY-MM-DD HH:MM"）*/
function formatRunTime(iso: string | null | undefined): string {
  if (!iso) return '今日尚未运行'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '今日尚未运行'
  const Y = d.getFullYear()
  const M = String(d.getMonth() + 1).padStart(2, '0')
  const D = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${Y}-${M}-${D} ${hh}:${mm}`
}

// ===== 主页面 =====
export default function AdminIndexPage() {
  const toast = useToast()
  const [maintenanceMode, setMaintenanceMode] = useState(false)
  const [pausePush, setPausePush] = useState(false)

  // API 数据查询
  const membersQuery = useMembers({ limit: 1 })
  const strategiesQuery = useStrategies()
  const dsaRunsQuery = useStrategyRuns('dsa', { limit: 1 })

  // KPI 1：有效用户数（从 useMembers 获取 total）
  const memberCount = membersQuery.data?.total ?? 0
  const memberLoading = membersQuery.isLoading

  // 策略数（从 useStrategies 获取 total，显示在页头描述）
  const strategyCount = strategiesQuery.data?.total ?? 0


  // DSA 最近运行
  const latestDsaRun = dsaRunsQuery.data?.items[0]
  const dsaLoading = dsaRunsQuery.isLoading
  const dsaError = dsaRunsQuery.isError

  // DSA 运行统计
  // 注：StrategyRun 无 processed/success/failed 字段，有运行记录时使用原型展示值
  const hasDsaRun = !!latestDsaRun
  const dsaProcessed = hasDsaRun ? 5362 : 0
  const dsaSuccess = hasDsaRun ? 5356 : 0
  const dsaFailed = hasDsaRun ? 6 : 0
  const dsaDuration = hasDsaRun
    ? formatDuration(latestDsaRun!.started_at, latestDsaRun!.finished_at)
    : '-'
  const dsaRunTime = formatRunTime(latestDsaRun?.finished_at)

  // 切换维护模式
  const handleToggleMaintenance = () => {
    const next = !maintenanceMode
    setMaintenanceMode(next)
    toast.show(
      next ? '已进入维护模式' : '已退出维护模式',
      next ? '普通用户将看到维护提示页' : '服务恢复正常访问',
    )
  }

  // 切换暂停全局推送
  const handleTogglePausePush = () => {
    const next = !pausePush
    setPausePush(next)
    toast.show(
      next ? '全局推送已暂停' : '全局推送已恢复',
      next ? '所有通知投递已暂停' : '通知投递已恢复',
    )
  }

  // 页头描述：附加策略数（数据加载完成后显示）
  const pageDescExtra = strategiesQuery.data
    ? ` · 已注册 ${strategyCount} 个策略`
    : ''

  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">系统概览</h1>
          <div className="page-desc">
            {`共享计算、任务、事件和通知基础设施的实时运行状态${pageDescExtra}`}
          </div>
        </div>
        <div className="actions">
          <button
            className={`btn${maintenanceMode ? ' danger' : ''}`}
            onClick={handleToggleMaintenance}
          >
            {maintenanceMode ? '退出维护模式' : '维护模式'}
          </button>
          <button
            className={`btn${pausePush ? ' danger' : ''}`}
            onClick={handleTogglePausePush}
          >
            {pausePush ? '恢复全局推送' : '暂停全局推送'}
          </button>
        </div>
      </div>

      {/* KPI 卡片组（admin-accent 紫色左边框，4 列）*/}
      <div className="grid kpi">
        {/* KPI 1：有效用户数（从 useMembers 获取）*/}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">有效用户</div>
          <div className="kpi-value">
            {memberLoading ? '-' : memberCount}
          </div>
          <div className="kpi-foot">今日活跃 62</div>
        </div>
        {/* KPI 2：去重监控股票数 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">去重监控股票</div>
          <div className="kpi-value">286</div>
          <div className="kpi-foot">来源于 1,742 条用户自选</div>
        </div>
        {/* KPI 3：最近一分钟处理数（100% 成功）*/}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">最近一分钟处理</div>
          <div className="kpi-value">286</div>
          <div className="kpi-foot">
            <span className="pos">100%</span> 成功 · 延迟 8 秒
          </div>
        </div>
        {/* KPI 4：今日投递成功率 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">今日投递成功率</div>
          <div className="kpi-value">99.2%</div>
          <div className="kpi-foot">失败 7 / 共 893</div>
        </div>
      </div>

      {/* split-3：监控吞吐 / DSA 最近运行 / 队列与任务 */}
      <div className="grid split-3">
        {/* 监控吞吐折线图 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">监控吞吐</div>
              <div className="card-sub">最近 45 分钟处理股票数</div>
            </div>
            <span className="status-pill ok">正常</span>
          </div>
          <ThroughputChart />
        </section>

        {/* DSA 最近运行 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">DSA 最近运行</div>
              <div className="card-sub">{dsaRunTime}</div>
            </div>
          </div>
          <div className="card-body">
            {dsaLoading ? (
              <div className="notice">加载中…</div>
            ) : dsaError ? (
              <div className="notice error">DSA 运行记录加载失败</div>
            ) : !hasDsaRun ? (
              <div className="notice">今日尚未运行</div>
            ) : (
              <>
                <div className="toggle-row">
                  <span>处理股票</span>
                  <b className="num">{dsaProcessed.toLocaleString()}</b>
                </div>
                <div className="toggle-row">
                  <span>成功</span>
                  <b className="num pos">{dsaSuccess.toLocaleString()}</b>
                </div>
                <div className="toggle-row">
                  <span>失败</span>
                  <b className="num neg">{dsaFailed}</b>
                </div>
                <div className="toggle-row">
                  <span>耗时</span>
                  <b className="num">{dsaDuration}</b>
                </div>
                <Link className="btn small card-body-action" to="/admin/strategies">
                  查看运行详情
                </Link>
              </>
            )}
          </div>
        </section>

        {/* 队列与任务 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">队列与任务</div>
              <div className="card-sub">当前积压</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>Node 计算</span>
              <b className="num">0</b>
            </div>
            <div className="toggle-row">
              <span>事件分发</span>
              <b className="num">3</b>
            </div>
            <div className="toggle-row">
              <span>通知投递</span>
              <b className="num">12</b>
            </div>
            <div className="toggle-row">
              <span>失败重试</span>
              <b className="num neg">7</b>
            </div>
            <Link className="btn small card-body-action" to="/admin/jobs">
              任务与事件 →
            </Link>
          </div>
        </section>
      </div>

      {/* split-2：服务健康状态 / 最近异常 */}
      <div className="grid split-2 section-gap">
        {/* 服务健康状态表 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">服务健康状态</div>
              <div className="card-sub">管理员无需进入服务器即可排查</div>
            </div>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>组件</th>
                  <th>最新时间</th>
                  <th>延迟</th>
                  <th>状态</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {HEALTH_ROWS.map((row) => (
                  <tr key={row.component}>
                    <td>{row.component}</td>
                    <td className="num">{row.latestTime}</td>
                    <td>{row.latency}</td>
                    <td>
                      <span className={`status-pill ${row.pillClass}`}>
                        {row.statusText}
                      </span>
                    </td>
                    <td>
                      <button className="btn small">详情</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* 最近异常列表 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">最近异常</div>
              <div className="card-sub">按影响范围排序</div>
            </div>
          </div>
          <div className="list">
            {ANOMALIES.map((item) => (
              <div className="list-item" key={item.title}>
                <div className={`list-icon ${item.iconClass}`}>{item.icon}</div>
                <div className="list-main">
                  <div className="list-title">{item.title}</div>
                  <div className="list-meta">{item.meta}</div>
                </div>
                <span className={`tag ${item.tagClass}`}>{item.tagText}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </>
  )
}
