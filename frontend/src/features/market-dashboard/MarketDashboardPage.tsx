// [MarketDashboard] - 大盘页（/review）
// 顶部：市场复盘 + 二级 Tab + 最后投影日期；四张卡片（顺序锁死）+ 长周期图 + 短周期图。
//
// [R3A] series 颜色身份统一由 shared palette 分配（普通 MA / EW 绝不使用 market.up/down）；
// 80%/20% 参考线使用 warning/muted 阈值语义色；EW 线更粗。完整 R3B 页面重构不在本轮。
import { useMemo } from 'react'
import { useMarketDashboard } from '@/hooks/useMarketDashboardApi'
import DashboardTabs from './DashboardTabs'
import DashboardState, { type DashboardStateKind } from './DashboardState'
import BreadthChart, { type BreadthLineSpec } from './BreadthChart'
import { BREADTH_REFERENCE_LINES, EW_LINE_WIDTH } from './chartTheme'
import { formatBreadth, formatEwIndex, classifyDashboardError } from './dashboardLogic'
import { extractMarketDashboardError } from '@/api/marketDashboard'
import type { BreadthPoint } from './types'
import styles from './dashboard.module.scss'

const LONG_SERIES: BreadthLineSpec[] = [
  { field: 'ma20', label: 'MA20', scale: 'left' },
  { field: 'ma50', label: 'MA50', scale: 'left' },
  { field: 'ma120', label: 'MA120', scale: 'left' },
  { field: 'ew_index', label: '等权指数', scale: 'right', lineWidth: EW_LINE_WIDTH },
]

const SHORT_SERIES: BreadthLineSpec[] = [
  { field: 'ma5', label: 'MA5', scale: 'left' },
  { field: 'ma10', label: 'MA10', scale: 'left' },
]

export default function MarketDashboardPage() {
  const query = useMarketDashboard(250)

  const errClass = query.isError ? classifyDashboardError(extractMarketDashboardError(query.error)) : null
  const state = useMemo<DashboardStateKind | null>(() => {
    if (query.isLoading) return 'loading'
    return errClass?.kind ?? null
  }, [query.isLoading, errClass])

  const cards = query.data?.cards
  const points = (query.data?.series ?? []) as BreadthPoint[]

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.pageTitle}>市场复盘</h1>
        <DashboardTabs />
        <span className={styles.projDate}>最后投影日期：{query.data?.projection_trade_date ?? '—'}</span>
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
          <div className={styles.cardGrid}>
            <Card label="MA20 线上占比" value={formatBreadth(cards?.ma20 ?? null)} />
            <Card label="MA50 线上占比" value={formatBreadth(cards?.ma50 ?? null)} />
            <Card label="MA5 线上占比" value={formatBreadth(cards?.ma5 ?? null)} />
            <Card label="全市场等权指数" value={formatEwIndex(cards?.equal_weight_index ?? null)} wide />
          </div>

          <section className={styles.chartCard}>
            <div className={styles.chartTitle}>长周期（MA20 / MA50 / MA120 + 等权指数）</div>
            {points.length > 0 ? (
              <BreadthChart points={points} series={LONG_SERIES} height={340} />
            ) : (
              <DashboardState kind="empty" desc="暂无长周期数据" />
            )}
          </section>

          <section className={styles.chartCard}>
            <div className={styles.chartTitle}>短周期（MA5 / MA10，参考线 80% / 20%）</div>
            <BreadthChart
              points={points}
              series={SHORT_SERIES}
              referenceLines={BREADTH_REFERENCE_LINES}
              height={300}
            />
          </section>
        </>
      )}
    </div>
  )
}

function Card({ label, value, wide }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={wide ? `${styles.card} ${styles.cardWide}` : styles.card}>
      <div className={styles.cardLabel}>{label}</div>
      <div className={styles.cardValue}>{value}</div>
    </div>
  )
}
