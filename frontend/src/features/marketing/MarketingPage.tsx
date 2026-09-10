// 盘迹营销门户（V1.6）
// 挂载路径：/（生产由 nginx 服务 site/index.html；轻部署走 panji-marketing-site-deploy）
// 约束：全部 deterministic，不接实时行情；不复制产品侧字段定义；零新增依赖。
import { useState } from 'react'
import MarketingNav from './sections/MarketingNav'
import Hero from './sections/Hero'
import AudienceProblems from './sections/AudienceProblems'
import Discovery from './sections/Discovery'
import Workflow from './sections/Workflow'
import StructureStory from './sections/StructureStory'
import ChipConsensusStory from './sections/ChipConsensusStory'
import MarketLanguage from './sections/MarketLanguage'
import StrategyLab from './sections/StrategyLab'
import WatchAndNotify from './sections/WatchAndNotify'
import MarketingFooter from './sections/MarketingFooter'
import FirstPyramidDrawerShell from './sections/FirstPyramidDrawerShell'
import styles from './marketing.module.scss'

// 页面顺序严格锁定（合同 S · V1.6）：
// MarketingNav → Hero → AudienceProblems → Discovery → Workflow → StructureStory
// → ChipConsensusStory → MarketLanguage → StrategyLab → WatchAndNotify
// → MarketingFooter（原 FinalCTA 已并入 footer 顶部 CTA 行） → FirstPyramidDrawer
export default function MarketingPage() {
  const [fieldDrawerOpen, setFieldDrawerOpen] = useState(false)

  return (
    <div className={styles.page} data-testid="marketing-page">
      <MarketingNav />
      <main>
        <Hero />
        <AudienceProblems />
        <Discovery />
        <Workflow />
        <StructureStory />
        <ChipConsensusStory />
        <MarketLanguage onOpenFieldDictionary={() => setFieldDrawerOpen(true)} />
        <StrategyLab />
        <WatchAndNotify />
      </main>
      <MarketingFooter />
      <FirstPyramidDrawerShell
        open={fieldDrawerOpen}
        onClose={() => setFieldDrawerOpen(false)}
      />
    </div>
  )
}
