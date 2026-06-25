// 管理后台首页（受保护路由，admin only）
// 对应原型：admin/index.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 卡片组（admin-accent 紫色左边框，4 列）：有效用户 / 去重监控股票 / 最近一分钟处理 / 评估成功率
// 3. split-2 布局：盘中监控 / 盘后任务（后端 monitor_runtime / after_close_pipeline 直出，前端不再组合判定）
// 4. split-3 布局：监控吞吐折线图（Canvas）/ DSA 最近运行 / 队列与任务
// 5. split-2 布局：服务健康状态表 / 最近异常列表
//
// 依赖 hooks：
// - useAdminSystemOverview：获取系统概览数据（活跃用户/监控标的/评估统计/盘中监控/盘后任务）
// - useStrategies：获取策略目录（策略数，显示在页头描述）

import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useStrategies, useAdminSystemOverview } from '@/hooks/useApi'
import { getVersion, type VersionInfo } from '@/api/endpoints'
import { formatShanghaiTime } from '@/utils/datetime'

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

// ===== 状态标签映射（后端枚举 → 中文，前端仅做展示映射，不做判定）=====

/** [盘中监控] - 描述: monitor_runtime.status 中文标签 */
function monitorStatusLabel(status: string | undefined): string {
  switch (status) {
    case 'RUNNING':
      return '运行中'
    case 'IDLE_EXPECTED':
      return '空闲（预期）'
    case 'SESSION_COMPLETED':
      return '已收盘，下午盘已完成'
    case 'DELAYED':
      return '数据延迟'
    case 'FAILED':
      return '下午盘失败'
    case 'WORKER_OFFLINE':
      return 'Worker 离线'
    case 'NOT_APPLICABLE':
      return '非交易日'
    default:
      return '-'
  }
}

/** [盘后任务] - 描述: after_close_pipeline.status 中文标签 */
function pipelineStatusLabel(status: string | undefined): string {
  switch (status) {
    case 'NOT_STARTED':
      return '未开始'
    case 'BARS_RUNNING':
      return '行情更新中'
    case 'BARS_FAILED':
      return '行情更新失败'
    case 'WAITING_DSA':
      return '等待DSA'
    case 'DSA_QUEUED':
      return 'DSA已排队'
    case 'DSA_RUNNING':
      return 'DSA计算中'
    case 'DSA_COMPLETED':
      return 'DSA已完成'
    case 'PUBLISHED':
      return '已发布'
    case 'DSA_FAILED':
      return 'DSA失败'
    case 'STALE':
      return '状态过期'
    default:
      return '-'
  }
}

/** [盘中监控] - 描述: market_session 中文标签 */
function marketSessionLabel(session: string | undefined): string {
  switch (session) {
    case 'NON_TRADING_DAY':
      return '非交易日'
    case 'PRE_OPEN':
      return '盘前'
    case 'MORNING_SESSION':
      return '上午盘'
    case 'LUNCH_BREAK':
      return '午间休市'
    case 'AFTERNOON_SESSION':
      return '下午盘'
    case 'MARKET_CLOSED':
      return '已收盘'
    default:
      return '-'
  }
}

/** [盘中监控] - 描述: session_job_status 中文标签 */
function sessionJobLabel(status: string | null | undefined): string {
  switch (status) {
    case 'running':
      return '运行中'
    case 'succeeded':
      return '成功'
    case 'failed':
      return '失败'
    default:
      return '无记录'
  }
}

/** [盘中监控] - 描述: worker 心跳状态（heartbeat_age_seconds < 90s 为正常） */
function workerHeartbeatLabel(heartbeatAgeSeconds: number | null | undefined): string {
  if (heartbeatAgeSeconds == null) return '离线'
  return heartbeatAgeSeconds < 90 ? '正常' : '离线'
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

  // [系统概览] - 描述: 盘中/盘后状态完全由后端 monitor_runtime / after_close_pipeline 直出，前端不再组合判定

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

      {/* 运行状态：盘中监控 / 盘后任务（后端 monitor_runtime / after_close_pipeline 直出）*/}
      <div className="grid split-2 section-gap">
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">盘中监控</div>
              <div className="card-sub">后端 monitor_runtime 直出</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>状态</span>
              <b>
                <span
                  className={`status-pill ${
                    overview?.monitor_runtime?.status === 'RUNNING' ? 'ok' : 'off'
                  }`}
                >
                  {overviewLoading ? '-' : monitorStatusLabel(overview?.monitor_runtime?.status)}
                </span>
              </b>
            </div>
            <div className="toggle-row">
              <span>市场时段</span>
              <b className="num">
                {overviewLoading ? '-' : marketSessionLabel(overview?.market_session)}
              </b>
            </div>
            <div className="toggle-row">
              <span>Worker 心跳</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : workerHeartbeatLabel(overview?.monitor_runtime?.heartbeat_age_seconds)}
              </b>
            </div>
            <div className="toggle-row">
              <span>session_job</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : sessionJobLabel(overview?.monitor_runtime?.session_job_status)}
              </b>
            </div>
            <div className="toggle-row">
              <span>最后计算时间</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : formatShanghaiTime(overview?.monitor_runtime?.last_cycle_at)}
              </b>
            </div>
            <div className="toggle-row">
              <span>最后源Bar时间</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : formatShanghaiTime(overview?.monitor_runtime?.last_source_bar_time)}
              </b>
            </div>
            <div className="toggle-row">
              <span>已评估 / 失败</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : `${overview?.monitor_runtime?.evaluated_count ?? 0} / ${
                      overview?.monitor_runtime?.failed_count ?? 0
                    }`}
              </b>
            </div>
          </div>
        </section>
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">盘后任务</div>
              <div className="card-sub">后端 after_close_pipeline 直出</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>状态</span>
              <b>
                <span
                  className={`status-pill ${
                    overview?.after_close_pipeline?.status === 'PUBLISHED' ? 'ok' : 'off'
                  }`}
                >
                  {overviewLoading
                    ? '-'
                    : pipelineStatusLabel(overview?.after_close_pipeline?.status)}
                </span>
              </b>
            </div>
            <div className="toggle-row">
              <span>计划启动</span>
              <b className="num">16:00</b>
            </div>
            <div className="toggle-row">
              <span>bars_scheduler</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : overview?.after_close_pipeline?.bars_job?.status ?? '今日尚未运行'}
              </b>
            </div>
            <div className="toggle-row">
              <span>DSA 状态</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : overview?.after_close_pipeline?.dsa_run?.status ?? '等待行情更新'}
              </b>
            </div>
            {/* [盘后任务] - 描述: DSA 失败时展示错误详情，便于管理员排查 */}
            {(overview?.after_close_pipeline?.dsa_run?.status === 'failed' ||
              overview?.after_close_pipeline?.status === 'DSA_FAILED') &&
              overview?.after_close_pipeline?.dsa_run && (
                <>
                  <div className="toggle-row">
                    <span>失败阶段</span>
                    <b className="num">
                      {overview.after_close_pipeline.dsa_run.failure_stage ?? '-'}
                    </b>
                  </div>
                  <div className="toggle-row">
                    <span>错误码</span>
                    <b className="num">
                      {overview.after_close_pipeline.dsa_run.error_code ?? '-'}
                    </b>
                  </div>
                  <div className="toggle-row">
                    <span>错误摘要</span>
                    <b className="num">
                      {overview.after_close_pipeline.dsa_run.error_message ?? '-'}
                    </b>
                  </div>
                  <div className="toggle-row">
                    <span>业务日期</span>
                    <b className="num">
                      {overview.after_close_pipeline.dsa_run.trade_date ?? '-'}
                    </b>
                  </div>
                  <div className="toggle-row">
                    <span>run_type</span>
                    <b className="num">
                      {overview.after_close_pipeline.dsa_run.run_type ?? '-'}
                    </b>
                  </div>
                  <div className="toggle-row">
                    <span>attempt_no</span>
                    <b className="num">
                      {overview.after_close_pipeline.dsa_run.attempt_no ?? '-'}
                    </b>
                  </div>
                </>
              )}
          </div>
        </section>
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
                  <span>股票计算失败数</span>
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
