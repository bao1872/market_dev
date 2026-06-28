// [门户] - 描述: 机会工作流演示（发现/验证/追踪 + 产业验证链条）
// 演示数据，非实时行情；workflow canvas 动画由 useWorkflowAnimation hook 驱动
import { useRef, type ReactNode } from 'react'
import { useWorkflowAnimation } from '@/pages/LandingPage/hooks/useWorkflowAnimation'
import {
  workflowSteps,
  workflowCopy,
  industryNodes,
  industryVerdicts,
  industryPanelHead,
} from '@/pages/LandingPage/landingData'
import styles from '@/pages/LandingPage/LandingPage.module.scss'

// 机会工作流面板：3 阶段步骤切换 + 工作流图 + 产业验证链条
// 步骤 1（发现）：扫描动画；步骤 2（验证）：产业验证面板；步骤 3（追踪）：成交密集区提醒
export default function OpportunityWorkflow() {
  // workflow 相关 ref（实际挂载到 DOM）
  const workflowCanvasRef = useRef<HTMLCanvasElement>(null)
  const workflowScanRef = useRef<HTMLDivElement>(null)
  const workflowZoneRef = useRef<HTMLDivElement>(null)
  const workflowAlertRef = useRef<HTMLDivElement>(null)
  const industryPanelRef = useRef<HTMLDivElement>(null)
  const workflowChartRef = useRef<HTMLDivElement>(null)

  // detail 相关 ref（不挂载到 DOM，仅满足 hook 接口要求）
  const detailCanvasRef = useRef<HTMLCanvasElement>(null)
  const detailZoneRef = useRef<HTMLDivElement>(null)
  const detailMarkerRef = useRef<HTMLDivElement>(null)

  // 启动动画 hook（workflow canvas 部分生效，detail canvas 因 ref 未挂载而跳过绘制）
  const { workflowStage, setWorkflowStage } = useWorkflowAnimation({
    workflowCanvas: workflowCanvasRef,
    detailCanvas: detailCanvasRef,
    workflowScan: workflowScanRef,
    workflowZone: workflowZoneRef,
    workflowAlert: workflowAlertRef,
    industryPanel: industryPanelRef,
    workflowChart: workflowChartRef,
    detailZone: detailZoneRef,
    detailMarker: detailMarkerRef,
  })

  const currentCopy = workflowCopy[workflowStage]
  // 产业验证面板仅在步骤 2（机会验证）显示
  const showIndustryPanel = workflowStage === 1

  return (
    <article className={`${styles.panel} ${styles.workflowPanel}`} id="workflow">
      {/* 面板标题 */}
      <div className={styles.workflowHead}>
        <h2>从发现机会，到验证机会，再到持续追踪</h2>
        <p>图形负责发现候选，产业逻辑决定机会的斜率和高度；只有验证通过的目标，才进入长期追踪。</p>
      </div>

      {/* 3 阶段步骤切换 */}
      <div className={styles.workflowSteps}>
        {workflowSteps.map((step, i) => (
          <button
            key={step.index}
            className={`${styles.workflowStep} ${i === workflowStage ? styles.active : ''}`}
            onClick={() => setWorkflowStage(i)}
          >
            <span>{step.index}</span>
            <div>
              <b>{step.title}</b>
              <small>{step.desc}</small>
            </div>
            <em>输出：{step.output}</em>
          </button>
        ))}
      </div>

      {/* 工作流主体：左侧文案 + 右侧图表演示 */}
      <div className={styles.workflowBody}>
        <div className={styles.workflowCopy}>
          <span className={styles.stageBadge}>{currentCopy.label}</span>
          <h3>{currentCopy.title}</h3>
          <p>{currentCopy.desc}</p>
          <ul>
            {currentCopy.bullets.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
          <div className={styles.workflowOutput}>
            <div>
              <small>当前输出</small>
              <strong>{currentCopy.output}</strong>
            </div>
            <span>{currentCopy.next}</span>
          </div>
        </div>

        <div
          className={`${styles.workflowChart} ${showIndustryPanel ? styles.stageIndustry : ''}`}
          ref={workflowChartRef}
        >
          {/* 终端头：当前阶段标题与状态 */}
          <div className={styles.workflowTerminalHead}>
            <span>{currentCopy.terminal}</span>
            <b>{currentCopy.state}</b>
          </div>

          {/* 工作流 Canvas（扫描/密集区/追踪动画） */}
          <canvas ref={workflowCanvasRef}></canvas>

          {/* 扫描动画覆盖层（步骤 1 显示） */}
          <div className={styles.workflowScan} ref={workflowScanRef}>
            <span></span>
            <b>图形筛选发现候选</b>
          </div>

          {/* 产业验证面板（步骤 2 显示） */}
          <div
            className={`${styles.industryValidationPanel} ${showIndustryPanel ? styles.show : ''}`}
            ref={industryPanelRef}
            aria-label="产业逻辑验证链条"
          >
            <div className={styles.industryPanelHead}>
              <div>
                <small>{industryPanelHead.small}</small>
                <h4>{industryPanelHead.h4}</h4>
              </div>
              <span className={styles.validationStatus}>{industryPanelHead.status}</span>
            </div>
            <div className={styles.industryChain}>
              {industryNodes.flatMap((node, i) => {
                // 在节点之间插入箭头（首节点之前不插箭头）
                const items: ReactNode[] = []
                if (i > 0) {
                  items.push(
                    <i
                      key={`arrow-${i}`}
                      className={styles.chainArrow}
                      style={{ ['--delay' as string]: `${node.delay - 60}ms` }}
                    >→</i>
                  )
                }
                items.push(
                  <div
                    key={node.index}
                    className={styles.industryNode}
                    style={{ ['--delay' as string]: `${node.delay}ms` }}
                  >
                    <span>{node.index}</span>
                    <div>
                      <small>{node.small}</small>
                      <strong>{node.strong}</strong>
                      <em>{node.em}</em>
                    </div>
                  </div>
                )
                return items
              })}
            </div>
            <div className={styles.industryVerdicts}>
              {industryVerdicts.map((v, i) => (
                <div
                  key={i}
                  className={v.cls === 'slopeCard' ? `${styles.verdictCard} ${styles.slopeCard}` :
                            v.cls === 'heightCard' ? `${styles.verdictCard} ${styles.heightCard}` :
                            styles.validationResult}
                >
                  <small>{v.small}</small>
                  <strong>{v.strong}</strong>
                  {(v.cls === 'slopeCard' || v.cls === 'heightCard') && (
                    <div className={styles.verdictMeter}>
                      <i></i>
                    </div>
                  )}
                  <span>{v.span}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 成交密集区标识（步骤 3 显示） */}
          <div className={styles.workflowZone} ref={workflowZoneRef}>股价共识度区</div>

          {/* 追踪提醒（步骤 3 显示） */}
          <div className={styles.workflowAlert} ref={workflowAlertRef}>
            <small>持续追踪</small>
            <strong>目标进入股价共识度区</strong>
            <p>验证通过后，盘迹继续等待真正值得你回来的变化。</p>
          </div>
        </div>
      </div>
    </article>
  )
}
