// [ReplayPage] - 描述: 复盘功能占位页（PRD V1.0 阶段一）
// 复盘入口进入明确的"功能规划中"占位，不伪造业务逻辑。
// 功能上线后替换本占位为真实复盘工作区。
export default function ReplayPage() {
  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#030915',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#e6edf3',
        gap: '12px',
      }}
    >
      <h1 style={{ fontSize: '24px', margin: 0 }}>复盘功能规划中</h1>
      <p style={{ margin: 0, color: '#8b949e' }}>该功能尚未上线，敬请期待</p>
    </div>
  )
}
