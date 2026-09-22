// [PANJI-REVIEW-UI-UNIFY] 表头筛选图标（SVG funnel）。
//
// 从 StrategyDataTable 的 `⌁` 字符升级而来：字符图标在不同字体下渲染不一致、
// 语义也不明确。此处只提取一个纯视觉 primitive（无 state、无逻辑），
// 供 StrategyDataTable 与 ScopeExplorerTable 两个表头共用，避免重复 SVG markup。
// 按 B16 边界：不建立 DataGrid framework / 不引入通用表格引擎。

interface Props {
  /** 图标边长（px） */
  size?: number
  className?: string
}

export default function TableFilterIcon({ size = 13, className }: Props) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2.2 2.9h11.6L9.5 8v4.1l-3 1.7V8L2.2 2.9Z" />
    </svg>
  )
}
