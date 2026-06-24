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

import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useToast } from '@/store/toast'
import { useMembers, useStrategies, useStrategyRuns } from '@/hooks/useApi'
import { getVersion, type VersionInfo } from '@/api/endpoints'

// ===== 监控吞吐折线图 =====
// TODO: [AdminIndexPage] 接入 GET /admin/metrics/throughput API 获取实时吞吐数据后恢复 Canvas 折线图

function ThroughputChartPlaceholder() {
  return (
    <div className="chart-panel small" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <span className="notice">暂无数据</span>
    </div>
  )
}

// TODO: [AdminIndexPage] 接入 GET /health/ready API 获取各组件真实健康状态
// TODO: [AdminIndexPage] 接入 GET /admin/anomalies API 获取最近异常列表

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
  const [backendVersion, setBackendVersion] = useState<VersionInfo | null>(null)

  // 获取后端版本信息
  useEffect(() => {
    getVersion().then(setBackendVersion).catch(() => {})
  }, [])

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
  // TODO: [AdminIndexPage] StrategyRun 无 processed/success/failed 字段，需后端扩展 API 返回运行统计
  const hasDsaRun = !!latestDsaRun
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
          {/* TODO: [AdminIndexPage] 需 GET /admin/metrics/active-users API 获取今日活跃数 */}
          <div className="kpi-foot">今日活跃 暂无数据</div>
        </div>
        {/* KPI 2：去重监控股票数 */}
        {/* TODO: [AdminIndexPage] 需 GET /admin/metrics/monitored-stocks API 获取去重监控股票数 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">去重监控股票</div>
          <div className="kpi-value">暂无数据</div>
          <div className="kpi-foot">暂无数据</div>
        </div>
        {/* KPI 3：最近一分钟处理数 */}
        {/* TODO: [AdminIndexPage] 需 GET /admin/metrics/throughput API 获取最近一分钟处理数与成功率 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">最近一分钟处理</div>
          <div className="kpi-value">暂无数据</div>
          <div className="kpi-foot">暂无数据</div>
        </div>
        {/* KPI 4：今日投递成功率 */}
        {/* TODO: [AdminIndexPage] 需 GET /admin/metrics/delivery-rate API 获取今日投递成功率 */}
        <div className="card kpi-card admin-accent">
          <div className="kpi-label">今日投递成功率</div>
          <div className="kpi-value">暂无数据</div>
          <div className="kpi-foot">暂无数据</div>
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
            {dsaLoading ? (
              <div className="notice">加载中…</div>
            ) : dsaError ? (
              <div className="notice error">DSA 运行记录加载失败</div>
            ) : !hasDsaRun ? (
              <div className="notice">今日尚未运行</div>
            ) : (
              <>
                {/* TODO: [AdminIndexPage] 需后端扩展 StrategyRun 返回 processed/success/failed 统计 */}
                <div className="toggle-row">
                  <span>处理股票</span>
                  <b className="num">暂无数据</b>
                </div>
                <div className="toggle-row">
                  <span>成功</span>
                  <b className="num">暂无数据</b>
                </div>
                <div className="toggle-row">
                  <span>失败</span>
                  <b className="num">暂无数据</b>
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
        {/* TODO: [AdminIndexPage] 需 GET /admin/metrics/queue-backlog API 获取各队列积压数 */}
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
              <b className="num">暂无数据</b>
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
              <b className="num">暂无数据</b>
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
            <div className="notice">暂无数据</div>
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
