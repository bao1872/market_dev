// [ReviewInstrumentTable] - 描述: 个股表格组件（PRD §14.6 阶段四精简表）
// 字段：股票/板块角色/与板块关系/趋势/主要结构/短线结构/动量/量能/新鲜事件/贡献/自选 +/-
// 操作：打开 /stock/:symbol；加入/移除自选；加入本信号追踪；"查看全部"跳转 /market
// 复盘页不重新实现 99 字段列设置和导出（PRD §16）
import type { ReviewInstrument } from './types'
import styles from './review.module.scss'

const BOARD_ROLE_LABEL: Record<string, string> = {
  core: '龙头',
  second_line: '二线',
  elasticity: '弹性',
  follower: '跟随',
  laggard: '滞涨',
  unclassified: '未分类',
}

const RELATION_LABEL: Record<string, string> = {
  synchronized_strengthening: '同步加强',
  synchronized_weakening: '同步减弱',
  instrument_leads_scope: '个股领先',
  scope_strong_instrument_lags: '板块强个股滞',
  instrument_strong_scope_unsupported: '个股强无支撑',
  unconfirmed: '未确认',
}

/** 安全读取 payload 中的字符串字段 */
function readStr(payload: Record<string, unknown>, key: string): string {
  const v = payload?.[key]
  if (typeof v === 'string' && v) return v
  return '-'
}

/** 第一金字塔方向点：up/down/neutral */
function directionDot(payload: Record<string, unknown>, key: string) {
  const v = readStr(payload, key).toLowerCase()
  if (['up', 'bull', 'bullish', 'strong', 'positive'].includes(v)) {
    return <span className={`${styles.fpDot} ${styles.fpDotUp}`} />
  }
  if (['down', 'bear', 'bearish', 'weak', 'negative'].includes(v)) {
    return <span className={`${styles.fpDot} ${styles.fpDotDown}`} />
  }
  return <span className={`${styles.fpDot} ${styles.fpDotNeutral}`} />
}

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return n.toFixed(digits)
}

export interface ReviewInstrumentTableProps {
  items: ReviewInstrument[]
  activeSymbol?: string | null
  /** 已自选的 instrument_id 集合（用于 +/− 按钮态） */
  watchlistInstrumentIds?: Set<string>
  /** 自选操作进行中的 instrument_id 集合（防重复点击） */
  watchlistPendingIds?: Set<string>
  onNavigateToStock?: (inst: ReviewInstrument) => void
  onToggleWatchlist?: (inst: ReviewInstrument, add: boolean) => void
  onAddTracking?: (inst: ReviewInstrument) => void
  onViewAllInMarket?: () => void
}

export default function ReviewInstrumentTable({
  items,
  activeSymbol,
  watchlistInstrumentIds,
  watchlistPendingIds,
  onNavigateToStock,
  onToggleWatchlist,
  onAddTracking,
  onViewAllInMarket,
}: ReviewInstrumentTableProps) {
  if (items.length === 0) {
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>无代表股票</div>
        <div className={styles.stateDesc}>
          该信号未映射代表股票，可能原因：个股无第一金字塔数据或 coverage 不足
        </div>
      </div>
    )
  }
  return (
    <div className={styles.panelSection}>
      <div className={styles.panelSectionHeader}>
        <span className={styles.panelSectionTitle}>代表股票</span>
        {onViewAllInMarket && (
          <button
            type="button"
            className={styles.btnLink}
            onClick={onViewAllInMarket}
          >
            查看全部（跳转 /market）
          </button>
        )}
      </div>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>股票</th>
            <th>板块角色</th>
            <th>与板块关系</th>
            <th>趋势</th>
            <th>主要结构</th>
            <th>短线结构</th>
            <th>动量</th>
            <th>量能</th>
            <th>新鲜事件</th>
            <th className={styles.numCell}>贡献</th>
            <th>自选</th>
            <th>追踪</th>
          </tr>
        </thead>
        <tbody>
          {items.map((inst) => {
            const fp = inst.firstPyramidPayload ?? {}
            const events = inst.freshEventsPayload ?? {}
            const isWatched = watchlistInstrumentIds?.has(inst.instrumentId) ?? false
            const pending = watchlistPendingIds?.has(inst.instrumentId) ?? false
            const active = activeSymbol === inst.symbol
            return (
              <tr
                key={inst.id}
                className={active ? styles.tableRowActive : undefined}
              >
                <td>
                  <button
                    type="button"
                    className={styles.btnLink}
                    onClick={() => onNavigateToStock?.(inst)}
                  >
                    {inst.name}
                    <span className={styles.symbolText}> {inst.symbol}</span>
                  </button>
                </td>
                <td>{BOARD_ROLE_LABEL[inst.boardRole ?? ''] ?? inst.boardRole ?? '-'}</td>
                <td>{RELATION_LABEL[inst.relationToScope ?? ''] ?? inst.relationToScope ?? '-'}</td>
                <td>
                  <span className={styles.fpCell}>
                    {directionDot(fp, 'trend')}
                    {readStr(fp, 'trend')}
                  </span>
                </td>
                <td>
                  <span className={styles.fpCell}>
                    {directionDot(fp, 'main_structure')}
                    {readStr(fp, 'main_structure')}
                  </span>
                </td>
                <td>
                  <span className={styles.fpCell}>
                    {directionDot(fp, 'short_structure')}
                    {readStr(fp, 'short_structure')}
                  </span>
                </td>
                <td>
                  <span className={styles.fpCell}>
                    {directionDot(fp, 'momentum')}
                    {readStr(fp, 'momentum')}
                  </span>
                </td>
                <td>
                  <span className={styles.fpCell}>
                    {directionDot(fp, 'volume')}
                    {readStr(fp, 'volume')}
                  </span>
                </td>
                <td>{readStr(events, 'summary') !== '-' ? readStr(events, 'summary') : '无'}</td>
                <td className={styles.numCell}>
                  {inst.contributionValue !== null
                    ? `${inst.contributionValue >= 0 ? '+' : ''}${fmtNum(inst.contributionValue)}`
                    : '-'}
                </td>
                <td>
                  <button
                    type="button"
                    className={styles.btn}
                    disabled={pending || !onToggleWatchlist}
                    onClick={() => onToggleWatchlist?.(inst, !isWatched)}
                    title={isWatched ? '移除自选' : '加入自选'}
                  >
                    {isWatched ? '−' : '+'}
                  </button>
                </td>
                <td>
                  <button
                    type="button"
                    className={styles.btn}
                    disabled={!onAddTracking}
                    onClick={() => onAddTracking?.(inst)}
                  >
                    追踪
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
