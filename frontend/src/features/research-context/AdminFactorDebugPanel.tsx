// [AdminFactorDebugPanel] - 描述: 管理员调试面板（仅 is_admin && debug=1 时渲染）
// 展示原始 factor/feature、timeframe、bar_time、版本、数据源、warmup/degraded 信息和可折叠 JSON。
// 普通用户即使手工加 debug=1 也不可见（由父组件 ResearchContextPanel 校验 is_admin）。
// 不新增后端算法；复用现有事件详情、structural、temporal 接口数据。
import type { UseQueryResult } from '@tanstack/react-query'
import type {
  StrategyEventDetail,
  StructuralFactorResponse,
  TemporalFeaturesResponse,
} from '@/api/endpoints'
import type { ResearchContextStyles } from './ResearchContextPanel'

interface AdminFactorDebugPanelProps {
  eventDetailQuery: UseQueryResult<StrategyEventDetail, Error>
  structuralQuery: UseQueryResult<StructuralFactorResponse, Error>
  temporalQuery: UseQueryResult<TemporalFeaturesResponse, Error>
  eventId: string | null | undefined
  styles: ResearchContextStyles
}

function DebugBlock({
  title,
  data,
  styles,
}: {
  title: string
  data: unknown
  styles: ResearchContextStyles
}) {
  return (
    <details className={styles.debugDetail}>
      <summary className={styles.debugSummary}>{title}</summary>
      <pre className={styles.debugPre}>
        {JSON.stringify(data, null, 2)}
      </pre>
    </details>
  )
}

function MetaRow({ label, value, styles }: { label: string; value: string; styles: ResearchContextStyles }) {
  return (
    <div className={styles.debugMetaRow}>
      <span className={styles.debugMetaLabel}>{label}</span>
      <span className={styles.debugMetaValue}>{value}</span>
    </div>
  )
}

export function AdminFactorDebugPanel({
  eventDetailQuery,
  structuralQuery,
  temporalQuery,
  eventId,
  styles,
}: AdminFactorDebugPanelProps) {
  const eventDetail = eventDetailQuery.data
  const structural = structuralQuery.data
  const temporal = temporalQuery.data

  return (
    <div className={styles.debugSection}>
      <div className={styles.sectionTitle}>调试信息（管理员）</div>

      {/* 事件详情调试 */}
      {eventId && (
        <div className={styles.debugGroup}>
          <div className={styles.debugGroupTitle}>事件详情原始数据</div>
          {eventDetail ? (
            <>
              <MetaRow label="event_id" value={eventDetail.id} styles={styles} />
              <MetaRow label="event_type" value={eventDetail.event_type} styles={styles} />
              <MetaRow label="event_time" value={eventDetail.event_time} styles={styles} />
              <MetaRow label="schema_version" value={String(eventDetail.schema_version)} styles={styles} />
              <MetaRow label="strategy_version_id" value={eventDetail.strategy_version_id} styles={styles} />
              <DebugBlock title="payload JSON" data={eventDetail.payload} styles={styles} />
              <DebugBlock title="snapshot JSON" data={eventDetail.snapshot} styles={styles} />
            </>
          ) : eventDetailQuery.isLoading ? (
            <div className={styles.loading}>加载中…</div>
          ) : eventDetailQuery.isError ? (
            <div className={styles.error}>加载失败：{eventDetailQuery.error?.message}</div>
          ) : (
            <div className={styles.empty}>事件不存在</div>
          )}
        </div>
      )}

      {/* 结构因子调试 */}
      <div className={styles.debugGroup}>
        <div className={styles.debugGroupTitle}>结构因子原始数据</div>
        {structural ? (
          <>
            <MetaRow label="as_of" value={structural.meta.as_of} styles={styles} />
            <MetaRow label="primary_lookback_bars" value={String(structural.meta.primary_lookback_bars)} styles={styles} />
            <MetaRow label="secondary_lookback_bars" value={String(structural.meta.secondary_lookback_bars)} styles={styles} />
            {structural.meta.degraded_reasons.length > 0 && (
              <MetaRow label="degraded_reasons" value={structural.meta.degraded_reasons.join('; ')} styles={styles} />
            )}
            {structural.meta.warmup_notes.length > 0 && (
              <MetaRow label="warmup_notes" value={structural.meta.warmup_notes.join('; ')} styles={styles} />
            )}
            <DebugBlock title="primary factors JSON" data={structural.primary} styles={styles} />
            <DebugBlock title="secondary factors JSON" data={structural.secondary} styles={styles} />
            <DebugBlock title="relation JSON" data={structural.relation} styles={styles} />
          </>
        ) : structuralQuery.isLoading ? (
          <div className={styles.loading}>加载中…</div>
        ) : structuralQuery.isError ? (
          <div className={styles.error}>加载失败：{structuralQuery.error?.message}</div>
        ) : (
          <div className={styles.empty}>暂无结构因子数据</div>
        )}
      </div>

      {/* 时序特征调试 */}
      <div className={styles.debugGroup}>
        <div className={styles.debugGroupTitle}>时序特征原始数据</div>
        {temporal ? (
          <>
            <MetaRow label="as_of" value={temporal.meta.as_of} styles={styles} />
            <MetaRow label="primary_timeframe" value={temporal.meta.primary_timeframe} styles={styles} />
            <MetaRow label="secondary_timeframe" value={temporal.meta.secondary_timeframe} styles={styles} />
            {temporal.meta.degraded_reasons.length > 0 && (
              <MetaRow label="degraded_reasons" value={temporal.meta.degraded_reasons.join('; ')} styles={styles} />
            )}
            {temporal.meta.warmup_notes.length > 0 && (
              <MetaRow label="warmup_notes" value={temporal.meta.warmup_notes.join('; ')} styles={styles} />
            )}
            <DebugBlock title="daily_context JSON" data={temporal.daily_context} styles={styles} />
            <DebugBlock title="m15_response JSON" data={temporal.m15_response} styles={styles} />
            <DebugBlock title="derived_relation JSON" data={temporal.derived_relation} styles={styles} />
          </>
        ) : temporalQuery.isLoading ? (
          <div className={styles.loading}>加载中…</div>
        ) : temporalQuery.isError ? (
          <div className={styles.error}>加载失败：{temporalQuery.error?.message}</div>
        ) : (
          <div className={styles.empty}>暂无时序特征数据</div>
        )}
      </div>
    </div>
  )
}
