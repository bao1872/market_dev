// 任务执行事件时间线 - 在任务详情抽屉中展示事件流
// 用法：在 AdminJobsPage 任务详情抽屉中嵌入，传入 selectedRunId
// 依赖：useJobRunEvents（GET /admin/job-runs/{run_id}/events）

import { useJobRunEvents } from '@/hooks/useApi'
import { formatShanghaiTime } from '@/utils/datetime'

interface JobRunEventTimelineProps {
  runId: string | null | undefined
}

export function JobRunEventTimeline({ runId }: JobRunEventTimelineProps) {
  const eventsQuery = useJobRunEvents(runId)

  if (!runId) return <div className="notice">未选择任务</div>
  if (eventsQuery.isLoading) return <div className="notice">事件加载中…</div>
  if (eventsQuery.isError) return <div className="notice error">事件加载失败</div>

  const events = eventsQuery.data?.items ?? []
  if (events.length === 0) return <div className="notice">暂无事件记录</div>

  return (
    <div className="job-event-timeline">
      {events.map((event) => (
        <div key={event.id} className={`job-event-item ${event.level}`}>
          <span className={`job-event-level ${event.level}`}>
            {event.level === 'error' ? 'ERROR' : event.level === 'warn' ? 'WARN' : 'INFO'}
          </span>
          <div className="job-event-main">
            <div className="job-event-step">{event.step}</div>
            <div className="job-event-message">{event.message}</div>
            <div className="job-event-time">{formatShanghaiTime(event.created_at)}</div>
            {event.payload && Object.keys(event.payload).length > 0 && (
              <pre className="job-event-payload">{JSON.stringify(event.payload, null, 2)}</pre>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
