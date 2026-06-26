// 管理后台首页（受保护路由，admin only）
// 对应原型：admin/index.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. 四层布局（advice.md 第九节）：
//    - 第一层：今日状态摘要（市场状态 / 盘中监控 / 今日数据，三张等宽卡 .split-3-even）
//    - 第二层：今日盘后处理（AfterClosePipelineCard 占整行，含七阶段/覆盖率/选股/Worker/心跳/日志/按钮）
//    - 第三层：基础设施两列（Worker与调度 / 消息队列与飞书）
//    - 第四层：异常与历史（最近失败+最近异常 / 高级技术折叠含版本信息+DSA详情）
// 3. 已删除监控吞吐空图（无真实数据，待后端有历史趋势再恢复）
// 4. 移动端响应式：所有多列布局在窄屏自动降为单列
//
// 依赖 hooks：
// - useAdminSystemOverview：获取系统概览数据（活跃用户/监控标的/评估统计/盘中监控/盘后任务/数据新鲜度/服务健康/最近任务/最近异常）
// - useStrategies：获取策略目录（策略数，显示在页头描述）

import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useStrategies, useAdminSystemOverview } from '@/hooks/useApi'
import { getVersion, type VersionInfo } from '@/api/endpoints'
import { formatShanghaiTime } from '@/utils/datetime'
import { AfterClosePipelineCard } from '@/features/after-close-pipeline/AfterClosePipelineCard'

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

/** 格式化运行时间（ISO → 上海时区 "yyyy/MM/dd HH:mm:ss"，无效返回"今日尚未运行"）*/
function formatRunTime(iso: string | null | undefined): string {
  if (!iso) return '今日尚未运行'
  const formatted = formatShanghaiTime(iso)
  return formatted === '-' ? '今日尚未运行' : formatted
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

  // KPI 2：去重监控股票数（并入盘中监控卡）
  const distinctInstruments = overview?.distinct_monitored_instruments ?? 0

  // KPI 3：最近一分钟处理数（并入盘中监控卡）
  const evalsLastMinute = overview?.evaluations_last_minute ?? 0

  // KPI 4：评估成功率（并入盘中监控卡）
  const evalSuccessRate = overview?.evaluations_success_rate ?? 0
  const evalSuccessRatePct = evalSuccessRate > 0 ? `${(evalSuccessRate * 100).toFixed(1)}%` : '暂无数据'

  // DSA 最近运行（从 system-overview 获取，并入第四层高级技术折叠）
  const latestSelectorRun = overview?.latest_selector_run
  const hasDsaRun = !!latestSelectorRun
  const dsaDuration = hasDsaRun
    ? formatDuration(latestSelectorRun!.started_at, latestSelectorRun!.finished_at)
    : '-'
  const dsaRunTime = formatRunTime(latestSelectorRun?.finished_at)

  // 队列与任务（并入第三层消息队列与飞书卡）
  const queueBacklog = overview?.queue_backlog ?? 0
  const failedRetryCount = overview?.failed_retry_count ?? 0
  const notificationDeliveryRate = overview?.notification_delivery_rate ?? 0
  const notificationDeliveryRatePct =
    notificationDeliveryRate > 0
      ? `${(notificationDeliveryRate * 100).toFixed(1)}%`
      : '暂无数据'

  // 服务健康（并入第三层 Worker与调度卡）
  const workerHealth = overview?.worker_health ?? 'unknown'
  const schedulerHealth = overview?.scheduler_health ?? 'unknown'
  const recentSchedulerJobs = overview?.recent_scheduler_jobs ?? []
  const recentAnomalies = overview?.recent_anomalies ?? []

  // 数据新鲜度（并入第一层今日数据卡）
  const dataFreshness = overview?.after_close_pipeline?.data_freshness
  const barsFreshness = dataFreshness?.bars
  const strategyFreshness = dataFreshness?.strategy

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

      {/* ===== 第一层：今日状态摘要（三张等宽卡 .split-3-even）===== */}
      <div className="grid split-3-even">
        {/* 市场状态：market_session / business_date / server_time / 有效用户 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">市场状态</div>
              <div className="card-sub">后端 business_date / market_session 直出</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>市场时段</span>
              <b className="num">
                {overviewLoading ? '-' : marketSessionLabel(overview?.market_session)}
              </b>
            </div>
            <div className="toggle-row">
              <span>业务日期</span>
              <b className="num">{overviewLoading ? '-' : (overview?.business_date ?? '-')}</b>
            </div>
            <div className="toggle-row">
              <span>服务端时间</span>
              <b className="num">
                {overviewLoading ? '-' : formatShanghaiTime(overview?.server_time)}
              </b>
            </div>
            <div className="toggle-row">
              <span>有效用户</span>
              <b className="num">{overviewLoading ? '-' : activeUsers}</b>
            </div>
          </div>
        </section>

        {/* 盘中监控：monitor_runtime 状态 + KPI（去重监控股票/最近一分钟处理/评估成功率）*/}
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
            <div className="toggle-row">
              <span>去重监控股票</span>
              <b className="num">{overviewLoading ? '-' : distinctInstruments}</b>
            </div>
            <div className="toggle-row">
              <span>最近一分钟处理</span>
              <b className="num">{overviewLoading ? '-' : evalsLastMinute}</b>
            </div>
            <div className="toggle-row">
              <span>评估成功率</span>
              <b className="num">{overviewLoading ? '-' : evalSuccessRatePct}</b>
            </div>
          </div>
        </section>

        {/* 今日数据：行情更新至 / 日线覆盖率 / 选股发布至 / 是否落后（来自 data_freshness）*/}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">今日数据</div>
              <div className="card-sub">行情与选股新鲜度</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>行情更新至</span>
              <b className="num">
                {overviewLoading ? '-' : (barsFreshness?.latest_daily_trade_date ?? '-')}
              </b>
            </div>
            <div className="toggle-row">
              <span>日线覆盖率</span>
              <b className="num">
                {overviewLoading || barsFreshness?.daily_coverage == null
                  ? '-'
                  : `${(barsFreshness.daily_coverage * 100).toFixed(1)}%`}
              </b>
            </div>
            <div className="toggle-row">
              <span>选股发布至</span>
              <b className="num">
                {overviewLoading ? '-' : (strategyFreshness?.latest_published_trade_date ?? '-')}
              </b>
            </div>
            <div className="toggle-row">
              <span>是否落后</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : barsFreshness?.is_behind_latest_trade_date
                    ? '是（落后最近交易日）'
                    : '否'}
              </b>
            </div>
            <div className="toggle-row">
              <span>最新 15m Bar</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : formatShanghaiTime(barsFreshness?.latest_15m_bar_time)}
              </b>
            </div>
            <div className="toggle-row">
              <span>最新 60m Bar</span>
              <b className="num">
                {overviewLoading
                  ? '-'
                  : formatShanghaiTime(barsFreshness?.latest_60m_bar_time)}
              </b>
            </div>
          </div>
        </section>
      </div>

      {/* ===== 第二层：今日盘后处理（占整行，AfterClosePipelineCard 含七阶段/覆盖率/选股/Worker/心跳/日志/按钮）===== */}
      <div className="grid section-gap">
        <AfterClosePipelineCard
          pipeline={overview?.after_close_pipeline ?? null}
          jobRunId={overview?.after_close_pipeline?.job_run_id ?? null}
          tradeDate={overview?.business_date ?? undefined}
          loading={overviewLoading}
        />
      </div>

      {/* ===== 第三层：基础设施两列（Worker与调度 / 消息队列与飞书）===== */}
      <div className="grid split-2 section-gap">
        {/* Worker与调度状态：worker_health / scheduler_health / 最近定时任务摘要 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">Worker与调度</div>
              <div className="card-sub">管理员无需进入服务器即可排查</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>Worker</span>
              <b className="num">{overviewLoading ? '-' : workerHealth}</b>
            </div>
            <div className="toggle-row">
              <span>Scheduler</span>
              <b className="num">{overviewLoading ? '-' : schedulerHealth}</b>
            </div>
            <div className="toggle-row">
              <span>最近任务数</span>
              <b className="num">{overviewLoading ? '-' : recentSchedulerJobs.length}</b>
            </div>
            {recentSchedulerJobs.length > 0 && (
              <div className="recent-jobs-list">
                {recentSchedulerJobs.slice(0, 5).map((job, i) => (
                  <div key={`${job.job_name}-${i}`} className="toggle-row">
                    <span>{job.job_name}</span>
                    <b className="num">
                      {job.status}
                      {job.business_date ? ` · ${job.business_date}` : ''}
                    </b>
                  </div>
                ))}
              </div>
            )}
            <Link className="btn small card-body-action" to="/admin/jobs">
              任务与事件 →
            </Link>
          </div>
        </section>

        {/* 消息队列与飞书状态：queue_backlog / failed_retry_count / notification_delivery_rate */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">消息队列与飞书</div>
              <div className="card-sub">队列积压与通知投递</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>监控计算积压</span>
              <b className="num">{overviewLoading ? '-' : queueBacklog}</b>
            </div>
            <div className="toggle-row">
              <span>失败重试</span>
              <b className="num">{overviewLoading ? '-' : failedRetryCount}</b>
            </div>
            <div className="toggle-row">
              <span>通知投递率</span>
              <b className="num">{overviewLoading ? '-' : notificationDeliveryRatePct}</b>
            </div>
            <div className="toggle-row">
              <span>事件分发</span>
              <b className="num">暂无数据</b>
            </div>
            <Link className="btn small card-body-action" to="/admin/jobs">
              任务与事件 →
            </Link>
          </div>
        </section>
      </div>

      {/* ===== 第四层：异常与历史（最近失败+最近异常 / 高级技术折叠）===== */}
      <div className="grid split-2 section-gap">
        {/* 最近失败任务 + 最近异常 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">异常与历史</div>
              <div className="card-sub">最近失败任务与最近异常</div>
            </div>
          </div>
          <div className="card-body">
            <div className="detail-title">最近失败任务</div>
            {recentSchedulerJobs.filter((j) => j.status === 'failed').length > 0 ? (
              recentSchedulerJobs
                .filter((j) => j.status === 'failed')
                .slice(0, 5)
                .map((job, i) => (
                  <div key={`failed-${job.job_name}-${i}`} className="toggle-row">
                    <span>{job.job_name}</span>
                    <b className="num">
                      {job.error_message ?? job.status}
                      {job.business_date ? ` · ${job.business_date}` : ''}
                    </b>
                  </div>
                ))
            ) : (
              <div className="notice">暂无失败任务</div>
            )}
            <div className="detail-title" style={{ marginTop: '12px' }}>
              最近异常
            </div>
            {recentAnomalies.length > 0 ? (
              <div className="notice">{recentAnomalies.length} 条异常记录（详情见日志）</div>
            ) : (
              <div className="notice">暂无数据</div>
            )}
          </div>
        </section>

        {/* 高级技术折叠：版本信息 + DSA 最近运行详情 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">高级技术</div>
              <div className="card-sub">展开查看版本信息与 DSA 运行详情</div>
            </div>
          </div>
          <div className="card-body">
            <details className="advanced-tech-collapse">
              <summary>版本信息</summary>
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
            </details>
            <details className="advanced-tech-collapse">
              <summary>DSA 最近运行详情</summary>
              {overviewLoading ? (
                <div className="notice">加载中…</div>
              ) : !hasDsaRun ? (
                <div className="notice">今日尚未运行</div>
              ) : (
                <>
                  <div className="toggle-row">
                    <span>完成时间</span>
                    <b className="num">{dsaRunTime}</b>
                  </div>
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
            </details>
          </div>
        </section>
      </div>
    </>
  )
}
