// 通用 API 类型定义

// 统一响应体（与后端 OpenAPI 契约对齐）
export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

// 分页结果
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
}

// 分页查询参数
export interface PageQuery {
  page?: number
  pageSize?: number
}

// 排序参数
export interface SortQuery {
  sort?: {
    key: string
    direction: 'asc' | 'desc'
  }
}

// 错误结构
export interface ApiError {
  code: number
  message: string
  details?: Record<string, unknown>
}

// 页面状态枚举（对应 UI_DEVELOPMENT_SPEC.md 第 2 节页面状态原则）
export type PageState =
  | 'loading'
  | 'ready'
  | 'empty'
  | 'partial'
  | 'stale'
  | 'error'
  | 'permission_denied'
  | 'strategy_unavailable'
