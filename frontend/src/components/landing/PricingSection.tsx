// [门户] - 描述: 价格区组件（月付/年付切换 + 申请内测按钮）
// 不接入真实支付；"申请内测"按钮通过 onApply 回调触发父组件打开 BetaApplicationModal
import { useState } from 'react'
import { pricingPlans } from '@/pages/LandingPage/landingData'
import styles from '@/pages/LandingPage/LandingPage.module.scss'

export interface PricingSectionProps {
  // [内测申请] - 描述: 申请内测回调，由父组件管理 Modal 状态
  onApply: () => void
}

// 价格面板：两档套餐（观察版/研究版）+ 月付/年付切换
// "申请内测"按钮：通过 onApply 回调触发父组件打开站内问卷
export default function PricingSection({ onApply }: PricingSectionProps) {
  // 计费模式：monthly | yearly
  const [billing, setBilling] = useState<'monthly' | 'yearly'>('yearly')

  return (
    <article className={styles.pricePanelWide}>
      {/* 标题区 + 计费切换 */}
      <div className={styles.pricePanelHead}>
        <div className={styles.priceIntro}>
          <h2>内测专享价格</h2>
          <p>两档功能相同，只区别同时跟踪的股票数量。价格公开，审核通过后开放购买。</p>
        </div>
        <div className={styles.billingToggle}>
          <button
            className={billing === 'monthly' ? styles.active : undefined}
            onClick={() => setBilling('monthly')}
          >
            月付
          </button>
          <button
            className={billing === 'yearly' ? styles.active : undefined}
            onClick={() => setBilling('yearly')}
          >
            年付　8折
          </button>
        </div>
      </div>

      <div className={styles.priceSide}>
        <div className={styles.pricePanel}>
          <div className={styles.priceGrid}>
            {pricingPlans.map((plan) => {
              // 年付模式按月显示价格（yearly/12），月付模式显示 monthly
              const price = billing === 'monthly' ? plan.monthly : Math.round(plan.yearly / 12)
              return (
                <div key={plan.key} className={styles.priceCard}>
                  <h3>{plan.title}</h3>
                  <strong>
                    {price}
                    <small> /月</small>
                  </strong>
                  <p>{plan.sub}</p>
                </div>
              )
            })}
          </div>
          <div className={styles.priceCta}>
            <button
              className={`${styles.btn} ${styles.btnPrimary} ${styles.btnWide}`}
              onClick={onApply}
            >
              申请内测 →
            </button>
          </div>
          <div className={styles.priceNote}>
            {'内测期间可随时取消，费用透明无隐藏消费'}
          </div>
        </div>
      </div>
    </article>
  )
}
