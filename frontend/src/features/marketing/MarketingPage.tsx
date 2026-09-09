// 盘迹营销门户（M1 骨架 + M1.1 边界修正）
// 挂载路径：/marketing-preview（Route D 影子开发，生产 / 仍由 Nginx 服务静态门户）
// 约束：全部 deterministic，不接实时行情；不复制产品侧字段定义；零新增依赖。
// M2 加入 StructureStory / ChipConsensusStory，M3 加入 StrategyLab / XueqiuToPanji。
import { useState } from 'react'
import MarketingNav from './sections/MarketingNav'
import Hero from './sections/Hero'
import Discovery from './sections/Discovery'
import Workflow from './sections/Workflow'
import StructureStory from './sections/StructureStory'
import ChipConsensusStory from './sections/ChipConsensusStory'
import MarketLanguage from './sections/MarketLanguage'
import FirstPyramidDrawerShell from './sections/FirstPyramidDrawerShell'
import MarketingFooter from './sections/MarketingFooter'
import styles from './marketing.module.scss'

export default function MarketingPage() {
  // 第一金字塔字段字典：渐进披露，默认关闭，不占主体 layout
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
      </main>
      <MarketingFooter />
      <FirstPyramidDrawerShell
        open={fieldDrawerOpen}
        onClose={() => setFieldDrawerOpen(false)}
      />
    </div>
  )
}
