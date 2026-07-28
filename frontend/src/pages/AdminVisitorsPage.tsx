// [Gate5] 访问统计页（admin only）
// 对应后端：GET /admin/visitors（读取 GoAccess JSON 报告）
//
// 用法：
// 1. 路由 /admin/visitors，受保护路由（ProtectedLayout + AdminRoute）
// 2. 三个时间窗口：今日 / 7 日 / 30 日
// 3. 每个窗口展示：PV/UV KPI + 热门页面 + 来源 + 状态码 + 设备/浏览器 + 时段趋势
// 4. 状态完备：loading / empty / error / data
// 5. IP 已匿名化（GoAccess --anonymize-ip），敏感 query 参数已脱敏（backend _sanitize_path）
//
// 依赖 hooks：
// - useAdminVisitors：5 分钟轮询拉取 /admin/visitors

import { useState } from 'react'
import { useAdminVisitors } from '@/hooks/useApi'
import type { VisitorReport, VisitorSummary, VisitorMetricItem } from '@/api/endpoints'
import { formatShanghaiTime } from '@/utils/datetime'

type TimeWindow = 'today' | 'seven_days' | 'thirty_days'

const WINDOW_LABELS: Record<TimeWindow, string> = {
  today: '今日',
  seven_days: '最近 7 日',
  thirty_days: '最近 30 日',
}

/** 渲染单个指标列表（如热门页面、来源等） */
function MetricList({
  title,
  items,
  emptyText,
}: {
  title: string
  items: VisitorMetricItem[]
  emptyText: string
}) {
  return (
    <div className="card section-gap">
      <div className="card-head">
        <div className="card-title">{title}</div>
      </div>
      {items.length === 0 ? (
        <div className="empty-state">{emptyText}</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>标签</th>
              <th>次数</th>
              <th>占比</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, idx) => (
              <tr key={`${item.label}-${idx}`}>
                <td className="num">{item.label}</td>
                <td className="num">{item.count}</td>
                <td className="num">
                  {item.percentage != null ? `${item.percentage.toFixed(1)}%` : '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

/** 渲染单个时间窗口的汇总 */
function SummarySection({ label, summary }: { label: string; summary: VisitorSummary }) {
  return (
    <div className="grid split-3-even section-gap">
      {/* KPI 卡 */}
      <section className="card">
        <div className="card-head">
          <div className="card-title">{label}</div>
        </div>
        <div className="kpi-row">
          <div className="kpi-item">
            <div className="kpi-label">PV（页面浏览）</div>
            <div className="kpi-value">{summary.pv}</div>
          </div>
          <div className="kpi-item">
            <div className="kpi-label">UV（独立访客）</div>
            <div className="kpi-value">{summary.uv}</div>
          </div>
        </div>
      </section>

      {/* 热门页面 */}
      <MetricList
        title="热门页面"
        items={summary.top_pages}
        emptyText="暂无页面访问数据"
      />

      {/* 来源 */}
      <MetricList
        title="访问来源"
        items={summary.top_referrers}
        emptyText="暂无来源数据"
      />

      {/* 状态码 */}
      <MetricList
        title="状态码分布"
        items={summary.status_codes}
        emptyText="暂无状态码数据"
      />

      {/* 设备 */}
      <MetricList
        title="设备类型"
        items={summary.devices}
        emptyText="暂无设备数据"
      />

      {/* 浏览器 */}
      <MetricList
        title="浏览器"
        items={summary.browsers}
        emptyText="暂无浏览器数据"
      />

      {/* 时段趋势 */}
      <MetricList
        title="24 小时时段趋势"
        items={summary.hourly_trend}
        emptyText="暂无时段趋势数据"
      />
    </div>
  )
}

export default function AdminVisitorsPage() {
  const visitorsQuery = useAdminVisitors()
  const [activeWindow, setActiveWindow] = useState<TimeWindow>('today')

  const report: VisitorReport | undefined = visitorsQuery.data

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">访问统计</h1>
          <div className="page-desc">
            [Gate5] GoAccess 报告 · IP 已匿名化 · 敏感参数已脱敏 · 5 分钟刷新
          </div>
        </div>
        <div className="actions">
          {report?.generated_at && (
            <span className="chip" title="报告生成时间">
              生成于 {formatShanghaiTime(report.generated_at)}
            </span>
          )}
        </div>
      </div>

      {/* 时间窗口切换 */}
      <div className="tab-row section-gap">
        {(Object.keys(WINDOW_LABELS) as TimeWindow[]).map((w) => (
          <button
            key={w}
            type="button"
            className={`jobs-tab ${activeWindow === w ? 'active' : ''}`}
            onClick={() => setActiveWindow(w)}
            aria-selected={activeWindow === w}
          >
            {WINDOW_LABELS[w]}
          </button>
        ))}
      </div>

      {/* Loading 状态 */}
      {visitorsQuery.isLoading && (
        <div className="card section-gap">
          <div className="empty-state">加载中...</div>
        </div>
      )}

      {/* 统一空态：合并 API error / report empty / report error 三种情况 */}
      {visitorsQuery.isError && (
        <div className="card section-gap">
          <div className="empty-state">
            {import.meta.env.DEV
              ? '本地不生成访问统计'
              : '访问统计服务异常'}
            <div className="hint">
              {import.meta.env.DEV
                ? '本地开发环境不运行 GoAccess 容器，请在生产环境查看访问统计。'
                : `生产环境请联系管理员确认 goaccess 容器已启动。${visitorsQuery.error?.message ? `（${visitorsQuery.error.message}）` : ''}`}
            </div>
          </div>
        </div>
      )}

      {report && (report.data_source === 'empty' || report.data_source === 'error') && (
        <div className="card section-gap">
          <div className="empty-state">
            {import.meta.env.DEV
              ? '本地不生成访问统计'
              : report.data_source === 'error'
                ? '访问统计服务异常'
                : '访问统计报告未生成'}
            <div className="hint">
              {import.meta.env.DEV
                ? '本地开发环境不运行 GoAccess 容器，请在生产环境查看访问统计。'
                : report.error_message
                  ? report.error_message
                  : '生产环境请确认 goaccess 容器已启动且 Nginx access.log 卷已正确挂载。'}
            </div>
          </div>
        </div>
      )}

      {/* 正常数据展示 */}
      {report && report.data_source === 'goaccess_json' && (
        <SummarySection
          label={WINDOW_LABELS[activeWindow]}
          summary={report[activeWindow]}
        />
      )}
    </>
  )
}
