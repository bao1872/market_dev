// [MarketDashboard] - 行业/概念板块页（共用主体，scopeType 区分）
// industry：显示 L1/L2/L3 selector；concept：不显示层级 selector、不出现“申万”字样。
// 布局：左/上 = 排行榜，右/下 = 选中板块详情，底部 = 重点板块比较（最多 20，默认 10 交易日）。
import { useMemo, useState } from 'react'
import { useMarketRankings, useMarketScopeDetail, useMarketCompare } from '@/hooks/useMarketDashboardApi'
import DashboardTabs from './DashboardTabs'
import DashboardState, { type DashboardStateKind } from './DashboardState'
import BreadthChart, { type BreadthLineSpec } from './BreadthChart'
import CompareChart from './CompareChart'
import RankingTable from './RankingTable'
import {
  classifyDashboardError,
  validateCompareSelection,
  MAX_COMPARE_BOARDS,
} from './dashboardLogic'
import { extractMarketDashboardError } from '@/api/marketDashboard'
import type { BreadthPoint, HierarchyLevel, ScopeType } from './types'
import styles from './dashboard.module.scss'

const DETAIL_LONG: BreadthLineSpec[] = [
  { field: 'ma20', label: 'MA20', color: '#2962ff', scale: 'left' },
  { field: 'ma50', label: 'MA50', color: '#00b28a', scale: 'left' },
  { field: 'ma120', label: 'MA120', color: '#f59e0b', scale: 'left' },
  { field: 'ew_index', label: '等权指数', color: '#111827', scale: 'right' },
]
const DETAIL_SHORT: BreadthLineSpec[] = [
  { field: 'ma5', label: 'MA5', color: '#2962ff', scale: 'left' },
  { field: 'ma10', label: 'MA10', color: '#00b28a', scale: 'left' },
]
const DETAIL_REFS = [
  { price: 0.8, color: '#ef4444', label: '80%' },
  { price: 0.2, color: '#22c55e', label: '20%' },
]

const LEVELS: HierarchyLevel[] = ['L1', 'L2', 'L3']

interface CompareItem {
  id: string
  name: string
}

export default function BoardDashboardPage({ scopeType }: { scopeType: ScopeType }) {
  const isIndustry = scopeType === 'industry'
  const [level, setLevel] = useState<HierarchyLevel>('L1')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [compare, setCompare] = useState<CompareItem[]>([])
  const [compareMsg, setCompareMsg] = useState<string | null>(null)

  const rankings = useMarketRankings(scopeType, isIndustry ? level : null)
  const detail = useMarketScopeDetail(selectedId, 250)
  const compareIds = useMemo(() => compare.map((c) => c.id), [compare])
  const compareQuery = useMarketCompare(compareIds, 10)

  const rankingsErr = rankings.isError ? classifyDashboardError(extractMarketDashboardError(rankings.error)) : null
  const detailErr = detail.isError ? classifyDashboardError(extractMarketDashboardError(detail.error)) : null
  const compareErr = compareQuery.isError ? classifyDashboardError(extractMarketDashboardError(compareQuery.error)) : null

  const addCompare = (item: CompareItem) => {
    setCompareMsg(null)
    if (compare.some((c) => c.id === item.id)) return
    const check = validateCompareSelection([...compare.map((c) => c.id), item.id])
    if (!check.ok) {
      setCompareMsg(check.message ?? `最多比较 ${MAX_COMPARE_BOARDS} 个板块`)
      return
    }
    setCompare((prev) => [...prev, item])
  }
  const removeCompare = (id: string) => setCompare((prev) => prev.filter((c) => c.id !== id))
  const clearCompare = () => {
    setCompare([])
    setCompareMsg(null)
  }

  const detailPoints = (detail.data?.series ?? []) as BreadthPoint[]
  const meta = detail.data?.metadata

  const rankState: DashboardStateKind | null = rankings.isLoading
    ? 'loading'
    : rankingsErr?.kind ?? null
  const emptyRanking =
    !rankState && rankings.data != null && rankings.data.top.length === 0 && rankings.data.bottom.length === 0

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.pageTitle}>{isIndustry ? '行业板块' : '概念板块'}</h1>
        <DashboardTabs />
        {isIndustry && (
          <div className={styles.levelSelector}>
            <span className={styles.levelLabel}>行业层级：</span>
            {LEVELS.map((lv) => (
              <button
                key={lv}
                type="button"
                className={level === lv ? `${styles.levelBtn} ${styles.levelBtnActive}` : styles.levelBtn}
                onClick={() => setLevel(lv)}
              >
                {lv}
              </button>
            ))}
          </div>
        )}
        {!isIndustry && <span className={styles.taxonomyNote}>同花顺概念 / 问财概念</span>}
      </div>

      <div className={styles.boardLayout}>
        {/* 左：排行榜 */}
        <div className={styles.rankCol}>
          {rankState && (
            <DashboardState
              kind={rankState}
              desc={
                rankState === 'forbidden'
                  ? '你当前没有市场数据访问权限'
                  : rankState === 'not-found'
                    ? '板块排行数据尚未生成'
                    : rankingsErr?.detail || '板块排行加载失败'
              }
              onRetry={() => void rankings.refetch()}
            />
          )}
          {!rankState && emptyRanking && <DashboardState kind="empty" desc="当前层级暂无可比较板块" />}
          {!rankState && !emptyRanking && (
            <>
              <RankingTable
                title="Top 10（MA5 5日变化领先）"
                items={rankings.data?.top ?? []}
                selectedId={selectedId}
                onSelect={setSelectedId}
                onAddCompare={(it) => addCompare({ id: it.board_id, name: it.board_name })}
              />
              <RankingTable
                title="Bottom 10（MA5 5日变化落后）"
                items={rankings.data?.bottom ?? []}
                selectedId={selectedId}
                onSelect={setSelectedId}
                onAddCompare={(it) => addCompare({ id: it.board_id, name: it.board_name })}
              />
            </>
          )}
        </div>

        {/* 右：选中板块详情 */}
        <div className={styles.detailCol}>
          {!selectedId && <DashboardState kind="empty" desc="点击左侧板块查看详情历史" />}
          {selectedId && detail.isLoading && <DashboardState kind="loading" />}
          {selectedId && detail.isError && (
            <DashboardState
              kind={detailErr!.kind}
              desc={
                detailErr!.kind === 'forbidden'
                  ? '你当前没有市场数据访问权限'
                  : detailErr!.kind === 'not-found'
                    ? '该板块当前不可用或没有复盘数据'
                    : detailErr!.detail || '板块详情加载失败'
              }
              onRetry={() => void detail.refetch()}
            />
          )}
          {selectedId && detail.data && meta && (
            <div className={styles.detailCard}>
              <div className={styles.detailHead}>
                <div>
                  <div className={styles.detailName}>{meta.name}</div>
                  <div className={styles.detailMeta}>
                    {meta.type} · {meta.hierarchy_level} · membership {meta.membership_version}
                  </div>
                  <div className={styles.detailMeta}>投影日期：{detail.data.projection_trade_date ?? '—'}</div>
                </div>
                <button type="button" className={styles.btn} onClick={() => addCompare({ id: meta.board_id, name: meta.name })}>
                  加入比较
                </button>
              </div>
              <section className={styles.chartCard}>
                <div className={styles.chartTitle}>长周期（MA20 / MA50 / MA120 + 等权指数）</div>
                {detailPoints.length > 0 ? (
                  <BreadthChart points={detailPoints} series={DETAIL_LONG} height={300} />
                ) : (
                  <DashboardState kind="empty" desc="暂无详情数据" />
                )}
              </section>
              <section className={styles.chartCard}>
                <div className={styles.chartTitle}>短周期（MA5 / MA10，参考线 80% / 20%）</div>
                <BreadthChart points={detailPoints} series={DETAIL_SHORT} referenceLines={DETAIL_REFS} height={260} />
              </section>
            </div>
          )}
        </div>
      </div>

      {/* 底部：重点板块比较 */}
      <section className={styles.compareSection}>
        <div className={styles.compareHead}>
          <div className={styles.chartTitle}>重点板块比较（最多 {MAX_COMPARE_BOARDS} 个，默认 10 交易日）</div>
          {compare.length > 0 && (
            <button type="button" className={styles.btnGhost} onClick={clearCompare}>
              清空
            </button>
          )}
        </div>
        {compare.length > 0 && (
          <div className={styles.chips}>
            {compare.map((c) => (
              <span key={c.id} className={styles.chip}>
                {c.name}
                <button
                  type="button"
                  className={styles.chipRemove}
                  onClick={() => removeCompare(c.id)}
                  aria-label="移除"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        {compareMsg && <div className={styles.compareMsg}>{compareMsg}</div>}
        {compareQuery.isError && (
          <DashboardState
            kind={compareErr!.kind}
            desc={compareErr!.detail || '比较数据加载失败'}
            onRetry={() => void compareQuery.refetch()}
          />
        )}
        {compareQuery.isLoading && <DashboardState kind="loading" />}
        {compareQuery.data && <CompareChart boards={compareQuery.data.boards} height={360} />}
        {!compareQuery.isLoading && !compareQuery.isError && compare.length === 0 && (
          <DashboardState kind="empty" desc="从排行榜或详情中加入板块以比较其等权指数" />
        )}
      </section>
    </div>
  )
}
