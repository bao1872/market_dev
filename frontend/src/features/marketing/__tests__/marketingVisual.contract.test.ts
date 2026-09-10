// Marketing Visual System V1.1 视觉/媒体契约测试
// 目的：把本轮 P0 修复锁死成 runtime contract，防止回退：
//   P0-1 真实图片部署：copy.ts 不得再引用 /landing/assets/*（轻部署不发布 landing）；
//         必须引用 /marketing-assets/media/*；build:marketing-site 必须拷贝真实媒体；
//         panji-marketing-site-deploy 必须 rsync media 并 verify 200。
//   P0-2 筹码图被挤压（chipStoryGrid 布局写反）：Chip 用 chipStoryGrid(1fr+300px)，
//         Structure 用 structureStoryGrid(240px+1fr)；禁止泛化 .storyGrid。
//   P0-3 伪造「QQ 邀请码」QR 已移除（qq_shot.png 为聊天 mockup 无真实 QR）。
//   V1.1 单一视觉系统：marketing.module.scss 无三代叠加、无遗留 layout class。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const copySrc = readSrc('src/features/marketing/data/copy.ts')
const watchSrc = readSrc('src/features/marketing/sections/WatchAndNotify.tsx')
const finalCtaSrc = readSrc('src/features/marketing/sections/FinalCTA.tsx')
const scssSrc = readSrc('src/features/marketing/marketing.module.scss')
const packageJson = readSrc('package.json')
const deploySrc = readSrc('../scripts/ops/panji-marketing-site-deploy')

const buildScript = (JSON.parse(packageJson).scripts as Record<string, string>)[
  'build:marketing-site'
]
assert.ok(buildScript, 'package.json 必须定义 build:marketing-site')

// 实际发布到 /marketing-assets/media/ 的真实媒体（与 build/deploy 步骤一致）
// [V1.2] 替换已退役的 intraday-monitor/market-workspace 旧示意图为真实产品素材 + 回放 JSON
const SHIPPED_MEDIA = [
  'poster_img1.webp',
  'panji-desktop-product.png',
  'panji-mobile-research.jpg',
  'xiaoz-xueqiu.png',
  'zhongji-xuchuang-300308-1d-2y.json',
  'nearshore-protein-688137-chip-consensus-1d-250d.json',
]

test('V1.1-1. copy.ts 不得引用 /landing/assets，必须引用 /marketing-assets/media/', () => {
  assert.ok(
    !copySrc.includes('/landing/assets/'),
    'copy.ts 不得再出现 /landing/assets/ 路径（轻部署不发布 landing）',
  )
  for (const file of SHIPPED_MEDIA) {
    assert.ok(
      copySrc.includes(`/marketing-assets/media/${file}`),
      `copy.ts 必须含 /marketing-assets/media/${file}`,
    )
  }
  // WATCH_NOTIFY.imageSrc 必须消费 MARKETING_MEDIA.feishuPoster（不能散落字面量）
  assert.ok(
    /MARKETING_MEDIA\.feishuPoster/.test(copySrc),
    'WATCH_NOTIFY.imageSrc 必须引用 MARKETING_MEDIA.feishuPoster',
  )
  // WatchAndNotify 组件消费数据源，不硬编码任何图片路径
  assert.ok(
    watchSrc.includes('WATCH_NOTIFY.imageSrc') && !watchSrc.includes('/landing/'),
    'WatchAndNotify 必须消费 WATCH_NOTIFY.imageSrc 且无 /landing/ 硬编码',
  )
  // 伪造 QQ QR 已移除：不再以 qq_shot.png 充当邀请码图片
  assert.ok(
    !copySrc.includes('qq_shot'),
    'copy.ts 不得再以 qq_shot.png 充当「QQ 邀请码」',
  )
})

test('V1.1-2. build:marketing-site 必须把真实媒体 cp 进 dist-marketing-site/media/', () => {
  assert.ok(
    buildScript.includes('dist-marketing-site/media'),
    'build 脚本必须 mkdir/cp dist-marketing-site/media',
  )
  for (const file of SHIPPED_MEDIA) {
    assert.ok(
      buildScript.includes(file),
      `build 脚本必须 cp ${file} 到 media 目录`,
    )
  }
  // 不得把整套 public/landing 拷贝进去（只拷贝实际使用资产）
  assert.ok(
    !buildScript.includes('-r public/landing') && !buildScript.includes('cp -r public/landing'),
    'build 脚本不得整体拷贝 public/landing',
  )
})

test('V1.1-3. deploy 脚本必须 rsync media 并 verify /marketing-assets/media/* = 200', () => {
  assert.ok(
    deploySrc.includes('BUILD_DIR}/media') && deploySrc.includes('SITE_ASSET_TARGET}/media'),
    'deploy 脚本必须 rsync BUILD_DIR}/media -> SITE_ASSET_TARGET}/media',
  )
  for (const file of SHIPPED_MEDIA) {
    assert.ok(
      deploySrc.includes(file),
      `deploy 脚本必须 verify media/${file} = 200`,
    )
  }
})

test('V1.1-4(V1.4). Chip 用 realChipWorkspace(1fr+300px) 真实回放；Structure 用 RealStructureReplay；禁止 .storyGrid', () => {
  assert.ok(
    /RealChipConsensusReplay/.test(
      readSrc('src/features/marketing/sections/ChipConsensusStory.tsx'),
    ),
    'ChipConsensusStory 必须渲染 RealChipConsensusReplay（真实筹码共识回放）',
  )
  assert.ok(
    /\.realChipWorkspace\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s*300px/.test(
      scssSrc.replace(/\s+/g, ' '),
    ),
    'realChipWorkspace 必须是 minmax(0,1fr) 300px（左图宽列右解）',
  )
  assert.ok(
    /styles\.chipStoryGrid/.test(
      readSrc('src/features/marketing/sections/ChipConsensusStory.tsx'),
    ) === false,
    'ChipConsensusStory V1.4 不得再使用教学 demo 的 chipStoryGrid',
  )
  // [V1.2] StructureStory 已不在双栏网格中：改为 full-width 真实回放工作区。
  assert.ok(
    /RealStructureReplay/.test(
      readSrc('src/features/marketing/sections/StructureStory.tsx'),
    ),
    'StructureStory 必须渲染 RealStructureReplay（真实结构回放）',
  )
  assert.ok(
    /styles\.structureStoryGrid/.test(
      readSrc('src/features/marketing/sections/StructureStory.tsx'),
    ) === false,
    'StructureStory V1.2 不得再使用双栏 structureStoryGrid',
  )
  assert.ok(
    !/\.storyGrid\s*\{/.test(scssSrc),
    'SCSS 不得再存在泛化 .storyGrid（布局写反根源）',
  )
})

test('V1.1-5. 六维桌面同行：langRow 为 6 列 grid，非 flex-wrap（修复 4+2）', () => {
  assert.ok(
    /\.langRow\s*\{[^}]*grid-template-columns:\s*repeat\(6,/.test(
      scssSrc.replace(/\s+/g, ' '),
    ),
    'langRow 必须是 repeat(6, minmax(0,1fr)) grid',
  )
})

test('V1.1-6. 单一视觉系统：文件头 V1.1、无遗留三代叠加 layout class、类全部 token', () => {
  assert.ok(
    scssSrc.includes('// PANJI Marketing Site Visual System V1.1'),
    'marketing.module.scss 文件头必须声明 Visual System V1.1',
  )
  for (const legacy of [
    '\\.grid\\s*\\{',
    '\\.grid2\\s*\\{',
    '\\.grid3\\s*\\{',
    '\\.card\\s*\\{',
    '\\.steps\\s*\\{',
    '\\.step\\s*\\{',
    '\\.dims\\s*\\{',
    '\\.dim\\s*\\{',
    '\\.storyStages\\b',
    '\\bM1 骨架\\b',
    'Slice A',
    'Full Alignment V1 additions',
  ]) {
    assert.ok(
      !new RegExp(legacy).test(scssSrc),
      `marketing.module.scss 不得残留遗留布局体系: ${legacy}`,
    )
  }
  // 硬编码 hex 白名单（只允许 token 展开值；不允许 #4f8ef7 等历史值）
  const hexMatches = scssSrc.match(/#[0-9a-fA-F]{6}/g) ?? []
  const allowed = new Set([
    '#0A0F14', '#111A23', '#161F29', '#1A2433',
    '#263440', '#1D2832',
    '#F2F6F8', '#98A1B3', '#657281',
    '#00F6C2', '#39F5CF', '#00B28A',
    '#3882F6', '#F59E0B', '#8B5CF6',
    '#FF4D4F', '#22C55E',
  ])
  for (const hex of hexMatches) {
    assert.ok(
      allowed.has(hex.toUpperCase()) || allowed.has(hex),
      `SCSS 不得出现非 token 硬编码 hex: ${hex}`,
    )
  }
})

test('V1.1-7. FinalCTA 不再渲染伪造二维码图（text-only 入口）', () => {
  assert.ok(
    !/finalInviteImg/.test(finalCtaSrc),
    'FinalCTA 不得再渲染 invite 图片（qq_shot mockup 移除）',
  )
  assert.ok(
    /invite\.href/.test(finalCtaSrc) && /invite\.ctaLabel/.test(finalCtaSrc),
    'FinalCTA 必须以真实链接渲染 invite CTA',
  )
})
