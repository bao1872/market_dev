// [门户] - 描述: Hero 区 K线 + 成交分布演示组件
// 演示数据，非实时行情；动画由 useHeroMarketAnimation hook 驱动
import { useRef } from 'react'
import { useHeroMarketAnimation } from '@/pages/LandingPage/hooks/useHeroMarketAnimation'
import { scenarios } from '@/pages/LandingPage/landingData'
import styles from '@/pages/LandingPage/LandingPage.module.scss'

// Hero 区动画演示：K线、成交分布、事件提示、场景切换
export default function HeroMarketDemo() {
  // Canvas 与 DOM 元素 ref（替代原型的 getElementById）
  const klineCanvasRef = useRef<HTMLCanvasElement>(null)
  const profileCanvasRef = useRef<HTMLCanvasElement>(null)
  const clusterLabelRef = useRef<HTMLDivElement>(null)
  const eventToastRef = useRef<HTMLDivElement>(null)
  const triggerPulseRef = useRef<HTMLDivElement>(null)
  const pocLineRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const phaseLabelRef = useRef<HTMLSpanElement>(null)
  const scenarioCountRef = useRef<HTMLSpanElement>(null)
  const profileStatusRef = useRef<HTMLElement>(null)

  // 启动 Hero 动画 hook
  const { currentScenarioIndex, playing, setScenario, togglePlay } = useHeroMarketAnimation({
    klineCanvas: klineCanvasRef,
    profileCanvas: profileCanvasRef,
    clusterLabel: clusterLabelRef,
    eventToast: eventToastRef,
    triggerPulse: triggerPulseRef,
    pocLine: pocLineRef,
    tooltip: tooltipRef,
    phaseLabel: phaseLabelRef,
    scenarioCount: scenarioCountRef,
    profileStatus: profileStatusRef,
  })

  return (
    <div className={styles.heroBoard} aria-label="三种典型价格变化动画">
      {/* 顶部状态栏：实时点 + 当前场景标题 */}
      <div className={styles.boardStatus}>
        <div className={styles.boardStatusLeft}>
          <span className={styles.liveDot}></span>
          <span>{scenarios[currentScenarioIndex]?.title}</span>
        </div>
      </div>

      {/* 主区域：K线图 + 成交分布面板 */}
      <div className={styles.boardMain}>
        <div className={styles.chartShell}>
          <canvas ref={klineCanvasRef}></canvas>
          <div className={styles.clusterLabel} ref={clusterLabelRef}>成交集中区</div>
          <div className={styles.eventToast} ref={eventToastRef}>
            <small>◉ 盘迹监控 · <span>{scenarios[currentScenarioIndex]?.eventTime}</span></small>
            <strong>价格进入成交集中区</strong>
            <a href="#workflow">查看详情 →</a>
          </div>
          <div className={styles.triggerPulse} ref={triggerPulseRef}></div>
        </div>
        <div className={styles.profilePanel}>
          <div className={styles.profileTitle}>成交分布</div>
          <div className={styles.profileSub}>柱子越长，成交越集中</div>
          <div className={styles.profileStats}>
            <span>当前变化</span>
            <b ref={profileStatusRef}>成交正在聚集</b>
          </div>
          <canvas ref={profileCanvasRef}></canvas>
          <div className={styles.pocLine} ref={pocLineRef}>
            <span>成交最集中价 137.48</span>
          </div>
        </div>
      </div>

      {/* 底部控制栏：播放/暂停 + 阶段标签 + 场景切换 dot */}
      <div className={styles.boardControl}>
        <div className={styles.controlLeft}>
          <button
            className={styles.playBtn}
            onClick={togglePlay}
            aria-label={playing ? '暂停' : '播放'}
          >
            {playing ? 'Ⅱ' : '▶'}
          </button>
        </div>
        <div className={styles.controlRight}>
          <span className={styles.phaseLabel} ref={phaseLabelRef}>成交正在聚集</span>
          <div className={styles.dots}>
            {scenarios.map((_, i) => (
              <button
                key={i}
                className={i === currentScenarioIndex ? styles.active : undefined}
                onClick={() => setScenario(i)}
                aria-label={`第${i + 1}种价格变化`}
              ></button>
            ))}
          </div>
          <span ref={scenarioCountRef}>1 / {scenarios.length}</span>
        </div>
      </div>

      {/* Tooltip：fixed 定位，跟随鼠标显示 K线详情 */}
      <div className={styles.tooltip} ref={tooltipRef}></div>
    </div>
  )
}
