// Marketing V1.5.3 StrategyLab 真实案例舞台布局契约。
// 目的：通过静态扫描锁死 6 条 V1.5.3 修正，防止回退。
// 读源：__dirname 解析定位 frontend/ 根目录；产品 copy 通过相对路径 import（tsx 直接解析）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { STRATEGY_LAB } from '../data/copy'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const scssSrc = readSrc('src/features/marketing/marketing.module.scss')

// ===== A. caseStage 不得有固定 min-height =====

test('V1.5.3-A. caseStage 不得有 min-height:520px，不得新增固定 height', () => {
  // 找到 .caseStage 规则块，检查里面没有 min-height/height
  const stageMatch = scssSrc.match(/^\.caseStage\s*\{([^}]*)\}/m)
  assert.ok(stageMatch, 'SCSS 必须有 .caseStage 规则块')
  const stageBlock = stageMatch[1]
  assert.ok(
    !/min-height\s*:\s*\d+/.test(stageBlock),
    'caseStage 不得声明 min-height（V1.5.3 改为内容驱动）',
  )
  assert.ok(
    !/height\s*:\s*\d+/.test(stageBlock),
    'caseStage 不得声明固定 height',
  )
  assert.match(
    stageBlock,
    /align-items\s*:\s*start/,
    'caseStage 必须 align-items:start，禁止网格子项被强制拉满',
  )
})

// ===== B. caseScreenshot 必须顶对齐 =====

test('V1.5.3-B. caseScreenshot 必须 align-items:flex-start，禁止 center', () => {
  const shotMatch = scssSrc.match(/^\.caseScreenshot\s*\{([^}]*)\}/m)
  assert.ok(shotMatch, 'SCSS 必须有 .caseScreenshot 规则块')
  const shotBlock = shotMatch[1]
  assert.ok(
    /align-items\s*:\s*flex-start/.test(shotBlock) ||
      scssSrc.includes('.caseScreenshot {\n  align-items: flex-start'),
    'caseScreenshot 必须 align-items:flex-start（图片贴顶，避免悬空）',
  )
  assert.ok(
    !/align-items\s*:\s*center\b/.test(shotBlock),
    'caseScreenshot 不得再用 align-items:center（悬空黑边根源）',
  )
})

// ===== C. 图片必须 auto height + contain + top =====

test('V1.5.3-C. caseScreenshot img 必须 height:auto + object-fit:contain + object-position:top', () => {
  const imgMatch = scssSrc.match(/^\.caseScreenshot img\s*\{([^}]*)\}/m)
  assert.ok(imgMatch, 'SCSS 必须有 .caseScreenshot img 规则块')
  const imgBlock = imgMatch[1]
  assert.ok(/height\s*:\s*auto/.test(imgBlock), 'img 必须 height:auto')
  assert.ok(
    /object-fit\s*:\s*contain/.test(imgBlock),
    'img 必须 object-fit:contain（禁止裁切真实截图）',
  )
  assert.ok(
    /object-position\s*:\s*top/.test(imgBlock),
    'img 必须 object-position:top（图片顶对齐图片区顶部）',
  )
  assert.ok(
    !/object-fit\s*:\s*cover/.test(imgBlock),
    '禁止 object-fit:cover（会裁掉 K 线/指标）',
  )
})

// ===== D. caseDisclaimer 不得用 margin:auto =====

test('V1.5.3-D. caseDisclaimer 不得用 margin:auto（禁止人为推到底部制造中间空白）', () => {
  const dmMatch = scssSrc.match(/^\.caseDisclaimer\s*\{([^}]*)\}/m)
  assert.ok(dmMatch, 'SCSS 必须有 .caseDisclaimer 规则块')
  const dmBlock = dmMatch[1]
  assert.ok(
    !/margin\s*:\s*auto/.test(dmBlock),
    'caseDisclaimer 不得用 margin:auto 把免责声明推到底部',
  )
})

// ===== E. 真实案例 playbook 不得包含 stock 名称 =====

test('V1.5.3-E. 三个真实案例 playbook 不得重复 stock 名称', () => {
  const REAL_CASES = ['dow123', 'trend', 'double-bottom']
  const stockByCase = {
    dow123: '国创高新',
    trend: '南亚新材',
    'double-bottom': '精智达',
  }

  for (const caseId of REAL_CASES) {
    const item = STRATEGY_LAB.cases.find((c) => c.id === caseId)
    assert.ok(item && item.kind === 'case', `必须有真实案例 ${caseId}`)
    const stock = stockByCase[caseId as keyof typeof stockByCase]
    assert.ok(
      !item.playbook.includes(stock),
      `${caseId} playbook 不得包含 ${stock}（避免和右上 stock 行重复）`,
    )
    // 也不得包含｜分隔符（旧格式）
    assert.ok(
      !item.playbook.includes('｜'),
      `${caseId} playbook 不得含「｜」（旧 stock|playbook 格式）`,
    )
  }
})

// ===== F. 真实案例文案不得有孤立单行 \n =====
// 允许 \n\n（段落分隔），禁止单个 \n 在句子内部制造"诗歌式断行"。

function hasOrphanNewline(text: string): boolean {
  // 移除所有 \n\n 段落分隔后的剩余文本中是否含单独的 \n
  const stripped = text.replace(/\n\n/g, '')
  return stripped.includes('\n')
}

test('V1.5.3-F. 真实案例文案不得有孤立单行 \\n（诗歌式断行）', () => {
  const REAL_CASES = ['dow123', 'trend', 'double-bottom']

  for (const caseId of REAL_CASES) {
    const item = STRATEGY_LAB.cases.find((c) => c.id === caseId)
    assert.ok(item && item.kind === 'case', `必须有真实案例 ${caseId}`)

    const fields = [
      { label: 'lead', value: item.lead },
      { label: 'what', value: item.what },
      { label: 'panji', value: item.panji },
      { label: 'note', value: item.note },
    ]

    for (const { label, value } of fields) {
      assert.ok(
        !hasOrphanNewline(value),
        `${caseId}.${label} 存在孤立 \\n（句子内部人工换行），应合并为自然句`,
      )
    }
  }
})