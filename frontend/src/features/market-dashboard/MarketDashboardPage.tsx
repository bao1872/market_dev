// [MarketDashboard][R3B] - 大盘页（/review）
//
// 结构（冻结）：Header（标题 + 4 个二级 tab + 真实数据日期）
//   → 时间范围（20/60/120/250，默认 250，窗口由 **server** 返回）
//   → 4 张 KPI 卡片（MA5 / MA20 / MA50 / EW，顺序锁死）
//   → 一张统一 chart（MA5/MA10/MA20/MA50/MA120 左轴 0..100% + EW 右轴）
//   → 2 列 summary：行业 Top5/Bottom5 | 概念 Top5/Bottom5
//
// 语义约束：
//   - 不伪造任何市场判断（实时 / 正常 / bull·bear / sentiment / capital tilt）；
//   - 左轴 0%..100% 由 shared chart foundation 呈现（数据仍 0..1，绝不 ×100）；
//   - legend hide/show 硬合同由 MultiLineChart 保证（toggle 只 applyOptions({visible})）；
//   - ranking 各自独立 loading/error/empty，单个失败不影响主图。
import { useMemo, useState } from 'react'
import { useMarketDashboard, useMarketRankings } from '@/hooks/useMarketDashboardApi'
import ReviewTopBar from './ReviewTopBar'
import DashboardState, { type DashboardStateKind } from './DashboardState'
import BreadthChart from './BreadthChart'
import MarketRankingSummary from './MarketRankingSummary'
import {
  EW_INDEX_BASE_HINT,
  MARKET_CARD_LABELS,
  MARKET_CARD_ORDER,
  MARKET_OVERVIEW_DEFAULT_RANGE,
  MARKET_OVERVIEW_RANGES,
  MARKET_OVERVIEW_SERIES,
  MARKET_RANKING_SUMMARIES,
  RANKING_SUMMARY_LIMIT,
  RANKING_SUMMARY_LOOKBACK,
  type MarketOverviewRange,
} from './marketOverviewConfig'
import { BREADTH_REFERENCE_LINES } from './chartTheme'
import { formatBreadth, formatEwIndex, classifyDashboardError } from './dashboardLogic'
import { extractMarketDashboardError } from '@/api/marketDashboard'
import type { BreadthPoint } from './types'
import styles from './dashboard.module.scss'

const INDUSTRY_SUMMARY = MARKET_RANKING_SUMMARIES[0]
const CONCEPT_SUMMARY = MARKET_RANKING_SUMMARIES[1]

/** 单个 ranking summary 的独立状态（不影响主图）。 */
function summaryState(query: { isLoading: boolean; isError: boolean; error: unknown }): DashboardStateKind | null {
  if (query.isLoading) return 'loading'
  if (query.isError) return classifyDashboardError(extractMarketDashboardError(query.error)).kind
  return null
}

export default function MarketDashboardPage() {
  // 时间范围：直接换 server 窗口（禁止先取 250 再前端 slice）。
  const [range, setRange] = useState<MarketOverviewRange>(MARKET_OVERVIEW_DEFAULT_RANGE)
  const query = useMarketDashboard(range)

  const industryRanking = useMarketRankings(
    INDUSTRY_SUMMARY.scopeType,
    INDUSTRY_SUMMARY.hierarchyLevel,
    RANKING_SUMMARY_LOOKBACK,
    RANKING_SUMMARY_LIMIT,
  )
  const conceptRanking = useMarketRankings(
    CONCEPT_SUMMARY.scopeType,
    CONCEPT_SUMMARY.hierarchyLevel,
    RANKING_SUMMARY_LOOKBACK,
    RANKING_SUMMARY_LIMIT,
  )

  const errClass = query.isError ? classifyDashboardError(extractMarketDashboardError(query.error)) : null
  const state = useMemo<DashboardStateKind | null>(() => {
    if (query.isLoading) return 'loading'
    return errClass?.kind ?? null
  }, [query.isLoading, errClass])

  const cards = query.data?.cards
  const points = (query.data?.series ?? []) as BreadthPoint[]

  return (
    <div className={styles.explorerPage}>
      <ReviewTopBar projectionDate={query.data?.projection_trade_date} />

      <div className={styles.rangeSelector} role="group" aria-label="时间范围">
        <span className={styles.rangeLabel}>时间范围：</span>
        {MARKET_OVERVIEW_RANGES.map((days) => (
          <button
            key={days}
            type="button"
            className={days === range ? `${styles.rangeBtn} ${styles.rangeBtnActive}` : styles.rangeBtn}
            aria-pressed={days === range}
            onClick={() => setRange(days)}
          >
            {days}
          </button>
        ))}
      </div>

      {state && (
        <DashboardState
          kind={state}
          desc={
            state === 'forbidden'
              ? '你当前没有市场数据访问权限'
              : state === 'not-found'
                ? '市场复盘数据尚未生成'
                : errClass?.detail || '市场复盘数据加载失败'
          }
          onRetry={state === 'forbidden' ? undefined : () => void query.refetch()}
        />
      )}

      {!state && (
        <>
          {/* KPI 卡片（顺序锁死：MA5 / MA20 / MA50 / EW） */}
          <div className={styles.cardGrid}>
            {MARKET_CARD_ORDER.map((key) => (
              <Card
                key={key}
                label={MARKET_CARD_LABELS[key]}
                value={key === 'ew' ? formatEwIndex(cards?.equal_weight_index ?? null) : formatBreadth(cards?.[key] ?? null)}
                hint={key === 'ew' ? EW_INDEX_BASE_HINT : undefined}
                wide={key === 'ew'}
              />
            ))}
          </div>

          {/* 统一 chart（single source：BreadthChart → MultiLineChart） */}
          <section className={styles.chartCard}>
            <div className={styles.chartTitle}>市场宽度（左轴 0–100%）与全市场等权指数（右轴）</div>
            {points.length > 0 ? (
              <BreadthChart
                points={points}
                series={[...MARKET_OVERVIEW_SERIES]}
                referenceLines={BREADTH_REFERENCE_LINES}
                height={380}
              />
            ) : (
              <DashboardState kind="empty" desc="当前时间范围暂无数据" />
            )}
          </section>

          {/* 行业 / 概念 summary（各自独立 loading/error/empty） */}
          <div className={styles.summaryGrid}>
            <MarketRankingSummary
              config={INDUSTRY_SUMMARY}
              top={industryRanking.data?.top ?? []}
              bottom={industryRanking.data?.bottom ?? []}
              state={summaryState(industryRanking)}
              errorDetail={industryRanking.isError ? extractMarketDashboardError(industryRanking.error).detail : null}
              onRetry={() => void industryRanking.refetch()}
            />
            <MarketRankingSummary
              config={CONCEPT_SUMMARY}
              top={conceptRanking.data?.top ?? []}
              bottom={conceptRanking.data?.bottom ?? []}
              state={summaryState(conceptRanking)}
              errorDetail={conceptRanking.isError ? extractMarketDashboardError(conceptRanking.error).detail : null}
              onRetry={() => void conceptRanking.refetch()}
            />
          </div>
        </>
      )}
    </div>
  )
}

function Card({
  label,
  value,
  hint,
  wide,
}: {
  label: string
  value: string
  hint?: string
  wide?: boolean
}) {
  return (
    <div className={wide ? `${styles.card} ${styles.cardWide}` : styles.card}>
      <div className={styles.cardLabel}>{label}</div>
      <div className={styles.cardValue}>{value}</div>
      {hint && <div className={styles.cardHint}>{hint}</div>}
    </div>
  )
}
