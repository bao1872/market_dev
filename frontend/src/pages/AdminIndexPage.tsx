// 管理后台首页（受保护路由，admin only）
// 对应原型：admin/index.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 卡片组（admin-accent 紫色左边框，4 列）：有效用户 / 去重监控股票 / 最近一分钟处理 / 评估成功率
// 3. split-3 布局：监控吞吐折线图（Canvas）/ DSA 最近运行 / 队列与任务
// 4. split-2 布局：服务健康状态表 / 最近异常列表
// 5. 操作：维护模式开关 / 暂停全局推送开关（尚未接入后端，已禁用）
//
// 依赖 hooks：
// - useAdminSystemOverview：获取系统概览数据（活跃用户/监控标的/评估统计）
// - useStrategies：获取策略目录（策略数，显示在页头描述）

import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useStrategies, useAdminSystemOverview } from '@/hooks/useApi'
import { getVersion, type VersionInfo } from '@/api/endpoints'

// ===== 监控吞吐折线图 =====

function ThroughputChartPlaceholder() {
  return (
    <div className="chart-panel small" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <span className="notice">暂无数据</span>
    </div>
  )
}

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
  const [backendVersion, setBackendVersion] = useState<VersionInfo | null>(null)

  // 获取后端版本信息
  useEffect(() => {
    getVersion().then(setBackendVersion).catch(() => {})
  }, [])

  // API 数据查询
  const strategiesQuery = useStrategies()
  const overviewQuery = useAdminSystemOverview()

  // 策略数（从 useStrategies 获取 total，显示在页头描述）
  const strategyCount = strategiesQuery.data?.total ?? 0

  // 系统概览数据
  const overview = overviewQuery.data
  const overviewLoading = overviewQuery.isLoading

  // KPI 1：有效用户数（从 system-overview 获取 active_users）
  const activeUsers = overview?.active_users ?? 0

  // KPI 2：去重监控股票数
  const distinctInstruments = overview?.distinct_monitored_instruments ?? 0

  // KPI 3：最近一分钟处理数
  const evalsLastMinute = overview?.evaluations_last_minute ?? 0

  // KPI 4：评估成功率
  const evalSuccessRate = overview?.evaluations_success_rate ?? 0
  const evalSuccessRatePct = evalSuccessRate > 0 ? `${(evalSuccessRate * 100).toFixed(1)}%` : '暂无数据'

  // DSA 最近运行（从 system-overview 获取）
  const latestSelectorRun = overview?.latest_selector_run
  const hasDsaRun = !!latestSelectorRun
  const dsaDuration = hasDsaRun
    ? formatDuration(latestSelectorRun!.started_at, latestSelectorRun!.finished_at)
    : '-'
  const dsaRunTime = formatRunTime(latestSelectorRun?.finished_at)

  // 队列与任务
  const queueBacklog = overview?.queue_backlog ?? 0
  const failedRetryCount = overview?.failed_retry_count ?? 0

  // 服务健康
  const workerHealth = overview?.worker_health ?? 'unknown'
  const schedulerHealth = overview?.scheduler_health ?? 'unknown'

  // 切换维护模式（尚未接入后端，按钮已禁用）
  const handleToggleMaintenance = () => {
    // [admin] - 维护模式尚未接入后端 API，暂不执行任何操作
  }

  // 切换暂停全局推送（尚未接入后端，按钮已禁用）
  const handleTogglePausePush = () => {
    // [admin] - 暂停全局推送尚未接入后端 API，暂不执行任何操作
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
            className="btn"
            onClick={handleToggleMaintenance}
            disabled
            title="尚未接入后端"
          >
            维护模式
            <span style={{ fontSize: '0.75em', opacity: 0.6, marginLeft: 4 }}>尚未接入后端</span>
          </button>
          <button
            className="btn"
            onClick={handleTogglePausePush}
            disabled
            title="尚未接入后端"
          >
            暂停全局推送
            <span style={{ fontSize: '0.75em', opacity: 0.6, marginLeft: 4 }}>尚未接入后端</span>
          </button>
        </div>
      </div>

      {/* KPI 卡片组（admin-accent 紫色左边框，4 列）*/}
      <div className="grid kpi">
        {/* KPI 1：有效用户数（从 system-overview 获取）*/}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">有效用户</div>
          <div className="kpi-value">
            {overviewLoading ? '-' : activeUsers}
          </div>
          <div className="kpi-foot">有活跃自选股的用户</div>
        </div>
        {/* KPI 2：去重监控股票数 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">去重监控股票</div>
          <div className="kpi-value">
            {overviewLoading ? '-' : distinctInstruments}
          </div>
          <div className="kpi-foot">活跃自选股去重</div>
        </div>
        {/* KPI 3：最近一分钟处理数 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">最近一分钟处理</div>
          <div className="kpi-value">
            {overviewLoading ? '-' : evalsLastMinute}
          </div>
          <div className="kpi-foot">评估完成数</div>
        </div>
        {/* KPI 4：评估成功率 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">评估成功率</div>
          <div className="kpi-value">
            {overviewLoading ? '-' : evalSuccessRatePct}
          </div>
          <div className="kpi-foot">SUCCEEDED / 已完成</div>
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
          </div>
          <ThroughputChartPlaceholder />
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
            {overviewLoading ? (
              <div className="notice">加载中…</div>
            ) : !hasDsaRun ? (
              <div className="notice">今日尚未运行</div>
            ) : (
              <>
                <div className="toggle-row">
                  <span>处理股票</span>
                  <b className="num">{latestSelectorRun!.total_instruments ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>成功</span>
                  <b className="num">{latestSelectorRun!.succeeded_count ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>失败</span>
                  <b className="num">{latestSelectorRun!.failed_count ?? '-'}</b>
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
              <span>监控计算</span>
              <b className="num">{overviewLoading ? '-' : queueBacklog}</b>
            </div>
            <div className="toggle-row">
              <span>事件分发</span>
              <b className="num">暂无数据</b>
            </div>
            <div className="toggle-row">
              <span>通知投递</span>
              <b className="num">暂无数据</b>
            </div>
            <div className="toggle-row">
              <span>失败重试</span>
              <b className="num">{overviewLoading ? '-' : failedRetryCount}</b>
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
          <div className="card-body">
            <div className="toggle-row">
              <span>Worker</span>
              <b className="num">{workerHealth}</b>
            </div>
            <div className="toggle-row">
              <span>Scheduler</span>
              <b className="num">{schedulerHealth}</b>
            </div>
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
          <div className="card-body">
            <div className="notice">暂无数据</div>
          </div>
        </section>
      </div>

      {/* 版本信息 */}
      <div className="grid split-2 section-gap">
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">版本信息</div>
              <div className="card-sub">前端构建版本与后端运行版本</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>前端 Git SHA</span>
              <b className="num" style={{ fontSize: '0.85em' }}>
                {import.meta.env.VITE_GIT_SHA ?? 'dev'}
              </b>
            </div>
            <div className="toggle-row">
              <span>前端构建时间</span>
              <b className="num" style={{ fontSize: '0.85em' }}>
                {import.meta.env.VITE_BUILD_TIME ?? '-'}
              </b>
            </div>
            <div className="toggle-row">
              <span>后端 App 版本</span>
              <b className="num">{backendVersion?.app_version ?? '-'}</b>
            </div>
            <div className="toggle-row">
              <span>后端 Git SHA</span>
              <b className="num" style={{ fontSize: '0.85em' }}>
                {backendVersion?.git_sha ?? '-'}
              </b>
            </div>
            <div className="toggle-row">
              <span>后端构建时间</span>
              <b className="num" style={{ fontSize: '0.85em' }}>
                {backendVersion?.build_time ?? '-'}
              </b>
            </div>
            <div className="toggle-row">
              <span>Alembic 迁移版本</span>
              <b className="num" style={{ fontSize: '0.85em' }}>
                {backendVersion?.alembic_revision ?? '-'}
              </b>
            </div>
          </div>
        </section>
        <section className="card" />
      </div>
    </>
  )
}
