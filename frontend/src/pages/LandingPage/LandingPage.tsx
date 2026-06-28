// [门户] - 描述: 盘迹门户页主组件（公开路由 / 的内容）
// 组合所有门户子组件；不调用任何受保护 API；不使用 iframe 或整页 dangerouslySetInnerHTML
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import HeroMarketDemo from '@/components/landing/HeroMarketDemo'
import FeishuNotificationDemo from '@/components/landing/FeishuNotificationDemo'
import OpportunityWorkflow from '@/components/landing/OpportunityWorkflow'
import PricingSection from '@/components/landing/PricingSection'
import LegalModal from '@/components/landing/LegalModal'
import LandingFooter from '@/components/landing/LandingFooter'
import { navLinks, features, type LegalType } from './landingData'
import styles from './LandingPage.module.scss'

// 盘迹门户页：公开入口，展示产品能力与工作流
export default function LandingPage() {
  const navigate = useNavigate()
  // 法律条款模态状态
  const [legalOpen, setLegalOpen] = useState(false)
  const [legalType, setLegalType] = useState<LegalType | null>(null)
  // 申请内测提示（VITE_BETA_APPLY_URL 未配置时显示）
  const [betaHint, setBetaHint] = useState('')

  // 登录按钮：跳转 /login（删除原型中的假登录弹窗）
  function handleLogin() {
    navigate('/login')
  }

  // 申请内测按钮：读取环境变量决定跳转或提示
  function handleBetaApply() {
    const betaApplyUrl = import.meta.env.VITE_BETA_APPLY_URL
    if (betaApplyUrl) {
      window.open(betaApplyUrl, '_blank', 'noopener,noreferrer')
    } else {
      setBetaHint('申请通道暂未配置，敬请期待')
    }
  }

  // 打开法律条款模态
  function openLegal(type: LegalType) {
    setLegalType(type)
    setLegalOpen(true)
  }

  function closeLegal() {
    setLegalOpen(false)
  }

  return (
    <div className={styles.landingPage}>
      {/* 顶部导航 */}
      <header className={styles.siteHeader}>
        <div className={`${styles.container} ${styles.nav}`}>
          <a className={styles.brand} href="#home">
            <span className={styles.brandMark}><i></i><b></b></span>
            <span>盘迹</span>
            <small>数据驱动的股票跟踪工具</small>
          </a>
          <nav className={styles.navLinks} aria-label="主导航">
            {navLinks.map((link) => (
              <a
                key={link.href}
                href={link.href}
                className={link.active ? styles.active : undefined}
              >
                {link.label}
              </a>
            ))}
          </nav>
          <div className={styles.navActions}>
            <button className={`${styles.btn} ${styles.btnGhost}`} onClick={handleLogin}>
              登录
            </button>
            <button className={`${styles.btn} ${styles.btnPrimary}`} onClick={handleBetaApply}>
              申请内测
            </button>
          </div>
        </div>
      </header>

      <main>
        {/* Hero 区：文案 + K线动画演示 */}
        <section className={styles.hero} id="home">
          <div className={`${styles.container} ${styles.heroGrid}`}>
            <div className={styles.heroCopy}>
              <h1>同时跟踪多只股票，<br />重要变化及时知道。</h1>
              <p>基于成交密集区的动态监控，<br />当价格进入关键区域时，第一时间通知你。</p>
              <div className={styles.heroActions}>
                <button className={`${styles.btn} ${styles.btnPrimary}`} onClick={handleBetaApply}>
                  申请内测
                </button>
                <button className={`${styles.btn} ${styles.btnGhost}`} onClick={handleLogin}>
                  登录体验
                </button>
              </div>
              {betaHint && <div className={styles.priceNote}>{betaHint}</div>}
            </div>
            <HeroMarketDemo />
          </div>
        </section>

        {/* 特性条：4 项核心能力 */}
        <section className={`${styles.container} ${styles.featureStrip}`} id="capability">
          {features.map((f, i) => (
            <article key={i} className={styles.feature}>
              <div className={`${styles.featureIcon} ${f.iconCls ? (styles as Record<string, string>)[f.iconCls] : ''}`}></div>
              <div>
                <h3>{f.title}</h3>
                <p>{f.desc}</p>
              </div>
            </article>
          ))}
        </section>

        {/* 内容区：飞书通知 + 工作流 + 价格 */}
        <section className={styles.section} id="audience">
          <div className={`${styles.container} ${styles.contentStack}`}>
            {/* 飞书通知演示 */}
            <article className={`${styles.panel} ${styles.notificationPanel}`}>
              <div className={`${styles.panelHeading} ${styles.notificationHeading}`}>
                <div>
                  <h2>变化出现时，信息直接送到你面前</h2>
                  <p>飞书消息先告诉你发生了什么、当前股价共识度区在哪里，以及原有产业逻辑是否仍值得继续跟踪。</p>
                </div>
                <span className={styles.feishuBadge}>飞书通知示例</span>
              </div>
              <FeishuNotificationDemo />
            </article>

            {/* 机会工作流 */}
            <OpportunityWorkflow />

            {/* 价格区 */}
            <PricingSection />
          </div>
        </section>
      </main>

      {/* 页脚 */}
      <LandingFooter onLegalClick={openLegal} />

      {/* 法律条款模态 */}
      <LegalModal open={legalOpen} type={legalType} onClose={closeLegal} />
    </div>
  )
}
