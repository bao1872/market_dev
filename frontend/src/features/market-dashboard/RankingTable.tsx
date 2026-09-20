// [MarketDashboard] - 排行榜表格（Top10 / Bottom10 共用）
// 后端已排好顺序，前端按返回顺序展示，不前端重排；默认主视觉列 = MA5 5日变化。
// 颜色：A 股约定 正=红(up) / 负=绿(down) / 0=null=中性(flat)。
import type { RankingItem } from './types'
import { formatBreadth, formatDelta, deltaDirection } from './dashboardLogic'
import styles from './dashboard.module.scss'

const DIR_CLASS: Record<'up' | 'down' | 'flat', string> = {
  up: styles.up,
  down: styles.down,
  flat: styles.flat,
}

export interface RankingTableProps {
  items: RankingItem[]
  selectedId?: string | null
  onSelect: (boardId: string) => void
  onAddCompare?: (item: RankingItem) => void
  title?: string
}

export default function RankingTable({ items, selectedId, onSelect, onAddCompare, title }: RankingTableProps) {
  return (
    <div className={styles.rankTable}>
      {title && <div className={styles.rankTitle}>{title}</div>}
      <table className={styles.table}>
        <thead>
          <tr>
            <th>板块名称</th>
            <th>MA5 当前</th>
            <th>MA5 5日变化</th>
            <th>MA10 当前</th>
            <th>MA10 5日变化</th>
            {onAddCompare && <th aria-label="操作" />}
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr
              key={it.board_id}
              className={selectedId === it.board_id ? styles.selected : ''}
              onClick={() => onSelect(it.board_id)}
            >
              <td className={styles.boardName}>{it.board_name}</td>
              <td>{formatBreadth(it.current.ma5)}</td>
              <td className={DIR_CLASS[deltaDirection(it.delta.ma5)]}>{formatDelta(it.delta.ma5)}</td>
              <td>{formatBreadth(it.current.ma10)}</td>
              <td className={DIR_CLASS[deltaDirection(it.delta.ma10)]}>{formatDelta(it.delta.ma10)}</td>
              {onAddCompare && (
                <td>
                  <button
                    type="button"
                    className={styles.addBtn}
                    onClick={(e) => {
                      e.stopPropagation()
                      onAddCompare(it)
                    }}
                  >
                    ＋比较
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
