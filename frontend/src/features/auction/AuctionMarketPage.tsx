// [AuctionMarketPage] - 描述: 竞价分析市场级页面
// 路由 /auction
// 后端完成全部计算，前端只展示、过滤和展开证据
// 接口必须显示：trade_date、algorithm_version、publication_id、source run IDs、coverage、reason_codes
//
// 展示：
// - 行业和概念排行（按 status_label 或 change_pct 排序）
// - 突破/破位广度（structure_breakout_count / structure_breakdown_count / dual_breakout_count 等）
// - 参与度（participation_median / abnormal_volume_pct）
// - 集中度（top3_contribution / top5_contribution / hhi / leader_median_gap）
// - Top 事件列表（top_events）

import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuctionMarket, extractAuctionError } from './api'
import {
  EVENT_LIFECYCLE_LABELS,
  type ScopeResult,
  type EventTracking,
} from './types'
import { formatShanghaiTime } from '@/utils/datetime'
import styles from './auction.module.scss'

type SortKey = 'status_label' | 'median_change_pct' | 'coverage_ratio' | 'hhi'

const SORT_OPTIONS: Array<{ value: SortKey; label: string }> = [
  { value: 'median_change_pct', label: '中位涨跌幅' },
  { value: 'status_label', label: '状态标签' },
  { value: 'coverage_ratio', label: '覆盖率' },
  { value: 'hhi', label: '集中度 HHI' },
]

const SCOPE_TYPE_LABELS: Record<string, string> = {
  market: '全市场',
  industry: '行业',
  concept: '概念',
}

/** 状态标签 chip 样式映射 */
function statusLabelChipClass(label: string | null | undefined): string {
  if (!label) return styles.chipDefault
  const l = label.toLowerCase()
  if (l.includes('breakout') || l.includes('up') || l.includes('strong')) return styles.chipSuccess
  if (l.includes('breakdown') || l.includes('down') || l.includes('weak')) return styles.chipDanger
  if (l.includes('neutral') || l.includes('flat')) return styles.chipDefault
  if (l.includes('mixed') || l.includes('divergen')) return styles.chipWarning
  return styles.chipInfo
}

/** 置信度 chip 样式 */
function confidenceChipClass(level: string | null | undefined): string {
  if (!level) return styles.chipDefault
  const l = level.toLowerCase()
  if (l.includes('high')) return styles.chipSuccess
  if (l.includes('medium') || l.includes('mid')) return styles.chipWarning
  if (l.includes('low')) return styles.chipDanger
  return styles.chipDefault
}

/** 安全格式化百分比（值已为百分比形式或小数形式，由后端决定；这里只做展示） */
function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(digits)}%`
}

/** 安全格式化数字 */
function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return v.toFixed(digits)
}

/** 安全截断 UUID（前 8 位） */
function fmtId(v: string | null | undefined): string {
  if (!v) return '—'
  return v.slice(0, 8)
}

/** 覆盖率条 */
function CoverageBar({ ratio }: { ratio: number | null | undefined }) {
  const pct = ratio !== null && ratio !== undefined ? Math.round(ratio * 100) : null
  return (
    <span className={styles.coverageBar}>
      {pct !== null ? (
        <>
          <span className={styles.coverageTrack}>
            <span
              className={styles.coverageFill}
              style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
            />
          </span>
          <span className={styles.metaValue}>{pct}%</span>
        </>
      ) : (
        <span className={styles.metricUnavailable}>—</span>
      )}
    </span>
  )
}

/** Scope 行：板块名 / 中位涨跌幅 / 广度 / 参与度 / 集中度 / 状态标签 / 置信度 */
function ScopeRow({
  scope,
  onClick,
}: {
  scope: ScopeResult
  onClick: (scope: ScopeResult) => void
}) {
  const median = scope.median_change_pct
  const medianCls = median === null ? styles.neutral : median > 0 ? styles.up : median < 0 ? styles.down : styles.neutral
  return (
    <tr onClick={() => onClick(scope)}>
      <td>{scope.scope_name ?? scope.scope_id ?? '—'}</td>
      <td className={`${styles.numCell} ${medianCls}`}>
        {median === null ? '—' : `${median >= 0 ? '+' : ''}${median.toFixed(2)}%`}
      </td>
      <td className={styles.numCell}>
        {scope.valid_count}/{scope.total_count}
      </td>
      <td>
        <CoverageBar ratio={scope.coverage_ratio} />
      </td>
      <td className={styles.numCell}>
        {scope.structure_breakout_count}/{scope.structure_breakdown_count}
      </td>
      <td className={styles.numCell}>
        {scope.chip_cross_up_count}/{scope.chip_cross_down_count}
      </td>
      <td className={styles.numCell}>{scope.dual_breakout_count}</td>
      <td className={styles.numCell}>{scope.dual_breakdown_count}</td>
      <td className={styles.numCell}>{fmtNum(scope.participation_median)}</td>
      <td className={styles.numCell}>{fmtPct(scope.abnormal_volume_pct)}</td>
      <td className={styles.numCell}>{fmtNum(scope.hhi, 4)}</td>
      <td className={styles.numCell}>{fmtPct(scope.top3_contribution)}</td>
      <td className={styles.numCell}>{fmtPct(scope.positive_coverage)}</td>
      <td className={styles.numCell}>{fmtPct(scope.negative_coverage)}</td>
      <td>
        <span className={`${styles.chip} ${statusLabelChipClass(scope.status_label)}`}>
          {scope.status_label ?? '—'}
        </span>
      </td>
      <td>
        <span className={`${styles.chip} ${confidenceChipClass(scope.confidence_level)}`}>
          {scope.confidence_level ?? '—'}
        </span>
      </td>
    </tr>
  )
}

/** 事件卡片 */
function EventCard({ event, onNavigate }: { event: EventTracking; onNavigate: (instrumentId: string) => void }) {
  return (
    <div className={styles.eventCard}>
      <div className={styles.eventHeader}>
        <span className={styles.eventType}>{event.event_type}</span>
        <span className={`${styles.chip} ${styles.chipInfo}`}>
          {EVENT_LIFECYCLE_LABELS[event.lifecycle] ?? event.lifecycle}
        </span>
      </div>
      <div className={styles.eventMeta}>
        <span>instrument: <code>{fmtId(event.instrument_id)}</code></span>
        {event.anchor_id && <span>anchor: <code>{fmtId(event.anchor_id)}</code></span>}
        {event.formed_at && <span>形成: {formatShanghaiTime(event.formed_at)}</span>}
        {event.confirmed_at && <span>确认: {formatShanghaiTime(event.confirmed_at)}</span>}
      </div>
      {event.trigger_price && (
        <div className={styles.eventTrigger}>
          trigger_price={event.trigger_price}
          {event.trigger_condition ? ` · ${event.trigger_condition}` : ''}
        </div>
      )}
      {event.reason_codes.length > 0 && (
        <div className={styles.reasonCodes}>
          {event.reason_codes.map((rc) => (
            <span key={rc} className={styles.reasonCode}>{rc}</span>
          ))}
        </div>
      )}
      <button
        type="button"
        className={styles.linkBtn}
        onClick={() => onNavigate(event.instrument_id)}
      >
        查看个股 →
      </button>
    </div>
  )
}

/** KPI 卡片 */
function KpiCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className={styles.kpiCard}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.kpiValue}>{value}</div>
      {sub && <div className={styles.kpiSub}>{sub}</div>}
    </div>
  )
}

export default function AuctionMarketPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const tradeDate = searchParams.get('trade_date') ?? undefined
  const [sort, setSort] = useState<SortKey>(
    (searchParams.get('sort') as SortKey) || 'median_change_pct',
  )

  const query = useAuctionMarket(tradeDate)

  const data = query.data

  // 客户端排序（前端只展示，不重算业务结论；后端默认按 median_change_pct 降序）
  const sortedIndustries = useMemo<ScopeResult[]>(() => {
    if (!data?.industry_scopes) return []
    const list = [...data.industry_scopes]
    list.sort((a, b) => {
      const av = a[sort]
      const bv = b[sort]
      if (av === null || av === undefined) return 1
      if (bv === null || bv === undefined) return -1
      if (typeof av === 'string' && typeof bv === 'string') {
        return bv.localeCompare(av)
      }
      return (bv as number) - (av as number)
    })
    return list
  }, [data?.industry_scopes, sort])

  const sortedConcepts = useMemo<ScopeResult[]>(() => {
    if (!data?.concept_scopes) return []
    const list = [...data.concept_scopes]
    list.sort((a, b) => {
      const av = a[sort]
      const bv = b[sort]
      if (av === null || av === undefined) return 1
      if (bv === null || bv === undefined) return -1
      if (typeof av === 'string' && typeof bv === 'string') {
        return bv.localeCompare(av)
      }
      return (bv as number) - (av as number)
    })
    return list
  }, [data?.concept_scopes, sort])

  const handleSortChange = (s: SortKey) => {
    setSort(s)
    const params = new URLSearchParams(searchParams)
    params.set('sort', s)
    setSearchParams(params, { replace: true })
  }

  const handleSelectScope = (scope: ScopeResult) => {
    if (scope.scope_id) {
      navigate(`/auction/board/${scope.scope_id}`)
    }
  }

  const handleNavigateInstrument = (instrumentId: string) => {
    // 通过 instrument_id 反查 symbol 不在前端完成，跳转个股详情页（带 instrument_id 提示）
    // 个股级 API 需要 symbol，这里跳到 /stock/:symbol 由其他入口选择；
    // 若未来支持 instrument_id → symbol 映射，可在此扩展
    navigate(`/auction/stock/${instrumentId}`)
  }

  // 错误态
  if (query.isError) {
    const err = extractAuctionError(query.error)
    return (
      <div className={styles.auctionPage}>
        <div className={styles.stateBox}>
          <div className={styles.stateTitle}>竞价市场页加载失败</div>
          <div className={styles.stateDesc}>{err.message}</div>
          {err.requestId && <div className={styles.stateRequestId}>request_id={err.requestId}</div>}
        </div>
      </div>
    )
  }

  // 加载态
  if (query.isLoading || !data) {
    return (
      <div className={styles.auctionPage}>
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>加载竞价市场数据...</div>
        </div>
      </div>
    )
  }

  const market = data.market_scope

  return (
    <div className={styles.auctionPage}>
      {/* 顶部信息栏：trade_date / algorithm_version / publication_id / source run IDs / coverage / reason_codes */}
      <div className={styles.metaBar}>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>交易日:</span>
          <span className={styles.metaValue}>{data.trade_date}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>算法版本:</span>
          <span className={styles.metaValue}>{data.algorithm_version}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>publication_id:</span>
          <span className={styles.metaValue}>{fmtId(data.publication_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>scan_run_id:</span>
          <span className={styles.metaValue}>{fmtId(data.scan_run_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>source_core_run_id:</span>
          <span className={styles.metaValue}>{fmtId(data.source_core_run_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>source_chip_run_id:</span>
          <span className={styles.metaValue}>{fmtId(data.source_chip_run_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>覆盖率:</span>
          <CoverageBar ratio={data.coverage} />
        </span>
        {data.reason_codes.length > 0 && (
          <div className={styles.reasonCodes}>
            {data.reason_codes.map((rc) => (
              <span key={rc} className={styles.reasonCode}>{rc}</span>
            ))}
          </div>
        )}
      </div>

      {/* 市场 KPI（突破/破位广度 + 参与度 + 集中度） */}
      {market && (
        <div className={styles.kpiGrid}>
          <KpiCard
            label="有效 / 总数"
            value={`${market.valid_count} / ${market.total_count}`}
            sub={`覆盖率 ${(market.coverage_ratio * 100).toFixed(1)}%`}
          />
          <KpiCard
            label="结构突破 / 破位"
            value={`${market.structure_breakout_count} / ${market.structure_breakdown_count}`}
            sub={`双突破 ${market.dual_breakout_count} · 双破位 ${market.dual_breakdown_count}`}
          />
          <KpiCard
            label="筹码上穿 / 下穿"
            value={`${market.chip_cross_up_count} / ${market.chip_cross_down_count}`}
          />
          <KpiCard
            label="中位涨跌幅"
            value={market.median_change_pct === null ? '—' : `${market.median_change_pct >= 0 ? '+' : ''}${market.median_change_pct.toFixed(2)}%`}
            sub={`等权 ${fmtPct(market.equal_weight_change_pct)} · 加权 ${fmtPct(market.amount_weight_change_pct)}`}
          />
          <KpiCard
            label="参与度中位"
            value={fmtNum(market.participation_median)}
            sub={`异常量比 ${fmtPct(market.abnormal_volume_pct)}`}
          />
          <KpiCard
            label="集中度 HHI"
            value={fmtNum(market.hhi, 4)}
            sub={`Top3 ${fmtPct(market.top3_contribution)} · 领先中位差 ${fmtNum(market.leader_median_gap)}`}
          />
          <KpiCard
            label="上涨覆盖"
            value={fmtPct(market.positive_coverage)}
            sub={`下跌覆盖 ${fmtPct(market.negative_coverage)}`}
          />
          <KpiCard
            label="高开 / 平开 / 低开"
            value={`${market.open_high_count} / ${market.open_flat_count} / ${market.open_low_count}`}
          />
          <KpiCard
            label="市场状态"
            value={market.status_label ?? '—'}
            sub={`置信度 ${market.confidence_level ?? '—'}`}
          />
        </div>
      )}

      {/* 工具行：排序 */}
      <div className={styles.toolbar}>
        <span className={styles.toolbarLabel}>排序：</span>
        {SORT_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            className={`${styles.btn} ${sort === opt.value ? styles.btnPrimary : ''}`}
            onClick={() => handleSortChange(opt.value)}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* 行业排行 */}
      <div>
        <div className={styles.sectionTitle}>
          行业排行（{sortedIndustries.length}）
        </div>
        {sortedIndustries.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无行业 scope 数据</div>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className={styles.scopeTable}>
              <thead>
                <tr>
                  <th>板块名</th>
                  <th>中位涨跌幅</th>
                  <th>有效/总数</th>
                  <th>覆盖率</th>
                  <th>结构突破/破位</th>
                  <th>筹码上穿/下穿</th>
                  <th>双突破</th>
                  <th>双破位</th>
                  <th>参与度中位</th>
                  <th>异常量比</th>
                  <th>HHI</th>
                  <th>Top3 贡献</th>
                  <th>上涨覆盖</th>
                  <th>下跌覆盖</th>
                  <th>状态标签</th>
                  <th>置信度</th>
                </tr>
              </thead>
              <tbody>
                {sortedIndustries.map((scope) => (
                  <ScopeRow key={scope.id} scope={scope} onClick={handleSelectScope} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 概念排行 */}
      <div>
        <div className={styles.sectionTitle}>
          概念排行（{sortedConcepts.length}）
        </div>
        {sortedConcepts.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无概念 scope 数据</div>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className={styles.scopeTable}>
              <thead>
                <tr>
                  <th>板块名</th>
                  <th>中位涨跌幅</th>
                  <th>有效/总数</th>
                  <th>覆盖率</th>
                  <th>结构突破/破位</th>
                  <th>筹码上穿/下穿</th>
                  <th>双突破</th>
                  <th>双破位</th>
                  <th>参与度中位</th>
                  <th>异常量比</th>
                  <th>HHI</th>
                  <th>Top3 贡献</th>
                  <th>上涨覆盖</th>
                  <th>下跌覆盖</th>
                  <th>状态标签</th>
                  <th>置信度</th>
                </tr>
              </thead>
              <tbody>
                {sortedConcepts.map((scope) => (
                  <ScopeRow key={scope.id} scope={scope} onClick={handleSelectScope} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Top 事件列表 */}
      <div>
        <div className={styles.sectionTitle}>
          Top 事件（{data.top_events.length}）
        </div>
        {data.top_events.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无竞价事件</div>
          </div>
        ) : (
          <div className={styles.eventList}>
            {data.top_events.map((ev) => (
              <EventCard
                key={ev.id}
                event={ev}
                onNavigate={handleNavigateInstrument}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
