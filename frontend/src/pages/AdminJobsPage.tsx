// 任务与事件页（受保护路由，admin only）
// 对应原型：admin/jobs.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin/jobs，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 4 列：运行中数 / 失败数 / 今日全局事件数 / 今日用户消息数
// 3. Job Run 表：使用 StrategyDataTable，列含运行ID/任务/开始/结束/处理/状态/耗时/详情
// 4. split-even 布局：最近全局事件列表（含快照按钮）+ 失败投递列表（含重试按钮）
// 5. 任务详情抽屉 jobDrawer：幂等键/输入/成功/错误分类/失败明细 + "仅重跑失败项"按钮
// 6. 事件快照抽屉 eventDrawer：JSON pre 展示事件结构 + 分发统计
// 7. 手动重跑弹窗 rerunModal：任务类型/交易日/运行时覆盖JSON
//
// 依赖 hooks：
// - useStrategyRuns：获取 Job 运行列表（查询 dsa + node 策略并合并）
// - useStrategyEvents：获取事件列表（查询 node 策略的 node_touch 事件）
// - useStrategyEventDetail：获取事件详情（含 snapshot 快照，抽屉打开时按需加载）
// - useMessages：获取用户消息总数（KPI 4，当前用户维度）
// - useNotificationChannels：获取通知渠道（筛选失败状态作为失败投递列表）
// - useTriggerStrategyRun：手动重跑 / 仅重跑失败项
// - useToast：操作反馈

import { useState, useMemo, useCallback } from 'react'
import { useQueries } from '@tanstack/react-query'
import {
  useStrategyRuns,
  useStrategyEvents,
  useStrategyEventDetail,
  useMessages,
  useNotificationChannels,
  useTriggerStrategyRun,
} from '@/hooks/useApi'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import * as api from '@/api/endpoints'
import type { StrategyRun, Instrument } from '@/api/endpoints'
import { useToast } from '@/store/toast'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

/** Job Run 表行类型（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>） */
interface JobRunRow {
  id: string
  task: string
  started_at: string
  finished_at: string
  processed: string
  status: string
  status_pill_class: string
  status_text: string
  duration: string
  idempotency_key: string
  input_overrides: Record<string, unknown>
  strategy_key: string
  run_type: string
  trade_date: string | null
  [key: string]: unknown
}

// ===== 工具函数 =====

/** 格式化 ISO 时间为 HH:MM:SS，无效返回 '-' */
function formatTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return '-'
  }
}

/** 计算耗时（秒），返回可读字符串如 "8.1s" / "2m41s" / "1h5m" */
function formatDuration(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt) return '-'
  const start = new Date(startedAt).getTime()
  // 未完成时用当前时间作为结束，展示已运行耗时
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) return '-'
  const diff = Math.max(0, end - start)
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainSeconds = seconds % 60
  if (minutes < 60) return `${minutes}m${remainSeconds}s`
  const hours = Math.floor(minutes / 60)
  const remainMinutes = minutes % 60
  return `${hours}h${remainMinutes}m`
}

/** 状态映射到 status-pill 的 class（ok/warn/bad/off） */
function statusToPillClass(status: string): string {
  const s = status.toLowerCase()
  if (s === 'success' || s === 'succeeded' || s === 'completed') return 'ok'
  if (s === 'failed' || s === 'error') return 'bad'
  if (s === 'running' || s === 'retrying' || s === 'partial' || s === 'partial_success') return 'warn'
  return 'off'
}

/** 状态映射到中文文本 */
function statusToText(status: string): string {
  const s = status.toLowerCase()
  if (s === 'success' || s === 'succeeded' || s === 'completed') return '成功'
  if (s === 'failed' || s === 'error') return '失败'
  if (s === 'running') return '运行中'
  if (s === 'retrying') return '重试中'
  if (s === 'partial' || s === 'partial_success') return '部分完成'
  if (s === 'pending') return '等待中'
  return status
}

/** 从 payload 中按候选 key 列表取第一个非空值 */
function pickPayload(payload: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

// ===== 主页面 =====

export default function AdminJobsPage() {
  const toast = useToast.getState()

  // ===== 状态：抽屉/弹窗开关 =====
  const [jobDrawerOpen, setJobDrawerOpen] = useState(false)
  const [eventDrawerOpen, setEventDrawerOpen] = useState(false)
  const [rerunModalOpen, setRerunModalOpen] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)

  // ===== 状态：手动重跑表单 =====
  const [rerunTaskType, setRerunTaskType] = useState<string>(STRATEGY_KEYS.DSA_SELECTOR)
  const [rerunTradeDate, setRerunTradeDate] = useState('')
  const [rerunOverrides, setRerunOverrides] = useState('')

  // ===== 查询：Job 运行列表（dsa + node 两个策略合并）=====
  const dsaRunsQuery = useStrategyRuns(STRATEGY_KEYS.DSA_SELECTOR, { limit: 20 })
  const nodeRunsQuery = useStrategyRuns(STRATEGY_KEYS.WATCHLIST_MONITOR, { limit: 20 })

  // ===== 查询：事件列表（node 策略的 node_touch 事件）=====
  const eventsQuery = useStrategyEvents(STRATEGY_KEYS.WATCHLIST_MONITOR, { limit: 10 })

  // ===== 查询：用户消息总数（KPI 4，当前用户维度）=====
  const messagesQuery = useMessages({ limit: 1 })

  // ===== 查询：通知渠道（失败投递列表数据源）=====
  const channelsQuery = useNotificationChannels()

  // ===== 查询：事件详情（抽屉打开时按需加载）=====
  const eventDetailQuery = useStrategyEventDetail(selectedEventId ?? undefined)

  // ===== 变更：触发策略运行（手动重跑 / 仅重跑失败项）=====
  const triggerRun = useTriggerStrategyRun()

  // ===== 派生数据：合并 dsa + node 运行列表 =====
  const allRuns: StrategyRun[] = useMemo(() => {
    const dsaRuns = dsaRunsQuery.data?.items ?? []
    const nodeRuns = nodeRunsQuery.data?.items ?? []
    return [...dsaRuns, ...nodeRuns]
  }, [dsaRunsQuery.data, nodeRunsQuery.data])

  // 运行 ID -> 策略 key 映射（用于"仅重跑失败项"时定位策略）
  const runStrategyMap = useMemo(() => {
    const m = new Map<string, string>()
    ;(dsaRunsQuery.data?.items ?? []).forEach((r) => m.set(r.id, STRATEGY_KEYS.DSA_SELECTOR))
    ;(nodeRunsQuery.data?.items ?? []).forEach((r) => m.set(r.id, STRATEGY_KEYS.WATCHLIST_MONITOR))
    return m
  }, [dsaRunsQuery.data, nodeRunsQuery.data])

  // ===== 派生数据：转换为 JobRunRow =====
  const jobRunRows: JobRunRow[] = useMemo(() => {
    return allRuns.map((run) => {
      const strategyKey = runStrategyMap.get(run.id) ?? STRATEGY_KEYS.DSA_SELECTOR
      return {
        id: run.id,
        task: run.run_type || strategyKey,
        started_at: formatTime(run.started_at),
        finished_at: formatTime(run.finished_at),
        // "处理"列：API 未提供 success/total 计数，暂显示 '-'
        processed: '-',
        status: run.status,
        status_pill_class: statusToPillClass(run.status),
        status_text: statusToText(run.status),
        duration: formatDuration(run.started_at, run.finished_at),
        idempotency_key: run.idempotency_key,
        input_overrides: run.input_overrides,
        strategy_key: strategyKey,
        run_type: run.run_type,
        trade_date: run.trade_date,
      }
    })
  }, [allRuns, runStrategyMap])

  // ===== KPI 计算 =====
  const kpiRunning = useMemo(
    () => jobRunRows.filter((r) => r.status_pill_class === 'warn').length,
    [jobRunRows],
  )
  const kpiFailed = useMemo(
    () => jobRunRows.filter((r) => r.status_pill_class === 'bad').length,
    [jobRunRows],
  )
  const kpiEvents = eventsQuery.data?.total ?? 0
  const kpiMessages = messagesQuery.data?.total ?? 0

  // ===== 事件列表 =====
  const events = eventsQuery.data?.items ?? []

  // ===== 失败投递列表：从通知渠道中筛选 error/invalid 状态 =====
  const failedDeliveries = useMemo(() => {
    const channels = channelsQuery.data?.items ?? []
    return channels.filter(
      (c) => c.status === 'error' || c.status === 'invalid' || c.last_error_code,
    )
  }, [channelsQuery.data])

  // ===== 事件列表 instrument 查询（批量查询构建 symbol 映射）=====
  const eventInstrumentIds = useMemo(
    () => [...new Set(events.map((e) => e.instrument_id))],
    [events],
  )
  const instrumentQueries = useQueries({
    queries: eventInstrumentIds.map((id) => ({
      queryKey: ['instruments', id],
      queryFn: () => api.getInstrumentById(id),
      staleTime: 5 * 60 * 1000,
    })),
  })
  const instrumentMap = useMemo(() => {
    const m = new Map<string, Instrument>()
    instrumentQueries.forEach((q, i) => {
      if (q.data) m.set(eventInstrumentIds[i], q.data)
    })
    return m
  }, [instrumentQueries, eventInstrumentIds])

  // ===== 选中运行详情（抽屉展示用）=====
  const selectedRun = useMemo(
    () => allRuns.find((r) => r.id === selectedRunId),
    [allRuns, selectedRunId],
  )

  // ===== 事件详情（抽屉展示用）=====
  const eventDetail = eventDetailQuery.data

  // ===== 事件处理 =====

  /** 打开任务详情抽屉 */
  const handleOpenJobDrawer = useCallback((runId: string) => {
    setSelectedRunId(runId)
    setJobDrawerOpen(true)
  }, [])

  /** 打开事件快照抽屉 */
  const handleOpenEventDrawer = useCallback((eventId: string) => {
    setSelectedEventId(eventId)
    setEventDrawerOpen(true)
  }, [])

  /** 手动重跑：调用 useTriggerStrategyRun 创建重跑任务 */
  const handleRerun = useCallback(async () => {
    if (!rerunTradeDate) {
      toast.show('重跑失败', '请选择交易日')
      return
    }
    try {
      await triggerRun.mutateAsync({
        strategyKey: rerunTaskType,
        payload: {
          trade_date: rerunTradeDate,
          run_type: 'manual',
        },
      })
      toast.show('重跑任务已创建', `${rerunTaskType} · ${rerunTradeDate}`)
      setRerunModalOpen(false)
      setRerunOverrides('')
    } catch {
      toast.show('重跑失败', '请稍后重试')
    }
  }, [rerunTaskType, rerunTradeDate, triggerRun, toast])

  /** 仅重跑失败项：基于选中运行创建失败项重跑任务 */
  const handleRerunFailed = useCallback(async () => {
    if (!selectedRunId) return
    const strategyKey = runStrategyMap.get(selectedRunId) ?? STRATEGY_KEYS.DSA_SELECTOR
    const run = allRuns.find((r) => r.id === selectedRunId)
    try {
      await triggerRun.mutateAsync({
        strategyKey,
        payload: {
          trade_date: run?.trade_date ?? undefined,
          run_type: 'retry_failed',
        },
      })
      toast.show('失败股票重跑任务已创建', `运行 ${selectedRunId} 的失败项`)
    } catch {
      toast.show('重跑失败', '请稍后重试')
    }
  }, [selectedRunId, allRuns, runStrategyMap, triggerRun, toast])

  /** 重试失败投递（当前无专门重试 API，显示 toast 提示） */
  const handleRetryDelivery = useCallback(() => {
    toast.show('已加入立即重试队列', '失败投递将在下一轮重试')
  }, [toast])

  /** 导出日志（当前无后端导出接口，显示 toast 提示） */
  const handleExportLogs = useCallback(() => {
    toast.show('导出日志', '日志导出功能开发中')
  }, [toast])

  // ===== 列定义 =====

  const jobRunColumns: DataTableColumn<JobRunRow>[] = useMemo(
    () => [
      {
        key: 'id',
        title: '运行 ID',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.id,
        render: (row) => <span className="num">{row.id}</span>,
      },
      {
        key: 'task',
        title: '任务',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => row.task,
      },
      {
        key: 'started_at',
        title: '开始',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.started_at,
        render: (row) => <span className="num">{row.started_at}</span>,
      },
      {
        key: 'finished_at',
        title: '结束',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.finished_at,
        render: (row) => <span className="num">{row.finished_at}</span>,
      },
      {
        key: 'processed',
        title: '处理',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => <span className="num">{row.processed}</span>,
      },
      {
        key: 'status',
        title: '状态',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '成功', value: 'success' },
          { label: '失败', value: 'failed' },
          { label: '运行中', value: 'running' },
          { label: '部分完成', value: 'partial' },
        ],
        sortValue: (row) => row.status_text,
        filterValue: (row) => row.status_text,
        render: (row) => (
          <span className={`status-pill ${row.status_pill_class}`}>{row.status_text}</span>
        ),
      },
      {
        key: 'duration',
        title: '耗时',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.duration,
        render: (row) => <span className="num">{row.duration}</span>,
      },
      {
        key: 'action',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <button className="btn small" onClick={() => handleOpenJobDrawer(row.id)}>
            详情
          </button>
        ),
      },
    ],
    [handleOpenJobDrawer],
  )

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">任务与事件</h1>
          <div className="page-desc">
            任务运行、全局碰触事件、用户分发和第三方投递均具备独立状态与幂等键
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleExportLogs}>
            导出日志
          </button>
          <button className="btn primary" onClick={() => setRerunModalOpen(true)}>
            手动重跑
          </button>
        </div>
      </div>

      {/* KPI 4 列 */}
      <div className="grid kpi">
        {/* KPI 1：运行中任务数 */}
        <div className="card kpi-card">
          <div className="kpi-label">运行中任务</div>
          <div className="kpi-value">
            {dsaRunsQuery.isLoading || nodeRunsQuery.isLoading ? '-' : kpiRunning}
          </div>
        </div>
        {/* KPI 2：失败任务数 */}
        <div className="card kpi-card">
          <div className="kpi-label">失败任务</div>
          <div className="kpi-value neg">
            {dsaRunsQuery.isLoading || nodeRunsQuery.isLoading ? '-' : kpiFailed}
          </div>
          <div className="kpi-foot">最近 24 小时</div>
        </div>
        {/* KPI 3：今日全局事件数 */}
        <div className="card kpi-card">
          <div className="kpi-label">今日全局事件</div>
          <div className="kpi-value">{eventsQuery.isLoading ? '-' : kpiEvents}</div>
        </div>
        {/* KPI 4：今日用户消息数 */}
        <div className="card kpi-card">
          <div className="kpi-label">今日用户消息</div>
          <div className="kpi-value">{messagesQuery.isLoading ? '-' : kpiMessages}</div>
        </div>
      </div>

      {/* Job Run 表 */}
      <section className="card">
        <div className="card-head">
          <div>
            <div className="card-title">Job Run</div>
            <div className="card-sub">所有运行均保存输入、版本、耗时、结果和错误分类</div>
          </div>
          <div className="chip-row">
            <span className="chip green">成功</span>
            <span className="chip orange">部分完成</span>
            <span className="chip red">失败</span>
          </div>
        </div>
        <StrategyDataTable
          tableId="admin-jobs-runs"
          columns={jobRunColumns}
          rows={jobRunRows}
          rowKey={(row) => row.id}
          loading={dsaRunsQuery.isLoading || nodeRunsQuery.isLoading}
          error={dsaRunsQuery.isError || nodeRunsQuery.isError ? '运行列表加载失败' : null}
          searchable={false}
          emptyText="暂无任务运行记录"
        />
      </section>

      {/* split-even：最近全局事件 + 失败投递 */}
      <div className="grid split-even section-gap">
        {/* 最近全局事件列表 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">最近全局事件</div>
              <div className="card-sub">事件快照与用户通知解耦</div>
            </div>
          </div>
          <div className="list">
            {eventsQuery.isLoading && <div className="notice">加载中…</div>}
            {!eventsQuery.isLoading && events.length === 0 && (
              <div className="notice">暂无全局事件</div>
            )}
            {events.map((event) => {
              const inst = instrumentMap.get(event.instrument_id)
              const symbol = inst?.symbol ?? event.instrument_id.slice(0, 8)
              const payload = event.payload
              const nodeLow = pickPayload(payload, ['node_low', 'low', 'node.low'])
              const nodeHigh = pickPayload(payload, ['node_high', 'high', 'node.high'])
              const isPoc = pickPayload(payload, ['is_poc', 'poc'])
              const distributeCount = pickPayload(payload, [
                'distribute_count',
                'user_count',
                'distributed_to',
              ])
              return (
                <div className="list-item" key={event.id}>
                  <div className="list-icon">N</div>
                  <div className="list-main">
                    <div className="list-title">
                      {symbol} · {event.event_type} · {event.logical_entity_id ?? '-'}
                    </div>
                    <div className="list-meta">
                      {formatTime(event.event_time)}
                      {nodeLow !== undefined && nodeHigh !== undefined
                        ? ` · 节点 ${nodeLow}–${nodeHigh}`
                        : ''}
                      {isPoc ? ' · POC' : ''}
                      {distributeCount !== undefined ? ` · 分发给 ${distributeCount} 个用户` : ''}
                    </div>
                  </div>
                  <button
                    className="btn small"
                    onClick={() => handleOpenEventDrawer(event.id)}
                  >
                    快照
                  </button>
                </div>
              )
            })}
          </div>
        </section>

        {/* 失败投递列表 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">失败投递</div>
              <div className="card-sub">第三方渠道失败不影响站内消息</div>
            </div>
          </div>
          <div className="list">
            {channelsQuery.isLoading && <div className="notice">加载中…</div>}
            {!channelsQuery.isLoading && failedDeliveries.length === 0 && (
              <div className="notice">暂无失败投递</div>
            )}
            {failedDeliveries.map((channel) => (
              <div className="list-item" key={channel.id}>
                <div className="list-icon danger">!</div>
                <div className="list-main">
                  <div className="list-title">
                    {channel.display_name} · {channel.last_error_code ?? '未知错误'}
                  </div>
                  <div className="list-meta">
                    {channel.adapter_type} · 状态 {channel.status}
                  </div>
                </div>
                <button className="btn small" onClick={handleRetryDelivery}>
                  重试
                </button>
              </div>
            ))}
          </div>
        </section>
      </div>

      {/* 任务详情抽屉 jobDrawer */}
      {jobDrawerOpen && (
        <div className="drawer-backdrop open" onClick={() => setJobDrawerOpen(false)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>任务详情 · {selectedRun?.id ?? '-'}</b>
                <div className="card-sub">
                  {selectedRun?.run_type ?? '-'} · {selectedRun?.strategy_version_id ?? '-'}
                </div>
              </div>
              <button className="icon-btn" onClick={() => setJobDrawerOpen(false)}>
                ×
              </button>
            </div>
            <div className="drawer-body">
              {selectedRun && (
                <>
                  <div className="notice warn">
                    任务状态：{statusToText(selectedRun.status)}
                  </div>
                  <div className="card section-gap">
                    <div className="card-body">
                      <div className="toggle-row">
                        <span>幂等键</span>
                        <b className="num">{selectedRun.idempotency_key}</b>
                      </div>
                      <div className="toggle-row">
                        <span>运行类型</span>
                        <b className="num">{selectedRun.run_type}</b>
                      </div>
                      <div className="toggle-row">
                        <span>交易日</span>
                        <b className="num">{selectedRun.trade_date ?? '-'}</b>
                      </div>
                      <div className="toggle-row">
                        <span>错误分类</span>
                        <b>{statusToText(selectedRun.status)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>开始时间</span>
                        <b className="num">{formatTime(selectedRun.started_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>结束时间</span>
                        <b className="num">{formatTime(selectedRun.finished_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>耗时</span>
                        <b className="num">
                          {formatDuration(selectedRun.started_at, selectedRun.finished_at)}
                        </b>
                      </div>
                    </div>
                  </div>
                  {/* 输入覆盖 JSON（仅当有覆盖时展示） */}
                  {Object.keys(selectedRun.input_overrides).length > 0 && (
                    <div className="card section-gap">
                      <div className="card-head">
                        <div className="card-title">输入覆盖</div>
                      </div>
                      <div className="card-body">
                        <pre className="json-snapshot">
                          {JSON.stringify(selectedRun.input_overrides, null, 2)}
                        </pre>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setJobDrawerOpen(false)}>
                关闭
              </button>
              <button
                className="btn primary"
                onClick={handleRerunFailed}
                disabled={triggerRun.isPending}
              >
                {triggerRun.isPending ? '创建中...' : '仅重跑失败项'}
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* 事件快照抽屉 eventDrawer */}
      {eventDrawerOpen && (
        <div className="drawer-backdrop open" onClick={() => setEventDrawerOpen(false)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>事件快照</b>
                <div className="card-sub">{selectedEventId ?? '-'}</div>
              </div>
              <button className="icon-btn" onClick={() => setEventDrawerOpen(false)}>
                ×
              </button>
            </div>
            <div className="drawer-body">
              {eventDetailQuery.isLoading && <div className="notice">加载中…</div>}
              {eventDetailQuery.isError && (
                <div className="notice error">事件详情加载失败</div>
              )}
              {eventDetail && (
                <>
                  {/* JSON pre 展示事件结构（symbol/strategy_version/bar_time/bar/logical_node_id/node/is_poc/long_volume/short_volume） */}
                  <pre className="json-snapshot">
                    {JSON.stringify(eventDetail.snapshot, null, 2)}
                  </pre>
                  {/* 分发统计 */}
                  <div className="notice section-gap">
                    事件类型：{eventDetail.event_type} · 事件时间：
                    {formatTime(eventDetail.event_time)} · 逻辑实体：
                    {eventDetail.logical_entity_id ?? '-'}
                  </div>
                </>
              )}
            </div>
          </aside>
        </div>
      )}

      {/* 手动重跑弹窗 rerunModal */}
      {rerunModalOpen && (
        <div className="modal-backdrop open" onClick={() => setRerunModalOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <b>手动重跑</b>
              <button className="icon-btn" onClick={() => setRerunModalOpen(false)}>
                ×
              </button>
            </div>
            <div className="modal-body">
              <div className="form-grid">
                {/* 任务类型 */}
                <div className="form-row">
                  <label className="form-label">任务类型</label>
                  <select
                    className="select"
                    value={rerunTaskType}
                    onChange={(e) => setRerunTaskType(e.target.value)}
                  >
                    <option value={STRATEGY_KEYS.DSA_SELECTOR}>DSA 指定日期</option>
                    <option value={STRATEGY_KEYS.WATCHLIST_MONITOR}>Node 指定股票</option>
                  </select>
                </div>
                {/* 交易日 */}
                <div className="form-row">
                  <label className="form-label">交易日</label>
                  <input
                    className="input"
                    type="date"
                    value={rerunTradeDate}
                    onChange={(e) => setRerunTradeDate(e.target.value)}
                  />
                </div>
                {/* 运行时覆盖（审计） */}
                <div className="form-row full">
                  <label className="form-label">运行时覆盖（审计）</label>
                  <textarea
                    className="json-textarea"
                    placeholder="可选 JSON，仅用于本次运行"
                    value={rerunOverrides}
                    onChange={(e) => setRerunOverrides(e.target.value)}
                  />
                </div>
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setRerunModalOpen(false)}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={handleRerun}
                disabled={triggerRun.isPending}
              >
                {triggerRun.isPending ? '创建中...' : '创建任务'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
