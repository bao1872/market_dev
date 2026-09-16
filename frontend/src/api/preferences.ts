// User Preferences 领域 API owner（[S3-E] 由 endpoints.ts 迁出；table-view-presets）。

import { apiClient } from './client'

// ===== Table View Presets 端点 =====
// ============================================================
// [Presets] - 描述: 用户表格视图配置 CRUD（/me/table-view-presets）
// config 仅保存 keyword/sort/filters/hiddenColumns/columnOrder/pageSize，禁止保存 selectedKeys/page/activeRunId/rows

/** 表格视图配置内容（与后端 TableViewPresetConfig 对齐，extra=forbid） */
export interface TableViewPresetConfig {
  keyword?: string | null
  sort?: { key: string; direction: 'asc' | 'desc' } | null
  filters?: Array<{ key: string; op: string; value: string | number; value2?: string | number }> | null
  hiddenColumns?: string[] | null
  columnOrder?: string[] | null
  pageSize?: number | null
  // CHANGE-20260713-006: /market 行业/概念筛选持久化（旧 preset 缺字段时正常兼容）
  industry?: string | null
  concept?: string | null
}

/** preset 响应（与后端 TableViewPresetResponse 对齐） */
export interface TableViewPreset {
  id: string
  user_id: string
  table_id: string
  strategy_key: string | null
  name: string
  config: Record<string, unknown>
  is_default: boolean
  created_at: string
  updated_at: string
}

/** preset 列表响应 */
export interface TableViewPresetListResponse {
  items: TableViewPreset[]
  total: number
}

/** 创建 preset 请求 */
export interface TableViewPresetCreateRequest {
  table_id: string
  strategy_key?: string | null
  name: string
  config: Record<string, unknown>
  is_default?: boolean
}

/** 更新 preset 请求（至少传一个字段） */
export interface TableViewPresetPatchRequest {
  name?: string
  config?: Record<string, unknown>
  is_default?: boolean
}

/** 查询当前用户的 preset 列表（按 table_id + strategy_key 过滤） */
export async function getTableViewPresets(
  tableId: string,
  strategyKey?: string,
): Promise<TableViewPresetListResponse> {
  const params: Record<string, string> = { table_id: tableId }
  if (strategyKey) params.strategy_key = strategyKey
  const { data } = await apiClient.get<TableViewPresetListResponse>('/v1/me/table-view-presets', { params })
  return data
}

/** 创建 preset */
export async function createTableViewPreset(
  payload: TableViewPresetCreateRequest,
): Promise<TableViewPreset> {
  const { data } = await apiClient.post<TableViewPreset>('/v1/me/table-view-presets', payload)
  return data
}

/** 更新 preset（name/config/is_default，user_id/table_id/strategy_key 不可修改） */
export async function updateTableViewPreset(
  id: string,
  payload: TableViewPresetPatchRequest,
): Promise<TableViewPreset> {
  const { data } = await apiClient.patch<TableViewPreset>(`/v1/me/table-view-presets/${id}`, payload)
  return data
}

/** 删除 preset */
export async function deleteTableViewPreset(id: string): Promise<void> {
  await apiClient.delete(`/v1/me/table-view-presets/${id}`)
}

// ============================================================

