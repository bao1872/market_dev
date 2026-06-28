// [门户] - 描述: 飞书通知演示 + 个股详情窗口
// 演示数据，非实时行情；detail canvas 动画由 useWorkflowAnimation hook 驱动
import { useRef } from 'react'
import { useWorkflowAnimation } from '@/pages/LandingPage/hooks/useWorkflowAnimation'
import { detailCases } from '@/pages/LandingPage/landingData'
import styles from '@/pages/LandingPage/LandingPage.module.scss'

// 飞书通知面板：左侧消息列表 + 右侧个股详情图
// 消息卡片点击切换右侧详情，detail canvas 循环播放 3 只示例股票
export default function FeishuNotificationDemo() {
  // detail 相关 ref（实际挂载到 DOM）
  const detailCanvasRef = useRef<HTMLCanvasElement>(null)
  const detailZoneRef = useRef<HTMLDivElement>(null)
  const detailMarkerRef = useRef<HTMLDivElement>(null)

  // workflow 相关 ref（不挂载到 DOM，仅满足 hook 接口要求）
  const workflowCanvasRef = useRef<HTMLCanvasElement>(null)
  const workflowScanRef = useRef<HTMLDivElement>(null)
  const workflowZoneRef = useRef<HTMLDivElement>(null)
  const workflowAlertRef = useRef<HTMLDivElement>(null)
  const industryPanelRef = useRef<HTMLDivElement>(null)
  const workflowChartRef = useRef<HTMLDivElement>(null)

  // 启动动画 hook（detail canvas 部分生效，workflow canvas 因 ref 未挂载而跳过绘制）
  const { activeDetail, setActiveDetail } = useWorkflowAnimation({
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

  const currentCase = detailCases[activeDetail]

  return (
    <div className={styles.notificationDemo}>
      {/* 飞书消息列表窗口 */}
      <section className={styles.feishuWindow} aria-label="飞书通知列表">
        <div className={styles.feishuAppbar}>
          <div className={styles.feishuLogo}>飞</div>
          <div>
            <strong>盘迹提醒</strong>
            <small>{detailCases.length} 条新消息</small>
          </div>
          <span>•••</span>
        </div>
        <div className={styles.feishuThread}>
          {detailCases.map((c, i) => (
            <button
              key={i}
              className={`${styles.feishuMessage} ${i === activeDetail ? styles.active : ''}`}
              onClick={() => setActiveDetail(i)}
            >
              <div className={styles.feishuAvatar}>盘</div>
              <div className={styles.feishuBubble}>
                <div className={styles.messageMeta}>
                  <strong>盘迹</strong>
                  <time>{c.notifyTime}</time>
                </div>
                <h3>{c.name}｜{c.state}</h3>
                <div className={styles.messageLine}>
                  <b>股价共识度区</b>
                  <span>{c.consensus}</span>
                </div>
                <div className={styles.messageLogic}>
                  <b>产业逻辑</b>
                  <p>{c.messageLogic}</p>
                </div>
                <div className={styles.messageAction}>
                  查看个股详情 <span>→</span>
                </div>
              </div>
            </button>
          ))}
        </div>
      </section>

      {/* 个股详情窗口 */}
      <section className={styles.detailWindow} aria-label="个股详情图">
        <div className={styles.detailHead}>
          <div>
            <small>个股详情</small>
            <h3>{currentCase.name}</h3>
            <span>{currentCase.code}</span>
          </div>
          <div className={styles.detailPrice}>
            <strong>{currentCase.price}</strong>
            <em>{currentCase.change}</em>
          </div>
        </div>
        <div className={styles.detailTabs}>
          <span className={styles.active}>日K</span>
          <span>60分</span>
          <span>15分</span>
          <b>{currentCase.state}</b>
        </div>
        <div className={styles.detailChartShell}>
          <canvas ref={detailCanvasRef}></canvas>
          <div className={styles.detailZone} ref={detailZoneRef}>
            股价共识度区 {currentCase.consensus}
          </div>
          <div className={styles.detailMarker} ref={detailMarkerRef}></div>
        </div>
        <div className={styles.detailSummary}>
          <div>
            <span>本次变化</span>
            <strong>{currentCase.event}</strong>
          </div>
          <div>
            <span>产业逻辑</span>
            <p>{currentCase.logic}</p>
          </div>
        </div>
      </section>
    </div>
  )
}
