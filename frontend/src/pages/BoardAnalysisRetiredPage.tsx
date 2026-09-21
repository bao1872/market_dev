// [BoardAnalysisRetiredPage] - 旧「板块分析」页面已退役。
// [REVIEW-V2-R1] 旧 Board Analysis 后端 API 已删除；板块分析能力并入「复盘」
// （Market Dashboard 的行业/概念 Explorer）。R1 保留 /boards 与 /boards/:boardId
// 路由并如实提示迁移（不依赖已删除的后端）；R3 将正式接到 Industry/Concept Explorer。
import { Link } from 'react-router-dom'

export default function BoardAnalysisRetiredPage() {
  return (
    <div
      data-testid="board-analysis-retired"
      style={{
        minHeight: '60vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 12,
        padding: 24,
        color: '#E0E0E0',
        textAlign: 'center',
      }}
    >
      <h1 style={{ fontSize: 22, margin: 0 }}>板块分析已并入复盘</h1>
      <p style={{ fontSize: 14, opacity: 0.75, margin: 0 }}>
        原「板块分析」页面已退役，其能力由复盘（Market Dashboard）承载。
      </p>
      <div style={{ display: 'flex', gap: 16, fontSize: 14 }}>
        <Link to="/review/industry">前往行业板块复盘</Link>
        <Link to="/review/concept">前往概念板块复盘</Link>
      </div>
    </div>
  )
}
