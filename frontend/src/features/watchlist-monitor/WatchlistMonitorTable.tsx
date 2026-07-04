// [自选监控] - 桌面端表格封装
// 职责：统一封装 StrategyDataTable，供首页与自选页共用
import { useMemo } from 'react'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { WatchlistMonitorRow } from './types'
import { getWatchlistMonitorColumns, type ColumnOptions } from './columns'

interface WatchlistMonitorTableProps extends ColumnOptions {
  tableId: string
  rows: WatchlistMonitorRow[]
  loading?: boolean
  error?: string | null
  emptyText?: string
  searchable?: boolean
}

export function WatchlistMonitorTable({
  tableId,
  rows,
  loading = false,
  error = null,
  emptyText = '暂无监控数据',
  searchable = true,
  readonly = false,
  onDetail,
  onRemove,
  removePending = false,
}: WatchlistMonitorTableProps) {
  const columns = useMemo(
    () => getWatchlistMonitorColumns({ readonly, onDetail, onRemove, removePending }),
    [readonly, onDetail, onRemove, removePending],
  )

  return (
    <StrategyDataTable
      tableId={tableId}
      columns={columns}
      rows={rows}
      rowKey={(row) => row.instrument_id}
      loading={loading}
      error={error}
      emptyText={emptyText}
      searchable={searchable}
      tableClassName="compact-table"
    />
  )
}
