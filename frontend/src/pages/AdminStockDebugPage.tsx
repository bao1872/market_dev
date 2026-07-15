// [AdminStockDebugPage] - 描述: 管理员个股调试页面（/admin/stocks/:symbol/debug）
// 位于 AdminAppShell + AdminRoute 下，普通用户不可访问（403）。
// PRD V1.1 §7.3 + AGENTS 规则17: 复用 useStockResearchData + StockResearchWorkspace
// 展示：K线（中栏） + code/label 分离状态 + raw payload（右栏，Raw JSON 默认折叠）
// 普通用户 /market 不展示任何原始因子或 JSON。
import { useState, useCallback, useMemo, useEffect } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { MarketInstrumentPane } from '@/features/market-workspace/MarketInstrumentPane'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { useAdminStockDebug } from '@/hooks/useApi'
import type { AdminStockDebugResponse } from '@/api/endpoints'
import {
  DEFAULT_TIMEFRAME,
  normalizeDisplayTimeframe,
  type DisplayTimeframe,
} from '@/features/stock-research/stockResearchTypes'
// [CHANGE-011 SMC] - 加载初始 layerVisibility.smc 状态，驱动 indicators 按需重拉
import { loadChartLayerVisibility } from '@/features/stock-research/indicatorPreferences'
import workspaceStyles from '@/features/market-workspace/MarketWorkspace.module.scss'
import debugStyles from '@/features/research-context/EventStatePanel.module.scss'
import clsx from 'clsx'

/** 可折叠的 Raw JSON 区块（默认折叠） */
function CollapsibleJsonSection({
  title,
  data,
}: {
  title: string
  data: unknown
}) {
  const [expanded, setExpanded] = useState(false)
  return (
    <section className={debugStyles.adminDebugSection}>
      <h3 className={debugStyles.adminDebugSectionTitle}>
        <button
          onClick={() => setExpanded(!expanded)}
          className={debugStyles.collapseToggle}
          aria-expanded={expanded}
        >
          {expanded ? '▼' : '▶'} {title}
        </button>
      </h3>
      {expanded && (
        <pre className={debugStyles.adminDebugJson}>
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </section>
  )
}

export default function AdminStockDebugPage() {
  const { symbol: routeSymbol } = useParams<{ symbol?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)
  const [timeframe, setTimeframe] = useState<DisplayTimeframe>(
    normalizeDisplayTimeframe(searchParams.get('timeframe')) ?? DEFAULT_TIMEFRAME,
  )

  const symbol = routeSymbol || searchParams.get('symbol') || null
  const asOf = searchParams.get('as_of') || null

  const handleSelectSymbol = useCallback(
    (_newSymbol: string, _instrumentId: string) => {
      const params = new URLSearchParams(searchParams)
      params.set('symbol', _newSymbol)
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )

  const handleTimeframeChange = useCallback((tf: DisplayTimeframe) => {
    setTimeframe(tf)
  }, [])

  // [CHANGE-011 SMC] - smc 开关状态：初始值从 localStorage 读取，与 StockResearchWorkspace 同源；
  //   用户在 IndicatorToolbar 切换时由 onSmcToggle 回调更新；驱动 useStockResearchData 重拉 indicators。
  //   AdminStockDebugPage 使用 source='watchlist' strategyKey='watchlist_monitor'，与 StockResearchWorkspace 调用一致。
  const [smcEnabled, setSmcEnabled] = useState<boolean>(() =>
    loadChartLayerVisibility('watchlist', 'watchlist_monitor').smc,
  )
  useEffect(() => {
    setSmcEnabled(loadChartLayerVisibility('watchlist', 'watchlist_monitor').smc)
  }, [])
  const handleSmcToggle = useCallback((enabled: boolean) => {
    setSmcEnabled(enabled)
  }, [])

  // K线研究数据（复用 /market 和 /stock/:symbol 共用 hook）
  const researchData = useStockResearchData({
    symbol: symbol ?? null,
    timeframe,
    includeSmc: smcEnabled,
  })

  const debugQuery = useAdminStockDebug(
    symbol ?? undefined,
    asOf ? { as_of: asOf } : undefined,
    { enabled: !!symbol },
  )

  const data: AdminStockDebugResponse | undefined = debugQuery.data

  // 提取原始 payload 用于管理员调试展示
  const rawPayload = useMemo(() => {
    if (!data?.rawDebug) return null
    return {
      structural: data.rawDebug.structuralPayload,
      temporal: data.rawDebug.temporalPayload,
      summary: data.rawDebug.summaryPayload,
      runId: data.rawDebug.runId,
      runType: data.rawDebug.runType,
      runStartedAt: data.rawDebug.runStartedAt,
      runFinishedAt: data.rawDebug.runFinishedAt,
      sourcePrimaryBarTime: data.rawDebug.sourcePrimaryBarTime,
      sourceSecondaryBarTime: data.rawDebug.sourceSecondaryBarTime,
    }
  }, [data])

  return (
    <div className={workspaceStyles.workspace}>
      {/* 左栏：股票搜索 */}
      <div className={workspaceStyles.leftPane}>
        <div className={workspaceStyles.scopeTabs}>
          <span className={clsx(workspaceStyles.scopeTab, workspaceStyles.scopeTabActive)}>调试</span>
        </div>
        <div className={workspaceStyles.leftPaneContent}>
          <MarketInstrumentPane
            scope="market"
            selectedSymbol={symbol ?? null}
            onSelectSymbol={handleSelectSymbol}
          />
        </div>
      </div>

      {/* 中栏：K线 + 右栏：调试数据 */}
      <div className={workspaceStyles.centerRight}>
        {symbol ? (
          <>
            {/* 中栏：K线研究区（复用 StockResearchWorkspace） */}
            <div className={workspaceStyles.centerPane}>
              <StockResearchWorkspace
                data={researchData}
                timeframe={timeframe}
                onTimeframeChange={handleTimeframeChange}
                source="watchlist"
                strategyKey="watchlist_monitor"
                rightPanelCollapsed={rightPanelCollapsed}
                showRightPanel={!rightPanelCollapsed}
                onSmcToggle={handleSmcToggle}
                rightPanel={
                  <div className={debugStyles.adminDebugContainer}>
                    <div className={debugStyles.adminDebugHeader}>
                      <h2 className={debugStyles.adminDebugTitle}>调试 - {symbol}</h2>
                      {debugQuery.isLoading && <span className={debugStyles.adminDebugStatus}>加载中…</span>}
                      {debugQuery.isError && <span className={debugStyles.adminDebugError}>加载失败</span>}
                    </div>

                    {data?.atomicFactsDebug && data.atomicFactsDebug.length > 0 && (
                      <section className={debugStyles.adminDebugSection}>
                        <h3 className={debugStyles.adminDebugSectionTitle}>
                          原子事实可追溯（Fact ID / 真实路径 / raw value / 阈值来源 / feature flag）
                        </h3>
                        <table className={debugStyles.adminDebugTable}>
                          <thead>
                            <tr>
                              <th>Fact ID</th>
                              <th>真实路径</th>
                              <th>raw value</th>
                              <th>阈值来源</th>
                              <th>阈值启用</th>
                              <th>feature flag</th>
                            </tr>
                          </thead>
                          <tbody>
                            {data.atomicFactsDebug.map((d) => (
                              <tr key={d.factId}>
                                <td>{d.factId}</td>
                                <td>{d.sourcePath ?? 'null'}</td>
                                <td>{d.rawValue ?? 'null'}</td>
                                <td>{d.thresholdRef ?? 'null'}</td>
                                <td>{String(d.thresholdEnabled)}</td>
                                <td>{String(d.featureFlag)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </section>
                    )}

                    {data?.recentChanges && data.recentChanges.length > 0 && (
                      <section className={debugStyles.adminDebugSection}>
                        <h3 className={debugStyles.adminDebugSectionTitle}>近期变化（point-in-time）</h3>
                        <table className={debugStyles.adminDebugTable}>
                          <thead>
                            <tr>
                              <th>Fact ID</th>
                              <th>from</th>
                              <th>to</th>
                              <th>as_of</th>
                            </tr>
                          </thead>
                          <tbody>
                            {data.recentChanges.slice(0, 30).map((c, i) => (
                              <tr key={`${c.factId}-${i}`}>
                                <td>{c.factId}</td>
                                <td>{c.fromCategory ?? c.fromValue ?? 'null'}</td>
                                <td>{c.toCategory ?? c.toValue ?? 'null'}</td>
                                <td>{c.asOf}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </section>
                    )}

                    {rawPayload && (
                      <section className={debugStyles.adminDebugSection}>
                        <h3 className={debugStyles.adminDebugSectionTitle}>Run 元数据</h3>
                        <table className={debugStyles.adminDebugTable}>
                          <tbody>
                            <tr><td>run_id</td><td>{rawPayload.runId}</td></tr>
                            <tr><td>run_type</td><td>{rawPayload.runType}</td></tr>
                            <tr><td>started_at</td><td>{rawPayload.runStartedAt ?? 'null'}</td></tr>
                            <tr><td>finished_at</td><td>{rawPayload.runFinishedAt ?? 'null'}</td></tr>
                            <tr><td>source_primary_bar_time</td><td>{rawPayload.sourcePrimaryBarTime ?? 'null'}</td></tr>
                            <tr><td>source_secondary_bar_time</td><td>{rawPayload.sourceSecondaryBarTime ?? 'null'}</td></tr>
                          </tbody>
                        </table>
                      </section>
                    )}

                    {/* Raw JSON 默认折叠 */}
                    {rawPayload && (
                      <CollapsibleJsonSection
                        title="Raw Structural Payload"
                        data={rawPayload.structural}
                      />
                    )}
                    {rawPayload && (
                      <CollapsibleJsonSection
                        title="Raw Temporal Payload"
                        data={rawPayload.temporal}
                      />
                    )}
                    {rawPayload && (
                      <CollapsibleJsonSection
                        title="Raw Summary Payload"
                        data={rawPayload.summary}
                      />
                    )}

                    {data?.dataQuality && (
                      <section className={debugStyles.adminDebugSection}>
                        <h3 className={debugStyles.adminDebugSectionTitle}>数据质量</h3>
                        <table className={debugStyles.adminDebugTable}>
                          <tbody>
                            <tr><td>has_succeeded_run</td><td>{String(data.dataQuality.hasSucceededRun)}</td></tr>
                            <tr><td>has_snapshot</td><td>{String(data.dataQuality.hasSnapshot)}</td></tr>
                            <tr><td>run_trade_date</td><td>{data.dataQuality.runTradeDate ?? 'null'}</td></tr>
                            <tr><td>run_published_at</td><td>{data.dataQuality.runPublishedAt ?? 'null'}</td></tr>
                            <tr><td>instrument_status</td><td>{data.dataQuality.instrumentStatus}</td></tr>
                            <tr><td>degraded_reasons</td><td>{data.dataQuality.degradedReasons.join(', ') || 'none'}</td></tr>
                          </tbody>
                        </table>
                      </section>
                    )}
                  </div>
                }
              />
            </div>

            {/* 右栏折叠按钮 */}
            {!rightPanelCollapsed && (
              <button
                className={workspaceStyles.collapseBtn}
                onClick={() => setRightPanelCollapsed(true)}
                aria-label="收起右栏"
              >
                ›
              </button>
            )}
            {rightPanelCollapsed && (
              <button
                className={workspaceStyles.expandBtn}
                onClick={() => setRightPanelCollapsed(false)}
                aria-label="展开右栏"
              >
                ‹
              </button>
            )}
          </>
        ) : (
          <div className={workspaceStyles.emptyCenter}>
            <div className={workspaceStyles.emptyIcon}>◎</div>
            <div className={workspaceStyles.emptyText}>从左侧搜索一只股票开始调试</div>
          </div>
        )}
      </div>
    </div>
  )
}
