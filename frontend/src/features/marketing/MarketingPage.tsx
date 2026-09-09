// 盘迹营销门户（Full Alignment V1）
// 挂载路径：/（生产由 nginx 服务 site/index.html；轻部署走 panji-marketing-site-deploy）
// 约束：全部 deterministic，不接实时行情；不复制产品侧字段定义；零新增依赖。
import { useState } from 'react'
import MarketingNav from './sections/MarketingNav'
import Hero from './sections/Hero'
import Discovery from './sections/Discovery'
import Workflow from './sections/Workflow'
import StructureStory from './sections/StructureStory'
import ChipConsensusStory from './sections/ChipConsensusStory'
import MarketLanguage from './sections/MarketLanguage'
import StrategyLab from './sections/StrategyLab'
import XiaozToPanji from './sections/XiaozToPanji'
import WatchAndNotify from './sections/WatchAndNotify'
import FinalCTA from './sections/FinalCTA'
import MarketingFooter from './sections/MarketingFooter'
import FirstPyramidDrawerShell from './sections/FirstPyramidDrawerShell'
import styles from './marketing.module.scss'

// 页面顺序严格锁定（合同 S）：
// MarketingNav → Hero → Discovery → Workflow → StructureStory → ChipConsensusStory
// → MarketLanguage → StrategyLab → XiaozToPanji → WatchAndNotify → FinalCTA
// → MarketingFooter → FirstPyramidDrawer
export default function MarketingPage() {
  const [fieldDrawerOpen, setFieldDrawerOpen] = useState(false)

  return (
    <div className={styles.page} data-testid="marketing-page">
      <MarketingNav />
      <main>
        <Hero />
        <Discovery />
        <Workflow />
        <StructureStory />
        <ChipConsensusStory />
        <MarketLanguage onOpenFieldDictionary={() => setFieldDrawerOpen(true)} />
        <StrategyLab />
        <XiaozToPanji />
        <WatchAndNotify />
        <FinalCTA />
      </main>
      <MarketingFooter />
      <FirstPyramidDrawerShell
        open={fieldDrawerOpen}
        onClose={() => setFieldDrawerOpen(false)}
      />
    </div>
  )
}
