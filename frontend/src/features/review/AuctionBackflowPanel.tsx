// [AuctionBackflowPanel] - 描述: 第二金字塔 + 竞价事件回流面板（PRD §75）
// 在 /review 页面展示当日竞价数据的四个维度：
// - 分布：event_type → count、lifecycle → count（含 formed/confirmed/continued/weakened/failed/transformed/expired）
// - 迁移：events 的 (prev_lifecycle, lifecycle) 转换计数（来自 confirmation_data.prev_lifecycle）
// - 新鲜度：anchor_items 按 freshness 桶聚合（today/3d/7d/30d/stale）
// - 集中度：market scope 的 HHI、top3/5、leader_median_gap；top3 行业同字段
//
// 数据来源：GET /api/v1/auction/backflow/{trade_date}
// 触发：用户在 /review 页面切换到 "竞价回流" Tab，或独立 section 渲染
// 前端不重算业务结论，只展示后端聚合结果。
//
// 跳转：点击事件行可跳转到 /auction/stock/{symbol}（使用 symbol 而非 UUID）

import { Link } from 'react-router-dom'
import { useAuctionBackflow, extractAuctionError } from '@/features/auction/api'
import {
  EVENT_LIFECYCLE_LABELS,
  type AuctionBackflowData,
  type AuctionConcentrationInfo,
} from '@/features/auction/types'
import { formatShanghaiTime } from '@/utils/datetime'
import styles from './review.module.scss'

const LIFECYCLE_ORDER = [
  'formed',
  'confirmed',
  'continued',
  'weakened',
  'failed',
  'transformed',
  'expired',
] as const

/** 安全格式化数值（保留两位小数） */
function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return v.toFixed(digits)
}

/** 安全截断 UUID（前 8 位） */
function fmtId(v: string | null | undefined): string {
  if (!v) return '—'
  return v.slice(0, 8)
}

/** 生命周期 chip 样式 */
function lifecycleChipClass(lifecycle: string | null | undefined): string {
  if (!lifecycle) return styles.chipDefault
  const l = lifecycle.toLowerCase()
  if (l === 'confirmed') return styles.chipSuccess
  if (l === 'continued') return styles.chipInfo
  if (l === 'formed') return styles.chipInfo
  if (l === 'weakened') return styles.chipWarning
  if (l === 'transformed') return styles.chipWarning
  if (l === 'failed' || l === 'expired') return styles.chipDanger
  return styles.chipDefault
}

/** 集中度行（market + top3 行业） */
function ConcentrationRow({
  label,
  info,
}: {
  label: string
  info: AuctionConcentrationInfo
}) {
  return (
    <div className={styles.panelSectionBody}>
      <div className={styles.headerMeta}>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>scope_name:</span>
          <span className={styles.metaValue}>{info.scope_name ?? label}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>HHI:</span>
          <span className={styles.metaValue}>{fmtNum(info.hhi ?? null, 4)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>top3:</span>
          <span className={styles.metaValue}>{fmtNum(info.top3_contribution ?? null)}%</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>top5:</span>
          <span className={styles.metaValue}>{fmtNum(info.top5_contribution ?? null)}%</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>leader_median_gap:</span>
          <span className={styles.metaValue}>{fmtNum(info.leader_median_gap ?? null)}%</span>
        </span>
        {info.median_change_pct !== undefined && (
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>median_change_pct:</span>
            <span className={styles.metaValue}>{fmtNum(info.median_change_pct ?? null)}%</span>
          </span>
        )}
      </div>
    </div>
  )
}

export interface AuctionBackflowPanelProps {
  tradeDate: string
}

export default function AuctionBackflowPanel({ tradeDate }: AuctionBackflowPanelProps) {
  const query = useAuctionBackflow(tradeDate, { topEvents: 50 })
  const data: AuctionBackflowData | undefined = query.data

  // 加载态
  if (query.isLoading) {
    return (
      <div className={styles.panel}>
        <div className={styles.panelSection}>
          <div className={styles.panelSectionHeader}>
            <span className={styles.panelSectionTitle}>竞价事件回流</span>
          </div>
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>加载第二金字塔数据...</div>
          </div>
        </div>
      </div>
    )
  }

  // 错误态
  if (query.isError) {
    const err = extractAuctionError(query.error)
    return (
      <div className={styles.panel}>
        <div className={styles.panelSection}>
          <div className={styles.panelSectionHeader}>
            <span className={styles.panelSectionTitle}>竞价事件回流</span>
          </div>
          <div className={styles.stateBox}>
            <div className={styles.stateTitle}>竞价回流数据加载失败</div>
            <div className={styles.stateDesc}>{err.message}</div>
            {err.requestId && (
              <div className={styles.stateRequestId}>request_id={err.requestId}</div>
            )}
          </div>
        </div>
      </div>
    )
  }

  if (!data) {
    return null
  }

  const reasonCodes = data.reason_codes ?? []
  const hasScanRun = !!data.scan_run_id

  // 顶部信息栏
  const headerMeta = (
    <div className={styles.headerMeta}>
      <span className={styles.metaItem}>
        <span className={styles.metaLabel}>交易日:</span>
        <span className={styles.metaValue}>{data.trade_date}</span>
      </span>
      <span className={styles.metaItem}>
        <span className={styles.metaLabel}>算法版本:</span>
        <span className={styles.metaValue}>{data.algorithm_version}</span>
      </span>
      {data.scan_run_id && (
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>scan_run_id:</span>
          <span className={styles.metaValue}>{fmtId(data.scan_run_id)}</span>
        </span>
      )}
      {data.anchor_publication_id && (
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>publication_id:</span>
          <span className={styles.metaValue}>{fmtId(data.anchor_publication_id)}</span>
        </span>
      )}
      {data.source_core_run_id && (
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>source_core:</span>
          <span className={styles.metaValue}>{fmtId(data.source_core_run_id)}</span>
        </span>
      )}
      {data.source_chip_run_id && (
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>source_chip:</span>
          <span className={styles.metaValue}>{fmtId(data.source_chip_run_id)}</span>
        </span>
      )}
    </div>
  )

  return (
    <div className={styles.panel}>
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>第二金字塔 · 竞价事件回流</span>
          <span className={styles.pagination}>
            事件 {data.backflow_events.length} · 迁移 {data.event_migrations.length}
          </span>
        </div>
        {headerMeta}
        {reasonCodes.length > 0 && (
          <div className={styles.reasonCodes}>
            {reasonCodes.map((rc) => (
              <span key={rc} className={styles.reasonCode}>
                {rc}
              </span>
            ))}
          </div>
        )}
        {!hasScanRun && (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>
              当日竞价 scan_run 未发布，盘后 09:25:05 触发完成后将自动生成
            </div>
          </div>
        )}
      </div>

      {/* 1. 分布 */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>分布</span>
        </div>
        <div className={styles.panelSectionBody}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div>
              <div className={styles.panelSectionTitle} style={{ marginBottom: 6 }}>
                事件类型（event_type）
              </div>
              <DistributionTable distribution={data.event_type_distribution} />
            </div>
            <div>
              <div className={styles.panelSectionTitle} style={{ marginBottom: 6 }}>
                生命周期（lifecycle）
              </div>
              <LifecycleDistributionTable
                distribution={data.lifecycle_distribution}
              />
            </div>
          </div>
        </div>
      </div>

      {/* 2. 迁移 */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>迁移</span>
          <span className={styles.pagination}>
            {data.event_migrations.length} 个转换
          </span>
        </div>
        <div className={styles.panelSectionBody}>
          {data.event_migrations.length === 0 ? (
            <div className={styles.stateDesc}>暂无 lifecycle 转换记录</div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>前态</th>
                  <th>现态</th>
                  <th>事件数</th>
                  <th>样本 instrument_id</th>
                </tr>
              </thead>
              <tbody>
                {data.event_migrations.map((m, i) => (
                  <tr key={`${m.from_lifecycle ?? 'null'}-${m.to_lifecycle}-${i}`}>
                    <td>
                      {m.from_lifecycle ? (
                        <span
                          className={`${styles.chip} ${lifecycleChipClass(m.from_lifecycle)}`}
                        >
                          {EVENT_LIFECYCLE_LABELS[m.from_lifecycle] ?? m.from_lifecycle}
                        </span>
                      ) : (
                        <span className={styles.chipDefault}>（首次）</span>
                      )}
                    </td>
                    <td>
                      <span
                        className={`${styles.chip} ${lifecycleChipClass(m.to_lifecycle)}`}
                      >
                        {EVENT_LIFECYCLE_LABELS[m.to_lifecycle] ?? m.to_lifecycle}
                      </span>
                    </td>
                    <td>{m.event_count}</td>
                    <td>
                      {m.sample_instrument_ids.slice(0, 3).map((id) => (
                        <code key={id} className={styles.metaValue}>
                          {fmtId(id)}
                        </code>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* 3. 新鲜度 */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>新鲜度</span>
          <span className={styles.pagination}>
            {data.anchor_freshness_buckets.length} 个 freshness 桶
          </span>
        </div>
        <div className={styles.panelSectionBody}>
          {data.anchor_freshness_buckets.length === 0 ? (
            <div className={styles.stateDesc}>暂无锚点新鲜度数据</div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>freshness</th>
                  <th>锚点总数</th>
                  <th>活跃锚点</th>
                  <th>活跃占比</th>
                </tr>
              </thead>
              <tbody>
                {data.anchor_freshness_buckets.map((b) => (
                  <tr key={b.freshness}>
                    <td>
                      <span className={`${styles.chip} ${styles.chipInfo}`}>
                        {b.freshness}
                      </span>
                    </td>
                    <td>{b.anchor_count}</td>
                    <td>{b.active_count}</td>
                    <td>
                      {b.anchor_count > 0
                        ? `${((b.active_count / b.anchor_count) * 100).toFixed(1)}%`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* 4. 集中度 */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>集中度</span>
        </div>
        <div className={styles.panelSectionBody}>
          <ConcentrationRow label="市场" info={data.market_concentration} />
          {data.top_industry_concentration.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className={styles.panelSectionTitle} style={{ marginBottom: 6 }}>
                Top 3 行业（按 median_change_pct）
              </div>
              {data.top_industry_concentration.map((ind, i) => (
                <ConcentrationRow
                  key={ind.scope_id ?? `industry-${i}`}
                  label={`行业 ${i + 1}`}
                  info={ind}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 5. 竞价事件回流（按 formed_at desc，最多 50 条） */}
      <div className={styles.panelSection}>
        <div className={styles.panelSectionHeader}>
          <span className={styles.panelSectionTitle}>竞价事件回流</span>
          <span className={styles.pagination}>
            共 {data.backflow_events.length} 条事件
          </span>
        </div>
        <div className={styles.panelSectionBody}>
          {data.backflow_events.length === 0 ? (
            <div className={styles.stateDesc}>当日暂无竞价事件</div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>symbol</th>
                  <th>名称</th>
                  <th>事件类型</th>
                  <th>生命周期</th>
                  <th>形成时间</th>
                  <th>跳转</th>
                </tr>
              </thead>
              <tbody>
                {data.backflow_events.map((ev) => (
                  <tr key={ev.id}>
                    <td>
                      {/* [P0-FE] 使用 symbol 而非 UUID 进行导航 */}
                      {ev.symbol ? (
                        <Link
                          to={`/auction/stock/${ev.symbol}`}
                          className={styles.breadcrumbItem}
                        >
                          {ev.symbol}
                        </Link>
                      ) : (
                        <code className={styles.metaValue}>{fmtId(ev.instrument_id)}</code>
                      )}
                    </td>
                    <td>{ev.name ?? '—'}</td>
                    <td>{ev.event_type}</td>
                    <td>
                      <span
                        className={`${styles.chip} ${lifecycleChipClass(ev.lifecycle)}`}
                      >
                        {EVENT_LIFECYCLE_LABELS[ev.lifecycle] ?? ev.lifecycle}
                      </span>
                    </td>
                    <td>
                      {ev.formed_at ? formatShanghaiTime(ev.formed_at) : '—'}
                    </td>
                    <td>
                      {ev.symbol && (
                        <Link
                          to={`/auction/stock/${ev.symbol}`}
                          className={styles.breadcrumbItem}
                        >
                          详情 →
                        </Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}

/** 通用分布表（key → count） */
function DistributionTable({
  distribution,
}: {
  distribution: Record<string, number>
}) {
  const entries = Object.entries(distribution).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) {
    return <div className={styles.stateDesc}>无数据</div>
  }
  const total = entries.reduce((sum, [, c]) => sum + c, 0)
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>类型</th>
          <th>数量</th>
          <th>占比</th>
        </tr>
      </thead>
      <tbody>
        {entries.map(([k, c]) => (
          <tr key={k}>
            <td>
              <span className={`${styles.chip} ${styles.chipDefault}`}>{k}</span>
            </td>
            <td>{c}</td>
            <td>{total > 0 ? `${((c / total) * 100).toFixed(1)}%` : '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** lifecycle 分布表（按 LIFECYCLE_ORDER 排序） */
function LifecycleDistributionTable({
  distribution,
}: {
  distribution: Record<string, number>
}) {
  const known = LIFECYCLE_ORDER.filter((k) => distribution[k] !== undefined)
  const others = Object.keys(distribution).filter((k) => !LIFECYCLE_ORDER.includes(k as never))
  const ordered = [...known, ...others]
  if (ordered.length === 0) {
    return <div className={styles.stateDesc}>无数据</div>
  }
  const total = ordered.reduce((sum, k) => sum + distribution[k], 0)
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>lifecycle</th>
          <th>数量</th>
          <th>占比</th>
        </tr>
      </thead>
      <tbody>
        {ordered.map((k) => (
          <tr key={k}>
            <td>
              <span className={`${styles.chip} ${lifecycleChipClass(k)}`}>
                {EVENT_LIFECYCLE_LABELS[k] ?? k}
              </span>
            </td>
            <td>{distribution[k]}</td>
            <td>{total > 0 ? `${((distribution[k] / total) * 100).toFixed(1)}%` : '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
