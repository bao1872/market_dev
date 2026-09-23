// [MarketDashboard][R3D] - 板块比较页（/review/compare）
//
// 设计约束（与 R3D0 backend additive 合同一致）：
//   - 数据唯一 server owner = GET /v1/market-dashboard/compare（useMarketCompare）；
//     前端绝不自行算 MA / delta / member_count，绝不 per-board detail / Explorer 请求。
//   - response boards 顺序 = request basket 顺序；前端不得对 boards 排序/重排。
//   - 比较篮只存最小身份 {id,name,type}（Zustand），remove/clear 不写回 API 值。
//   - 矩阵列顺序锁死（comparePageConfig.COMPARE_MATRIX_COLUMNS）；类型/数值只展示。
//   - 空篮：不调 API，诚实空态 + SPA Link（保留 session basket）。
//
// [PANJI-REVIEW-UI-RUNTIME-PARITY-FIX] CSS Module 绑定修正：
//   vite localsConvention = camelCaseOnly → 所有 kebab 选择器只能通过 camelCase 访问。
//   下方 className 统一用 styles.compareSection / styles.projDate ... 形式（camelCase 访问，不再用 kebab 中括号查找）。
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMarketCompare } from '@/hooks/useMarketDashboardApi'
import { useCompareBasketStore } from '@/store/compareBasket'
import { extractMarketDashboardError } from '@/api/marketDashboard'
import CompareChart from './CompareChart'
import DashboardState from './DashboardState'
import DashboardTabs from './DashboardTabs'
import { classifyDashboardError, deltaDirection, formatBreadth, formatDelta } from './dashboardLogic'
import type { DeltaDirection } from './dashboardLogic'
import {
  COMPARE_DEFAULT_RANGE,
  COMPARE_MATRIX_COLUMNS,
  COMPARE_RANGES,
  boardTypeLabel,
  formatMemberCount,
  type CompareMatrixColumn,
  type CompareRange,
} from './comparePageConfig'
import type { CompareBoard } from './types'
import styles from './dashboard.module.scss'

const COLUMN_LABELS: Record<CompareMatrixColumn, string> = {
  name: '名称',
  type: '类型',
  ma5: 'MA5',
  ma5_delta: '5日Δ',
  ma10: 'MA10',
  ma10_delta: '5日Δ',
  ma20: 'MA20',
  ma50: 'MA50',
  ma120: 'MA120',
  member_count: '成员数',
}

function matrixCell(col: CompareMatrixColumn, board: CompareBoard): { text: string; dir: DeltaDirection | null } {
  switch (col) {
    case 'name':
      return { text: board.board_name, dir: null }
    case 'type':
      return { text: boardTypeLabel(board.board_type), dir: null }
    case 'ma5':
      return { text: formatBreadth(board.ma5), dir: null }
    case 'ma10':
      return { text: formatBreadth(board.ma10), dir: null }
    case 'ma20':
      return { text: formatBreadth(board.ma20), dir: null }
    case 'ma50':
      return { text: formatBreadth(board.ma50), dir: null }
    case 'ma120':
      return { text: formatBreadth(board.ma120), dir: null }
    case 'ma5_delta':
      return { text: formatDelta(board.ma5_delta), dir: deltaDirection(board.ma5_delta) }
    case 'ma10_delta':
      return { text: formatDelta(board.ma10_delta), dir: deltaDirection(board.ma10_delta) }
    case 'member_count':
      return { text: formatMemberCount(board.member_count), dir: null }
  }
}

export default function ComparePage() {
  const items = useCompareBasketStore((s) => s.items)
  const remove = useCompareBasketStore((s) => s.remove)
  const clear = useCompareBasketStore((s) => s.clear)

  const [range, setRange] = useState<CompareRange>(COMPARE_DEFAULT_RANGE)
  // 请求顺序 = basket 顺序（backend response 顺序已冻结为 request board_ids 顺序）。
  const boardIds = useMemo(() => items.map((i) => i.id), [items])

  const compare = useMarketCompare(boardIds, range)
  const data = compare.data
  const isEmpty = items.length === 0
  const error = compare.error ? classifyDashboardError(extractMarketDashboardError(compare.error)) : null

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.pageTitle}>板块对比</h1>
        <DashboardTabs />
        {data?.projection_trade_date && (
          <span className={styles.projDate}>数据日期 {data.projection_trade_date}</span>
        )}
      </div>

      {/* 比较篮：chips 保持 store 顺序；remove/clear 只改 Zustand identity store。 */}
      <div className={styles.compareSection}>
        <div className={styles.compareHead}>
          <span className={styles.rankTitle}>对比篮</span>
          {items.length > 0 && (
            <button type="button" className={styles.btnGhost} onClick={clear}>
              清空全部
            </button>
          )}
        </div>

        {items.length === 0 ? (
          <p className={styles.taxonomyNote}>尚未选择对比板块</p>
        ) : (
          <div className={styles.chips}>
            {items.map((it) => (
              <span key={it.id} className={styles.chip}>
                {it.name}
                <span className={styles.taxonomyNote}>{boardTypeLabel(it.type)}</span>
                <button
                  type="button"
                  className={styles.chipRemove}
                  onClick={() => remove(it.id)}
                  aria-label={`移除 ${it.name}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        <div className={styles.rangeSelector}>
          <span className={styles.rangeLabel}>区间</span>
          {COMPARE_RANGES.map((r) => (
            <button
              key={r}
              type="button"
              className={r === range ? `${styles.rangeBtn} ${styles.rangeBtnActive}` : styles.rangeBtn}
              aria-pressed={r === range}
              onClick={() => setRange(r)}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {/* 空篮：不调 API，诚实空态 + SPA Link（保留 session basket，不整页 reload）。 */}
      {isEmpty ? (
        <div className={styles.compareSection}>
          <DashboardState kind="empty" desc="尚未选择对比板块" />
          <div className={styles.compareLinkRow}>
            <Link className={styles.summaryLink} to="/review/industry?hierarchy_level=L1">
              前往行业
            </Link>
            <Link className={styles.summaryLink} to="/review/concept">
              前往概念
            </Link>
          </div>
        </div>
      ) : compare.isLoading ? (
        <DashboardState kind="loading" />
      ) : error ? (
        <DashboardState
          kind={error.kind}
          desc={error.kind === 'not-found' ? '对比列表中存在当前不可用的板块，请移除后重试' : error.detail}
          onRetry={() => compare.refetch()}
        />
      ) : data ? (
        <>
          {/* 图：复用 CompareChart（不写第二套 chart）；EW 独立归一，起点=100。 */}
          <div className={styles.chartCard}>
            <div className={styles.chartTitle}>板块等权指数对比</div>
            <div className={styles.cardHint}>各板块在当前显示区间独立归一，起点 = 100</div>
            <CompareChart boards={data.boards} height={360} />
          </div>

          {/* 矩阵：列顺序锁死；行顺序 = basket / API response 顺序（前端不重排）。 */}
          <div className={styles.rankTable}>
            <div className={styles.rankTitle}>比较矩阵</div>
            <div className={styles.detailSub}>
              数据日期 {data.projection_trade_date ?? '—'} · 5日前 {data.previous_trade_date ?? '—'} · 5日Δ
              使用统一市场投影交易日 T 与 T-5
            </div>
            <table className={styles.table}>
              <thead>
                <tr>
                  {COMPARE_MATRIX_COLUMNS.map((c) => (
                    <th key={c}>{COLUMN_LABELS[c]}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.boards.map((b) => (
                  <tr key={b.board_id}>
                    {COMPARE_MATRIX_COLUMNS.map((c) => {
                      const cell = matrixCell(c, b)
                      const alignLeft = c === 'name' || c === 'type'
                      const dirClass = cell.dir ? ` ${styles[cell.dir]}` : ''
                      const alignClass = alignLeft ? ` ${styles.nameCell}` : ''
                      return (
                        <td key={c} className={`${alignClass}${dirClass}`.trim()}>
                          {cell.text}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </div>
  )
}
