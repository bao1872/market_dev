// User Preferences hooks owner（[S3-E] 由 useApi.ts 迁出；table-view-presets）。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as api from '../api/preferences'
import type { TableViewPresetCreateRequest, TableViewPresetPatchRequest } from '../api/preferences'

export function useTableViewPresets(tableId: string | undefined, strategyKey?: string | null) {
  return useQuery({
    queryKey: ['table-view-presets', tableId, strategyKey ?? null],
    queryFn: () => api.getTableViewPresets(tableId!, strategyKey ?? undefined),
    enabled: !!tableId,
    staleTime: 30000,
  })
}

/** 创建 preset（自动失效同维度缓存） */
export function useCreateTableViewPreset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: TableViewPresetCreateRequest) => api.createTableViewPreset(payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: ['table-view-presets', variables.table_id, variables.strategy_key ?? null],
      })
    },
  })
}

/** 更新 preset（自动失效同维度缓存） */
export function useUpdateTableViewPreset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: TableViewPresetPatchRequest }) =>
      api.updateTableViewPreset(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['table-view-presets'] })
    },
  })
}

/** 删除 preset（自动失效同维度缓存） */
export function useDeleteTableViewPreset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteTableViewPreset(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['table-view-presets'] })
    },
  })
}

// ============================================================

