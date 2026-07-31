// [AuctionBoardPage] - 描述: 竞价分析板块级页面
// 路由 /auction/board/:boardId
// 后端完成全部计算，前端只展示、过滤和展开证据
// 接口必须显示：trade_date、algorithm_version、scan_run_id、coverage、reason_codes
//
// 展示：
// - 板块分布（高平低开、结构/筹码迁移）
// - 锚点迁移（突破/破位广度、双突破/双破位、阻力/支撑区）
// - 贡献/反例/未跟随股（top_instruments，按 change_pct 排序）
// - 样本和置信度（scope.coverage_ratio、status_label、confidence_level）
// - Top instruments（板块下按 change_pct 排序的个股列表）

import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useAuctionBoard, extractAuctionError } from './api'
import {
  EVENT_LIFECYCLE_LABELS,
  PARTICIPATION_LABELS,
  type EventTracking,
  type InstrumentResult,
  type ScopeResult,
} from './types'
import { formatShanghaiTime } from '@/utils/datetime'
import styles from './auction.module.scss'

type SortKey = 'change_pct' | 'auction_amount' | 'relative_volume_median_20d' | 'volume_percentile'

const SORT_OPTIONS: Array<{ value: SortKey; label: string }> = [
  { value: 'change_pct', label: '涨跌幅' },
  { value: 'auction_amount', label: '竞价金额' },
  { value: 'relative_volume_median_20d', label: '相对量比' },
  { value: 'volume_percentile', label: '量分位' },
]

/** 涨跌幅颜色 class */
function changePctClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return styles.neutral
  return v > 0 ? styles.up : v < 0 ? styles.down : styles.neutral
}

/** 安全格式化百分比 */
function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(digits)}%`
}

/** 安全格式化数字 */
function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return v.toFixed(digits)
}

/** 安全格式化金额（万元） */
function fmtAmount(v: string | null | undefined): string {
  if (!v) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return v
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(2)}万`
  return n.toFixed(0)
}

/** 安全截断 UUID（前 8 位） */
function fmtId(v: string | null | undefined): string {
  if (!v) return '—'
  return v.slice(0, 8)
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

/** 参与度 chip 样式 */
function participationChipClass(level: string | null | undefined): string {
  if (!level) return styles.chipDefault
  const l = level.toLowerCase()
  if (l.includes('high')) return styles.chipSuccess
  if (l.includes('medium') || l.includes('mid')) return styles.chipWarning
  if (l.includes('low')) return styles.chipDanger
  return styles.chipDefault
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

/** 板块分布条（高平低开） */
function OpenDistBar({ scope }: { scope: ScopeResult }) {
  const total = scope.open_high_count + scope.open_flat_count + scope.open_low_count
  if (total === 0) return <span className={styles.metricUnavailable}>—</span>
  const highPct = (scope.open_high_count / total) * 100
  const flatPct = (scope.open_flat_count / total) * 100
  const lowPct = (scope.open_low_count / total) * 100
  return (
    <div>
      <div className={styles.distBar}>
        <span className={styles.distSegUp} style={{ width: `${highPct}%` }} />
        <span className={styles.distSegFlat} style={{ width: `${flatPct}%` }} />
        <span className={styles.distSegDown} style={{ width: `${lowPct}%` }} />
      </div>
      <div className={styles.distLegend}>
        <span className={styles.distLegendItem}>
          <i className={styles.distSegUp} /> 高开 {scope.open_high_count} ({highPct.toFixed(1)}%)
        </span>
        <span className={styles.distLegendItem}>
          <i className={styles.distSegFlat} /> 平开 {scope.open_flat_count}
        </span>
        <span className={styles.distLegendItem}>
          <i className={styles.distSegDown} /> 低开 {scope.open_low_count} ({lowPct.toFixed(1)}%)
        </span>
      </div>
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

/** 个股结果行 */
function InstrumentRow({
  result,
  onClick,
}: {
  result: InstrumentResult
  onClick: (instrumentId: string) => void
}) {
  const changeCls = changePctClass(result.change_pct)
  return (
    <tr onClick={() => onClick(result.instrument_id)}>
      <td>
        <code className={styles.metaValue}>{fmtId(result.instrument_id)}</code>
      </td>
      <td className={`${styles.numCell} ${changeCls}`}>
        {result.change_pct === null
          ? '—'
          : `${result.change_pct >= 0 ? '+' : ''}${result.change_pct.toFixed(2)}%`}
      </td>
      <td className={styles.numCell}>{result.final_auction_price ?? '—'}</td>
      <td className={styles.numCell}>{result.prev_close ?? '—'}</td>
      <td className={styles.numCell}>{fmtAmount(result.auction_amount)}</td>
      <td className={styles.numCell}>{fmtNum(result.relative_volume_median_20d)}</td>
      <td className={styles.numCell}>{fmtNum(result.volume_percentile)}</td>
      <td className={styles.numCell}>{fmtNum(result.atr_distance_pct)}</td>
      <td>
        <span className={`${styles.chip} ${participationChipClass(result.participation_level)}`}>
          {result.participation_level
            ? PARTICIPATION_LABELS[result.participation_level] ?? result.participation_level
            : '—'}
        </span>
      </td>
      <td>{result.structure_position ?? '—'}</td>
      <td>{result.chip_position ?? '—'}</td>
      <td>{result.event_type ?? '—'}</td>
      <td>
        {result.event_lifecycle ? (
          <span className={`${styles.chip} ${styles.chipInfo}`}>
            {EVENT_LIFECYCLE_LABELS[result.event_lifecycle] ?? result.event_lifecycle}
          </span>
        ) : (
          '—'
        )}
      </td>
      <td>
        {result.is_limit_up ? (
          <span className={`${styles.chip} ${styles.chipDanger}`}>涨停</span>
        ) : result.is_limit_down ? (
          <span className={`${styles.chip} ${styles.chipSuccess}`}>跌停</span>
        ) : result.is_suspended ? (
          <span className={`${styles.chip} ${styles.chipDefault}`}>停牌</span>
        ) : result.is_ex_right ? (
          <span className={`${styles.chip} ${styles.chipWarning}`}>除权</span>
        ) : (
          '—'
        )}
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
        {event.failed_at && <span>失效: {formatShanghaiTime(event.failed_at)}</span>}
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

export default function AuctionBoardPage() {
  const { boardId } = useParams<{ boardId: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const tradeDate = searchParams.get('trade_date') ?? undefined
  const [sort, setSort] = useState<SortKey>(
    (searchParams.get('sort') as SortKey) || 'change_pct',
  )

  const query = useAuctionBoard(boardId, tradeDate)
  const data = query.data

  // 客户端排序（前端只展示，不重算业务结论；后端默认按 change_pct 降序）
  const sortedInstruments = useMemo<InstrumentResult[]>(() => {
    if (!data?.top_instruments) return []
    const list = [...data.top_instruments]
    list.sort((a, b) => {
      const av = a[sort]
      const bv = b[sort]
      if (av === null || av === undefined) return 1
      if (bv === null || bv === undefined) return -1
      // auction_amount 在 TS 中是字符串，转数字比较
      if (typeof av === 'string' && typeof bv === 'string') {
        return Number(bv) - Number(av)
      }
      return (bv as number) - (av as number)
    })
    return list
  }, [data?.top_instruments, sort])

  const handleSortChange = (s: SortKey) => {
    setSort(s)
    const params = new URLSearchParams(searchParams)
    params.set('sort', s)
    setSearchParams(params, { replace: true })
  }

  const handleNavigateInstrument = (instrumentId: string) => {
    navigate(`/auction/stock/${instrumentId}`)
  }

  // 缺少 boardId 参数
  if (!boardId) {
    return (
      <div className={styles.auctionPage}>
        <div className={styles.stateBox}>
          <div className={styles.stateTitle}>缺少板块 ID</div>
          <div className={styles.stateDesc}>URL 路径必须包含 boardId，例如 /auction/board/{`{boardId}`}</div>
          <Link to="/auction" className={styles.linkBtn}>返回市场页 →</Link>
        </div>
      </div>
    )
  }

  // 错误态
  if (query.isError) {
    const err = extractAuctionError(query.error)
    return (
      <div className={styles.auctionPage}>
        <div className={styles.breadcrumb}>
          <Link to="/auction" className={styles.breadcrumbItem}>市场</Link>
          <span className={styles.breadcrumbSep}>/</span>
          <span className={styles.breadcrumbCurrent}>板块 {fmtId(boardId)}</span>
        </div>
        <div className={styles.stateBox}>
          <div className={styles.stateTitle}>竞价板块页加载失败</div>
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
        <div className={styles.breadcrumb}>
          <Link to="/auction" className={styles.breadcrumbItem}>市场</Link>
          <span className={styles.breadcrumbSep}>/</span>
          <span className={styles.breadcrumbCurrent}>板块 {fmtId(boardId)}</span>
        </div>
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>加载竞价板块数据...</div>
        </div>
      </div>
    )
  }

  const scope = data.scope

  return (
    <div className={styles.auctionPage}>
      {/* 面包屑 */}
      <div className={styles.breadcrumb}>
        <Link to="/auction" className={styles.breadcrumbItem}>市场</Link>
        <span className={styles.breadcrumbSep}>/</span>
        <span className={styles.breadcrumbCurrent}>
          板块{scope?.scope_name ? ` · ${scope.scope_name}` : ` ${fmtId(boardId)}`}
        </span>
      </div>

      {/* 顶部信息栏：trade_date / algorithm_version / scan_run_id / coverage / reason_codes */}
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
          <span className={styles.metaLabel}>scan_run_id:</span>
          <span className={styles.metaValue}>{fmtId(data.scan_run_id)}</span>
        </span>
        <span className={styles.metaItem}>
          <span className={styles.metaLabel}>板块 ID:</span>
          <span className={styles.metaValue}>{boardId}</span>
        </span>
        {scope && (
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>覆盖率:</span>
            <CoverageBar ratio={scope.coverage_ratio} />
          </span>
        )}
        {data.reason_codes.length > 0 && (
          <div className={styles.reasonCodes}>
            {data.reason_codes.map((rc) => (
              <span key={rc} className={styles.reasonCode}>{rc}</span>
            ))}
          </div>
        )}
      </div>

      {/* 板块分布（高平低开、结构/筹码迁移）+ 锚点迁移 + 样本与置信度 */}
      {scope ? (
        <>
          <div className={styles.kpiGrid}>
            <KpiCard
              label="有效 / 总数"
              value={`${scope.valid_count} / ${scope.total_count}`}
              sub={`覆盖率 ${(scope.coverage_ratio * 100).toFixed(1)}%`}
            />
            <KpiCard
              label="中位涨跌幅"
              value={
                scope.median_change_pct === null
                  ? '—'
                  : `${scope.median_change_pct >= 0 ? '+' : ''}${scope.median_change_pct.toFixed(2)}%`
              }
              sub={`P25 ${fmtPct(scope.p25_change_pct)} · P75 ${fmtPct(scope.p75_change_pct)}`}
            />
            <KpiCard
              label="等权涨跌幅"
              value={fmtPct(scope.equal_weight_change_pct)}
              sub={`加权 ${fmtPct(scope.amount_weight_change_pct)}`}
            />
            <KpiCard
              label="结构突破 / 破位"
              value={`${scope.structure_breakout_count} / ${scope.structure_breakdown_count}`}
              sub={`双突破 ${scope.dual_breakout_count} · 双破位 ${scope.dual_breakdown_count}`}
            />
            <KpiCard
              label="筹码上穿 / 下穿"
              value={`${scope.chip_cross_up_count} / ${scope.chip_cross_down_count}`}
            />
            <KpiCard
              label="阻力区 / 支撑区"
              value={`${scope.resistance_zone_count} / ${scope.support_zone_count}`}
            />
            <KpiCard
              label="参与度中位"
              value={fmtNum(scope.participation_median)}
              sub={`异常量比 ${fmtPct(scope.abnormal_volume_pct)}`}
            />
            <KpiCard
              label="集中度 HHI"
              value={fmtNum(scope.hhi, 4)}
              sub={`Top3 ${fmtPct(scope.top3_contribution)} · Top5 ${fmtPct(scope.top5_contribution)}`}
            />
            <KpiCard
              label="上涨覆盖"
              value={fmtPct(scope.positive_coverage)}
              sub={`下跌覆盖 ${fmtPct(scope.negative_coverage)}`}
            />
            <KpiCard
              label="领先中位差"
              value={fmtNum(scope.leader_median_gap)}
              sub={`离散度 ${fmtNum(scope.dispersion)}`}
            />
            <KpiCard
              label="板块状态"
              value={scope.status_label ?? '—'}
              sub={`置信度 ${scope.confidence_level ?? '—'}`}
            />
          </div>

          {/* 高平低开分布条 */}
          <div>
            <div className={styles.sectionTitle}>高平低开分布</div>
            <OpenDistBar scope={scope} />
          </div>

          {/* 状态标签 + 置信度 */}
          <div className={styles.toolbar}>
            <span className={styles.toolbarLabel}>板块判定：</span>
            <span className={`${styles.chip} ${statusLabelChipClass(scope.status_label)}`}>
              {scope.status_label ?? '—'}
            </span>
            <span className={styles.toolbarLabel} style={{ marginLeft: 12 }}>置信度：</span>
            <span className={`${styles.chip} ${confidenceChipClass(scope.confidence_level)}`}>
              {scope.confidence_level ?? '—'}
            </span>
          </div>

          {scope.reason_codes.length > 0 && (
            <div className={styles.reasonCodes}>
              {scope.reason_codes.map((rc) => (
                <span key={rc} className={styles.reasonCode}>{rc}</span>
              ))}
            </div>
          )}
        </>
      ) : (
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>该板块在当日竞价 scan 中无 scope 数据</div>
        </div>
      )}

      {/* 工具行：排序 */}
      <div className={styles.toolbar}>
        <span className={styles.toolbarLabel}>个股排序：</span>
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

      {/* 贡献/反例/未跟随股 - Top instruments */}
      <div>
        <div className={styles.sectionTitle}>
          板块个股（{sortedInstruments.length}）
        </div>
        {sortedInstruments.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无板块个股数据</div>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className={styles.scopeTable}>
              <thead>
                <tr>
                  <th>instrument_id</th>
                  <th>涨跌幅</th>
                  <th>竞价价</th>
                  <th>前收</th>
                  <th>竞价金额</th>
                  <th>相对量比</th>
                  <th>量分位</th>
                  <th>ATR 距离</th>
                  <th>参与度</th>
                  <th>结构位置</th>
                  <th>筹码位置</th>
                  <th>事件类型</th>
                  <th>生命周期</th>
                  <th>标记</th>
                </tr>
              </thead>
              <tbody>
                {sortedInstruments.map((r) => (
                  <InstrumentRow
                    key={r.id}
                    result={r}
                    onClick={handleNavigateInstrument}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 板块相关事件 */}
      <div>
        <div className={styles.sectionTitle}>
          板块事件（{data.events.length}）
        </div>
        {data.events.length === 0 ? (
          <div className={styles.stateBox}>
            <div className={styles.stateDesc}>暂无板块事件</div>
          </div>
        ) : (
          <div className={styles.eventList}>
            {data.events.map((ev) => (
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
