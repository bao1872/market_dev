// [S2-A-C1] 导出按钮 UI policy 唯一 owner（纯函数，无 React 依赖）
//
// UI 可见性 policy：
//   - 导出入口 ONLY admin 可见（普通用户根本不持有 onExport）；
//     后端 require_admin 才是最终安全边界，前端 policy 仅做 UX 收口。
//   - capability 未就绪（accessReady=false）时不暴露按钮，避免
//     「self_selection-only 用户先发 scope=market 再被 403」类竞态的前置可见态。
//   - in-flight（exporting=true）时按钮保留可见但禁用，防双击/多 tab/并发；
//     真正拦截 double-fire 的守卫仍在 handleExport 内的 exportingRef 上。
//
// 该函数必须是确定性的、可被契约测试覆盖的，禁止内联散落在组件 JSX 中。

export interface MarketExportPolicyInput {
  /** 真实 is_admin（来自 auth store，非 role 视图切换） */
  isAdmin: boolean
  /** capability 解析完成（accessStatus === 'ready'） */
  accessReady: boolean
  /** 当前是否正在导出（in-flight 锁） */
  exporting: boolean
}

export interface MarketExportPolicy {
  /** 是否渲染导出按钮（仅 admin 且 capability 已就绪为 true） */
  canShowExportButton: boolean
  /** 是否把真实 onExport 回调接到表格（false 时表格不渲染按钮） */
  onExportWired: boolean
  /** 导出按钮是否可点击（in-flight 时禁用） */
  exportButtonEnabled: boolean
}

export function resolveMarketExportPolicy(
  input: MarketExportPolicyInput,
): MarketExportPolicy {
  const canExport = input.accessReady && input.isAdmin
  return {
    canShowExportButton: canExport,
    onExportWired: canExport,
    exportButtonEnabled: canExport && !input.exporting,
  }
}
