// [AuctionScopeTable] - 描述: V3.2 Scope 列表（原子列 + 全列 asc/desc + null 永远最后）
// 不重算业务指标；数值统一等宽字体，右对齐；方向值（gap/capital/贡献）用 A股 红涨绿跌。
import type { AuctionScopeRow, AuctionScopeSortField } from './auctionScopeViewModel'
import { formatRatioAsPercent, formatNumber } from './auctionScopeViewModel'
import styles from './auction.module.scss'

interface ColumnDef {
  key: AuctionScopeSortField
  label: string
  group: string
  get: (r: AuctionScopeRow) => number | null
  format: (v: number | null) => string
  /** 方向着色：'gap' 红涨绿跌；'neutral' 中性 */
  tone?: 'gap' | 'neutral'
}

// 列顺序遵循 PRD §47 分组：Identity / Current Price / Historical / Participation / Cross-sectional / Structure
const COLUMNS: readonly ColumnDef[] = [
  {
    key: 'equalWeightGap',
    label: '高开幅度(EW)',
    group: 'Current Price',
    get: (r) => r.equalWeightGap,
    format: (v) => formatRatioAsPercent(v),
    tone: 'gap',
  },
  {
    key: 'amountWeightedGap',
    label: '高开幅度(AW)',
    group: 'Current Price',
    get: (r) => r.amountWeightedGap,
    format: (v) => formatRatioAsPercent(v),
    tone: 'gap',
  },
  {
    key: 'capitalTilt',
    label: '资金倾斜',
    group: 'Current Price',
    get: (r) => r.capitalTilt,
    format: (v) => formatNumber(v),
    tone: 'gap',
  },
  {
    key: 'positiveGapBreadth',
    label: '高开广度',
    group: 'Current Price',
    get: (r) => r.positiveGapBreadth,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'gapDispersion',
    label: '离散度',
    group: 'Current Price',
    get: (r) => r.gapDispersion,
    format: (v) => formatNumber(v),
  },
  {
    key: 'priceNormalizedHhi',
    label: '价格HHI',
    group: 'Current Price',
    get: (r) => r.priceNormalizedHhi,
    format: (v) => formatNumber(v, 3),
  },
  {
    key: 'ewPosition',
    label: 'EW位置',
    group: 'Historical',
    get: (r) => r.ewPosition,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'ewVelocity',
    label: 'EW速度',
    group: 'Historical',
    get: (r) => r.ewVelocity,
    format: (v) => formatNumber(v),
  },
  {
    key: 'ewAcceleration',
    label: 'EW加速度',
    group: 'Historical',
    get: (r) => r.ewAcceleration,
    format: (v) => formatNumber(v),
  },
  {
    key: 'amountHistoricalPosition',
    label: '金额位置',
    group: 'Participation',
    get: (r) => r.amountHistoricalPosition,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'amountMultiple',
    label: '金额倍数',
    group: 'Participation',
    get: (r) => r.amountMultiple,
    format: (v) => formatNumber(v, 2),
  },
  {
    key: 'amountAbnormalBreadth',
    label: '异常广度',
    group: 'Participation',
    get: (r) => r.amountAbnormalBreadth,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'totalAuctionAmount',
    label: '竞价额',
    group: 'Participation',
    get: (r) => r.totalAuctionAmount,
    format: (v) => formatNumber(v, 0),
  },
  {
    key: 'normalizedHhi',
    label: '金额HHI',
    group: 'Participation',
    get: (r) => r.normalizedHhi,
    format: (v) => formatNumber(v, 3),
  },
  {
    key: 'crossRepricing',
    label: '截面-重定价',
    group: 'Cross-sectional',
    get: (r) => r.crossSectional.repricing,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'crossBreadth',
    label: '截面-广度',
    group: 'Cross-sectional',
    get: (r) => r.crossSectional.breadth,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'crossParticipation',
    label: '截面-参与',
    group: 'Cross-sectional',
    get: (r) => r.crossSectional.participation,
    format: (v) => formatNumber(v, 1),
  },
  {
    key: 'leadershipMigration',
    label: '龙头迁移',
    group: 'Leadership',
    get: (r) => r.leadershipMigration,
    format: (v) => formatNumber(v),
  },
  {
    key: 'priceValidCount',
    label: '有效样本',
    group: 'Structure',
    get: (r) => r.priceValidCount,
    format: (v) => formatNumber(v, 0),
  },
]

interface Props {
  rows: AuctionScopeRow[]
  sort: AuctionScopeSortField
  direction: 'asc' | 'desc'
  selectedKey?: string
  onSort: (field: AuctionScopeSortField) => void
  onSelect: (row: AuctionScopeRow) => void
}

function toneClass(value: number | null, tone?: 'gap' | 'neutral'): string {
  if (value === null || tone !== 'gap') return ''
  if (value > 0) return styles.up ?? ''
  if (value < 0) return styles.down ?? ''
  return styles.neutral ?? ''
}

export function AuctionScopeTable({
  rows,
  sort,
  direction,
  selectedKey,
  onSort,
  onSelect,
}: Props) {
  return (
    <div className={styles.scopeTableScroll}>
      <table className={styles.scopeTable}>
        <thead>
          <tr>
            <th className={`${styles.stickyCol} ${styles.stickyHead}`}>板块</th>
            {COLUMNS.map((col) => {
              const active = sort === col.key
              const ariaSort = active
                ? direction === 'asc'
                  ? 'ascending'
                  : 'descending'
                : 'none'
              return (
                <th
                  key={col.key}
                  className={styles.numHead}
                  aria-sort={ariaSort}
                  onClick={() => onSort(col.key)}
                  title={col.label}
                >
                  <span className={styles.colLabel}>{col.label}</span>
                  <span className={styles.sortCaret}>
                    {active ? (direction === 'asc' ? '▲' : '▼') : ''}
                  </span>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = row.scopeKey === selectedKey
            return (
              <tr
                key={row.scopeKey}
                className={selected ? styles.selectedRow : undefined}
                onClick={() => onSelect(row)}
              >
                <td className={`${styles.stickyCol} ${styles.stickyBody}`}>
                  {row.scopeName}
                </td>
                {COLUMNS.map((col) => {
                  const v = col.get(row)
                  return (
                    <td
                      key={col.key}
                      className={`${styles.numCell} ${toneClass(v, col.tone)}`}
                    >
                      {col.format(v)}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export { COLUMNS }
