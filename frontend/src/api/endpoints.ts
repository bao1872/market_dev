// API 端点定义层 - 类型安全的 API 访问函数
//
// 职责：
// 1. 定义所有 API 实体的 TypeScript 接口（与后端 Pydantic schema 对齐，字段使用 snake_case）
// 2. 导出按领域分组的 API 调用函数，每个函数使用 apiClient 或 publicApiClient 发起请求并返回 response.data
//
// 约定：
// - 后端直接返回数据（FastAPI response_model 序列化），不包裹在 ApiResponse 中
// - 字段命名使用 snake_case 以匹配后端 JSON 格式（apiClient 无 camelCase 转换）
// - UUID / datetime / date 字段在 TS 中统一为 string（JSON 序列化后为字符串）
// - user_id 由认证上下文注入，不出现在请求体中（V1.1 安全约束）
// - 通知 API 当前使用 X-User-Id header（占位，后续接入 JWT）
// - 公开端点（login/register/refresh）使用 publicApiClient，避免携带旧 token 或触发 401 refresh

import { apiClient, publicApiClient } from './client'

// ============================================================
// Auth 领域 —— 实现已迁至 ./auth（唯一 owner）
// ============================================================
//
// [S3-A] 认证/自身访问上下文（登录、注册、续期、刷新、改密、me/access/membership）
// 的类型与端点函数的唯一实现 owner 现在是 ./auth。
// 本文件仅作为兼容 barrel 重新导出，保持既有 caller 的 import path 与运行时行为不变。
//
// 依赖方向：client ← auth ← endpoints（本文件）。
// 例外：UserResponse 也被 admin 用户管理端点复用，故在本文件顶部 import（仍以 ./auth 为唯一 owner）。

export {
  login,
  register,
  renew,
  refreshToken,
  changePassword,
  getMe,
  getMyAccess,
  getMyMembership,
} from './auth'

export type {
  CapabilityInfo,
  AccessProfile,
  LoginResponse,
  TokenResponse,
  UserResponse,
  MembershipResponse,
  RegisterSuccessResponse,
  RenewSuccessResponse,
  LoginRequest,
  RegisterRequest,
  RenewRequest,
} from './auth'

// ============================================================
// Market / Watchlist 领域 —— 实现已迁至 ./market 与 ./watchlist（唯一 owner）
// ============================================================
//
// [S3-B] 兼容 barrel：既有 caller 的 import path 与运行时行为不变。
// 依赖方向：client ← market ← watchlist ← endpoints（本文件）。

export {
  getMarketStatus,
  getMarketStocks,
  getMarketBoards,
  getMarketFilterSpecs,
} from './market'

export type {
  MarketSession,
  MarketStatus,
  MarketStockRow,
  MarketStocksResponse,
  MarketStocksQueryParams,
  MarketBoardItem,
  MarketBoardsResponse,
  FpDataType,
  FpInputControl,
  FpValueNormalizer,
  FpFieldSpec,
  FpFieldSpecs,
} from './market'

export {
  getWatchlist,
  addToWatchlist,
  removeFromWatchlist,
  getWatchlistMonitorStatus,
} from './watchlist'

export type {
  WatchlistItem,
  WatchlistListResponse,
  WatchlistSummaryItem,
  WatchlistSummaryResponse,
  WatchlistMonitorStatusItem,
  WatchlistMonitorStatusResponse,
  WatchlistAddRequest,
} from './watchlist'

// ============================================================
// Strategy 领域 —— 实现已迁至 ./strategy（唯一 owner）
// ============================================================
//
// [S3-C] 兼容 barrel。注意：admin strategy 端点（/v1/admin/strategies/*，
// 含 createStrategy / releaseStrategyVersion / archiveStrategyVersion /
// triggerStrategyRun / getAdminStrategyRuns）仍留在本文件，不属 strategy domain。

export {
  getStrategies,
  getStrategy,
  getStrategyVersions,
  getStrategyVersionSchema,
  getStrategyRuns,
  getPublishedRuns,
  getStrategyRunResults,
  getStrategyRunResultDetail,
  getInstrumentMonitorStates,
  getStrategyMonitorStates,
  getInstrumentEvents,
  getStrategyEvents,
  getStrategyEventDetail,
} from './strategy'

export type {
  Strategy,
  StrategyListResponse,
  StrategyVersion,
  StrategyVersionListResponse,
  StrategySchema,
  StrategyRun,
  StrategyRunListResponse,
  StrategyResult,
  StrategyResultListResponse,
  MonitorState,
  MonitorStateListResponse,
  StrategyEvent,
  StrategyEventDetail,
  StrategyEventListResponse,
  StrategyEventQueryParams,
  StrategyResultQueryParams,
} from './strategy'

// ============================================================
// Admin / AfterClose 领域 —— 实现已迁至 ./admin 与 ./adminAfterClose（唯一 owner）
// ============================================================

export {
  createStrategy,
  releaseStrategyVersion,
  archiveStrategyVersion,
  triggerStrategyRun,
  getAdminStrategyRuns,
  getMessageDeliveries,
  retryMessageDelivery,
  createInviteCodes,
  getInviteCodes,
  revokeInviteCode,
  adminListUserChannels,
  adminCreateUserChannel,
  adminUpdateUserChannel,
  adminDeleteUserChannel,
  adminVerifyUserChannel,
  adminTestUserChannel,
  getMembers,
  getMemberRedemptions,
  getAdminUsers,
  getAdminUser,
  adminEnableUser,
  adminDisableUser,
  adminResetUserPassword,
  adminGrantSubscription,
  adminRenewSubscription,
  adminRevokeSubscription,
  adminChangeSubscriptionPlan,
  getUserCapabilities,
  adminGrantCapability,
  adminRevokeCapability,
  getAdminAuditLogs,
  getAdminBetaApplications,
  getAdminBetaApplicationStats,
  getAdminBetaApplicationDetail,
  updateAdminBetaApplication,
  retryAdminBetaApplicationFeishu,
  buildBetaApplicationExportUrl,
  getSchedulerJobRuns,
  getWorkerHeartbeats,
  getAdminSystemOverview,
  getAdminProductReadiness,
  getAdminVisitors,
  triggerComputeBoard,
  triggerComputeAllBoards
} from './admin'

export type {
  UserListResponse,
  AuditLogListItem,
  AuditLogListResponse,
  SubscriptionResponse,
  SubscriptionRenewResponse,
  InviteCode,
  InviteCodeListItem,
  InviteCodeListResponse,
  InviteRedemption,
  MemberListItem,
  MemberListResponse,
  TriggerRunRequest,
  InviteCodeCreateRequest,
  CapabilityGrantInput,
  GrantCapabilityRequest,
  UserCapabilityInfo,
  GrantSubscriptionRequest,
  RenewSubscriptionRequest,
  ChangePlanRequest,
  ChangeAccountStatusRequest,
  UserCapabilitiesResponse,
  BetaApplicationStatus,
  BetaApplicationReasonCode,
  WatchStockRange,
  BetaApplicationListItem,
  BetaApplicationListResponse,
  BetaApplicationDetail,
  BetaApplicationStats,
  BetaApplicationPatchRequest,
  RetryFeishuResponse,
  BetaApplicationQueryParams,
  BarsFreshness,
  StrategyFreshness,
  DataFreshness,
  SystemOverview,
  RecentSchedulerJobSummary,
  SchedulerJobRunItem,
  SchedulerJobRunListResponse,
  WorkerHeartbeatItem,
  WorkerHeartbeatListResponse,
  ProductReadinessItem,
  BenefitsIssue,
  GovernanceReport,
  ProductReadinessResponse,
  VisitorMetricItem,
  VisitorSummary,
  VisitorReport
} from './admin'

export {
  getJobRunEvents,
  getAfterCloseRunStatus,
  createAfterCloseRun,
  forceAfterCloseRun,
  retryAfterCloseRun,
  resumeAfterCloseRun,
  getAfterClosePipelineLatest,
  getAfterClosePipelineByDate,
  getAfterClosePipelineRuns,
  createAfterClosePipelineRun,
  cancelAfterCloseRun,
  reconcileAfterCloseRun,
  restartAfterCloseRun,
  forceRestartAfterCloseRun
} from './adminAfterClose'

export type {
  JobRunEvent,
  JobRunEventListResponse,
  AfterCloseRunStatusResponse,
  AfterCloseRunCreateResponse,
  AfterCloseStepStatus,
  PipelineStep,
  AfterCloseRunSummary,
  AfterCloseDiagnostics,
  FeatureSnapshotRunSummary,
  AfterClosePipelineResponse,
  PipelineRunItem,
  AfterClosePipelineRunListResponse,
  AfterCloseRestartStep,
  AfterClosePipelineRunRequest,
  AfterCloseRunActionResponse,
  AfterClosePipelineRunResponse
} from './adminAfterClose'

// ============================================================
// Notification / StockData / BoardAnalysis / Preferences 领域 —— 实现已迁至对应 owner
// ============================================================

export {
  getMessages,
  markMessageRead,
  getUnreadCount,
  readAllMessages,
  getNotificationChannels,
  createNotificationChannel,
  updateNotificationChannel,
  deleteNotificationChannel,
  verifyNotificationChannel,
  testNotificationChannel,
  testNotificationChannelLatestEvent,
  sendStockDetailFeishu,
  getStockDetailFeishuStatus,
  previewNotification
} from './notification'

export type {
  DeliveryStatus,
  MessageDelivery,
  PrimaryInstrument,
  NotificationMessage,
  NotificationMessageListResponse,
  UnreadCountResponse,
  ReadAllMessagesResponse,
  NotificationChannel,
  NotificationChannelListResponse,
  DeliveryResult,
  ChannelTestResponse,
  ChannelLatestEventTestResponse,
  ShareDeliveryStatus,
  StockDetailFeishuCreateResponse,
  StockDetailFeishuStatusResponse,
  NotificationPreviewResponse,
  CreateChannelRequest,
  UpdateChannelRequest,
  NotificationPreviewRequest
} from './notification'

export {
  getEventsSummary,
  getInstruments,
  batchGetInstruments,
  getInstrumentById,
  getInstrumentBySymbol,
  getStockMemo,
  upsertStockMemo,
  deleteStockMemo,
  toggleMemoNotify,
  getBars,
  getQuote,
  getIndicators,
  getChartSnapshot,
  getCalendar,
  isTradingDay,
  getStructuralFactors,
  getTemporalFeatures,
  getStockContext,
  getFirstPyramid,
  FEISHU_CAPTURE_VIEW
} from './stockData'

export type {
  Instrument,
  InstrumentListResponse,
  CaptureSnapshotResponse,
  IndicatorView,
  Bar,
  BarListResponse,
  DisplayFrame,
  CaptureRenderFrame,
  CalendarDay,
  CalendarListResponse,
  TradingDayResponse,
  InstrumentQueryParams,
  BarQueryParams,
  CalendarQueryParams,
  EventsSummaryResponse,
  InstrumentBatchResponse,
  StockMemo,
  StockMemoUpsertRequest,
  StockMemoNotifyToggleRequest,
  QuoteResponse,
  ChartLayer,
  VisualSegment,
  DsaSelectorData,
  IndicatorQueryParams,
  IndicatorResponse,
  CalculationDiagnostics,
  ChartSnapshotQueryParams,
  ChartSnapshotResponse,
  StructuralFactorQueryParams,
  StructuralFactorResponse,
  TemporalFeaturesQueryParams,
  TemporalFeaturesResponse,
  AtomicFactItem,
  AtomicFactAvailability,
  AtomicFactChange,
  ProductObservationItem,
  ProductObservations,
  StockContextDataQuality,
  AtomicFactsMeta,
  AtomicFactsContextResponse,
  VolumeContextSchema,
  PyramidEvent,
  DimensionResult,
  FirstPyramidSnapshot,
  ChipStatusState,
  ChipStatus
} from './stockData'

export {
  getBoardAnalysisList,
  getBoardAnalysisDetail
} from './boardAnalysis'

export type {
  BoardAnalysisSnapshotDTO,
  BoardAnalysisListResponse,
  BoardAnalysisDetailResponse,
  BoardAnalysisListParams
} from './boardAnalysis'

export {
  getMarketDashboard,
  getMarketRankings,
  getMarketScopeDetail,
  getMarketCompare,
  extractMarketDashboardError
} from './marketDashboard'

export type {
  MarketDashboardResponse,
  MarketDashboardCard,
  MarketDashboardPoint,
  RankingsResponse,
  RankingItem,
  BreadthPair,
  ScopeDetailResponse,
  ScopeMetadata,
  ScopePoint,
  CompareResponse,
  CompareBoard,
  ComparePoint,
  MarketDashboardApiError
} from './marketDashboard'

export {
  getTableViewPresets,
  createTableViewPreset,
  updateTableViewPreset,
  deleteTableViewPreset
} from './preferences'

export type {
  TableViewPresetConfig,
  TableViewPreset,
  TableViewPresetListResponse,
  TableViewPresetCreateRequest,
  TableViewPresetPatchRequest
} from './preferences'

export {
  getAdminStockDebug
} from './admin'

export type {
  AdminAtomicFactDebugItem,
  AdminStockDebugResponse
} from './admin'



/** 用户列表分页响应 */
export type PlanCode = string

/** 套餐定义响应（与 backend/app/schemas/plan.py PlanResponse 对齐） */
export interface PlanResponse {
  plan_code: string
  display_name: string
  monitor_limit: number
  notification_channel_limit: number
  message_retention_days: number
  features: string[]
}

/** 邀请码响应（含明文，仅生成时返回）+ 套餐快照 */
export interface VersionInfo {
  git_sha: string
  build_time: string
  app_version: string
  alembic_revision: string
}

/** 获取后端版本信息（无需认证） */
export async function getVersion(): Promise<VersionInfo> {
  const res = await apiClient.get<VersionInfo>('/v1/version')
  return res.data
}

// ============================================================
// Health 领域类型
// ============================================================

/** 健康检查响应 */
export interface HealthResponse {
  status: 'ok' | string
  service: string
  version: string
}

/** 获取后端健康状态（无需认证） */
export async function getHealth(): Promise<HealthResponse> {
  const res = await apiClient.get<HealthResponse>('/v1/health')
  return res.data
}

// [S3-B] MarketSession / MarketStatus 已迁至 ./market（见顶部兼容 re-export）。

// ============================================================
// 请求体类型
// ============================================================

// [S3-A] 登录/注册/续期请求类型已迁至 ./auth（见顶部兼容 re-export）。

/** 触发策略运行请求 */
export interface PaginationParams {
  limit?: number
  offset?: number
}

// ============================================================
// ===== Auth 端点 =====
// ============================================================
//
// [S3-A] login / register / renew / refreshToken / changePassword / getMe /
// getMyAccess / getMyMembership 的实现已迁至 ./auth（唯一 owner），
// 并在本文件顶部以兼容 barrel 重新导出。

// ============================================================
// ===== Events Summary 领域类型
// ============================================================

/** 策略事件汇总响应 */
export async function getPlans(): Promise<PlanResponse[]> {
  const { data } = await publicApiClient.get<PlanResponse[]>('/v1/plans')
  return data
}

// ============================================================
// ===== Stock Context 端点（PRD V1.1 §7.3）=====
// ============================================================

/** AtomicFactItem - 普通用户侧单个原子事实（绝不含 factId / sourcePath 等内部字段） */

