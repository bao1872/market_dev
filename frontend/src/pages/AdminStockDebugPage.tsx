// [AdminStockDebugPage] - 描述: 管理员个股调试页面（独立 /admin/stocks/:symbol/debug 路由）
// 位于 AdminAppShell + AdminRoute 下，普通用户不可访问（403）。
// PRD V1.1 §7.3: 使用 AdminStockDebug 接口（/api/v1/admin/stocks/{symbol}/debug）
// 展示：code/label 分离、raw payload、run/schema/as_of/published_at、事件证据。
// 普通用户 /market 不展示任何原始因子或 JSON。
import { useState, useCallback, useMemo } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { MarketInstrumentPane } from '@/features/market-workspace/MarketInstrumentPane'
import { useAdminStockDebug } from '@/hooks/useApi'
import type { AdminStockDebugResponse } from '@/api/endpoints'
import workspaceStyles from '@/features/market-workspace/MarketWorkspace.module.scss'
import debugStyles from '@/features/research-context/EventStatePanel.module.scss'
import clsx from 'clsx'

export default function AdminStockDebugPage() {
  const { symbol: routeSymbol } = useParams<{ symbol?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)

  const symbol = routeSymbol || searchParams.get('symbol') || null
  const asOf = searchParams.get('as_of') || null

  const handleSelectSymbol = useCallback(
    (newSymbol: string, _instrumentId: string) => {
      const params = new URLSearchParams(searchParams)
      params.set('symbol', newSymbol)
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )

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

      {/* 中栏 + 右栏 */}
      <div className={workspaceStyles.centerRight}>
        {symbol ? (
          <>
            <div className={workspaceStyles.centerPane}>
              <div className={debugStyles.adminDebugContainer}>
                <div className={debugStyles.adminDebugHeader}>
                  <h2 className={debugStyles.adminDebugTitle}>管理员调试 - {symbol}</h2>
                  {debugQuery.isLoading && <span className={debugStyles.adminDebugStatus}>加载中…</span>}
                  {debugQuery.isError && <span className={debugStyles.adminDebugError}>加载失败</span>}
                </div>

                {data?.state && (
                  <section className={debugStyles.adminDebugSection}>
                    <h3 className={debugStyles.adminDebugSectionTitle}>状态向量（code/label 分离）</h3>
                    <table className={debugStyles.adminDebugTable}>
                      <thead>
                        <tr>
                          <th>字段路径</th>
                          <th>code</th>
                          <th>label</th>
                          <th>value</th>
                          <th>unit</th>
                          <th>timeframe</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          <td>structure.price</td>
                          <td>{data.state.structure.price.code ?? 'null'}</td>
                          <td>{data.state.structure.price.label}</td>
                          <td>{data.state.structure.price.value ?? 'null'}</td>
                          <td>{data.state.structure.price.unit ?? 'null'}</td>
                          <td>{data.state.structure.price.timeframe}</td>
                        </tr>
                        <tr>
                          <td>structure.consensusRelation</td>
                          <td>{data.state.structure.consensusRelation.code ?? 'null'}</td>
                          <td>{data.state.structure.consensusRelation.label}</td>
                          <td>{data.state.structure.consensusRelation.value ?? 'null'}</td>
                          <td>{data.state.structure.consensusRelation.unit ?? 'null'}</td>
                          <td>{data.state.structure.consensusRelation.timeframe}</td>
                        </tr>
                        <tr>
                          <td>momentum.macd</td>
                          <td>{data.state.momentum.macd.code ?? 'null'}</td>
                          <td>{data.state.momentum.macd.label}</td>
                          <td>{data.state.momentum.macd.value ?? 'null'}</td>
                          <td>{data.state.momentum.macd.unit ?? 'null'}</td>
                          <td>{data.state.momentum.macd.timeframe}</td>
                        </tr>
                        <tr>
                          <td>momentum.sqzmom</td>
                          <td>{data.state.momentum.sqzmom.code ?? 'null'}</td>
                          <td>{data.state.momentum.sqzmom.label}</td>
                          <td>{data.state.momentum.sqzmom.value ?? 'null'}</td>
                          <td>{data.state.momentum.sqzmom.unit ?? 'null'}</td>
                          <td>{data.state.momentum.sqzmom.timeframe}</td>
                        </tr>
                        {data.state.momentum.temporal.map((t, i) => (
                          <tr key={`temporal-${i}`}>
                            <td>momentum.temporal[{i}]</td>
                            <td>{t.code ?? 'null'}</td>
                            <td>{t.label}</td>
                            <td>{t.value ?? 'null'}</td>
                            <td>{t.unit ?? 'null'}</td>
                            <td>{t.timeframe}</td>
                          </tr>
                        ))}
                        <tr>
                          <td>volatility.bollPosition</td>
                          <td>{data.state.volatility.bollPosition.code ?? 'null'}</td>
                          <td>{data.state.volatility.bollPosition.label}</td>
                          <td>{data.state.volatility.bollPosition.value ?? 'null'}</td>
                          <td>{data.state.volatility.bollPosition.unit ?? 'null'}</td>
                          <td>{data.state.volatility.bollPosition.timeframe}</td>
                        </tr>
                      </tbody>
                    </table>
                  </section>
                )}

                {data?.events && data.events.length > 0 && (
                  <section className={debugStyles.adminDebugSection}>
                    <h3 className={debugStyles.adminDebugSectionTitle}>事件证据</h3>
                    {data.events.slice(0, 20).map((ev) => (
                      <div key={ev.id} className={debugStyles.adminDebugEvent}>
                        <div className={debugStyles.adminDebugEventHeader}>
                          <span className={debugStyles.adminDebugEventTime}>{ev.occurredAt}</span>
                          <span className={debugStyles.adminDebugEventType}>{ev.eventType}</span>
                          <span className={debugStyles.adminDebugEventAsOf}>as_of={ev.currentAsOf}</span>
                        </div>
                        <div className={debugStyles.adminDebugEventTitle}>{ev.title}</div>
                        <div className={debugStyles.adminDebugEventDesc}>{ev.description}</div>
                        {ev.changedFields.length > 0 && (
                          <div className={debugStyles.adminDebugChangedFields}>
                            changed_fields: {ev.changedFields.join(', ')}
                          </div>
                        )}
                        {ev.evidence.length > 0 && (
                          <div className={debugStyles.adminDebugEvidence}>
                            {ev.evidence.map((e, i) => (
                              <div key={i} className={debugStyles.adminDebugEvidenceItem}>
                                {e.fieldName}: {e.previousValue ?? 'null'} → {e.currentValue ?? 'null'}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
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

                {rawPayload && (
                  <section className={debugStyles.adminDebugSection}>
                    <h3 className={debugStyles.adminDebugSectionTitle}>Raw Structural Payload</h3>
                    <pre className={debugStyles.adminDebugJson}>
                      {JSON.stringify(rawPayload.structural, null, 2)}
                    </pre>
                  </section>
                )}

                {rawPayload && (
                  <section className={debugStyles.adminDebugSection}>
                    <h3 className={debugStyles.adminDebugSectionTitle}>Raw Temporal Payload</h3>
                    <pre className={debugStyles.adminDebugJson}>
                      {JSON.stringify(rawPayload.temporal, null, 2)}
                    </pre>
                  </section>
                )}

                {rawPayload && (
                  <section className={debugStyles.adminDebugSection}>
                    <h3 className={debugStyles.adminDebugSectionTitle}>Raw Summary Payload</h3>
                    <pre className={debugStyles.adminDebugJson}>
                      {JSON.stringify(rawPayload.summary, null, 2)}
                    </pre>
                  </section>
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
