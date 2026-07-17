// [WencaiBoardSyncContract] - 描述: 问财板块同步前端契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/wencaiBoardSyncContract.test.ts
//
// CHANGE-20260716-007 更新：
//  1. industry 改为关键词匹配：不再校验 industryNameSet.has(trimmed)
//  2. concept 仍保持精确匹配：conceptNameSet.has(trimmed)
//  3. 行业 placeholder 改为"搜索行业关键词"
//  4. BoardFilterCombobox 替换原生 datalist，提供键盘导航 / 高亮 / a11y
//  5. 保留：source/stale/last_attempt_status 字段、stale 时"沿用上次板块数据"提示、disabled 行为

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

const ENDPOINTS_PATH = join(__dirname, '..', '..', '..', 'api', 'endpoints.ts')
const TOOLBAR_PATH = join(__dirname, '..', 'MarketToolbar.tsx')
const PAGE_PATH = join(__dirname, '..', 'MarketWorkspacePage.tsx')
const COMBOBOX_PATH = join(__dirname, '..', 'BoardFilterCombobox.tsx')
const SCSS_PATH = join(__dirname, '..', 'MarketWorkspace.module.scss')

const endpointsSrc = readFileSync(ENDPOINTS_PATH, 'utf-8')
const toolbarSrc = readFileSync(TOOLBAR_PATH, 'utf-8')
const pageSrc = readFileSync(PAGE_PATH, 'utf-8')
const comboboxSrc = readFileSync(COMBOBOX_PATH, 'utf-8')
const scssSrc = readFileSync(SCSS_PATH, 'utf-8')

test('MarketBoardsResponse 包含 source/stale/last_attempt_status 字段', () => {
  assert.ok(endpointsSrc.includes('source: string | null'), 'MarketBoardsResponse 缺少 source 字段')
  assert.ok(endpointsSrc.includes('stale: boolean'), 'MarketBoardsResponse 缺少 stale 字段')
  assert.ok(
    endpointsSrc.includes('last_attempt_status: string | null'),
    'MarketBoardsResponse 缺少 last_attempt_status 字段',
  )
})

test('MarketToolbar 使用 BoardFilterCombobox 替换原生 datalist', () => {
  // 应导入并使用 BoardFilterCombobox
  assert.ok(
    toolbarSrc.includes("from './BoardFilterCombobox'") &&
      toolbarSrc.includes('<BoardFilterCombobox'),
    'MarketToolbar 未使用 BoardFilterCombobox',
  )
  // 不应再使用原生 datalist
  assert.ok(
    !toolbarSrc.includes('<datalist'),
    'MarketToolbar 不应再使用原生 datalist',
  )
  assert.ok(
    !toolbarSrc.includes('list="industry-options"'),
    'MarketToolbar 不应再使用 list 属性绑定 datalist',
  )
})

test('MarketToolbar 行业 placeholder 为"搜索行业关键词"', () => {
  assert.ok(
    toolbarSrc.includes('搜索行业关键词'),
    '行业 placeholder 未改为"搜索行业关键词"',
  )
})

test('MarketToolbar 行业不再校验 industryNameSet（关键词匹配）', () => {
  // 不应再出现 industryNameSet.has 校验
  assert.ok(
    !toolbarSrc.includes('industryNameSet.has'),
    '行业不应再使用 industryNameSet.has 校验精确值（关键词匹配）',
  )
  // 也不应再出现 industryNameSet 定义
  assert.ok(
    !toolbarSrc.includes('industryNameSet'),
    '行业不应再定义 industryNameSet',
  )
})

test('MarketToolbar 概念仍保持精确校验（在 Combobox 内）', () => {
  // Combobox 中 concept 模式应使用 conceptNameSet 校验
  assert.ok(
    comboboxSrc.includes('conceptNameSet') && comboboxSrc.includes('conceptNameSet.has'),
    '概念模式应保留 conceptNameSet.has 精确校验',
  )
})

test('MarketToolbar stale 时显示沿用提示', () => {
  assert.ok(
    toolbarSrc.includes('boardsStale'),
    '未读取 stale 状态',
  )
  assert.ok(
    toolbarSrc.includes('沿用上次板块数据'),
    'stale 时未显示"沿用上次板块数据"提示',
  )
})

test('MarketToolbar disabled 当 boards 不可用时', () => {
  assert.ok(
    toolbarSrc.includes('disabled={!boardsAvailable}'),
    'boards 不可用时未禁用输入',
  )
})

test('MarketWorkspacePage 传递 stale 到 toolbar', () => {
  assert.ok(
    pageSrc.includes('stale: boardsQuery.data.stale'),
    'MarketWorkspacePage 未传递 stale 到 toolbar',
  )
})

// ===== BoardFilterCombobox 契约（CHANGE-20260716-007）=====

test('BoardFilterCombobox 支持 industry / concept 两种模式', () => {
  assert.ok(
    comboboxSrc.includes("mode === 'industry'") && comboboxSrc.includes("mode === 'concept'"),
    'BoardFilterCombobox 未区分 industry / concept 模式',
  )
})

test('BoardFilterCombobox 行业模式接受任意关键词提交', () => {
  // industry 模式：commit 不应做精确校验
  assert.ok(
    comboboxSrc.includes('任意关键词都接受') ||
      comboboxSrc.includes('industry 模式：任意关键词都接受'),
    '行业模式未明确允许任意关键词提交',
  )
})

test('BoardFilterCombobox 支持键盘导航 ArrowUp/ArrowDown/Enter/Escape', () => {
  assert.ok(comboboxSrc.includes("e.key === 'ArrowDown'"), '未处理 ArrowDown')
  assert.ok(comboboxSrc.includes("e.key === 'ArrowUp'"), '未处理 ArrowUp')
  assert.ok(comboboxSrc.includes("e.key === 'Enter'"), '未处理 Enter')
  assert.ok(comboboxSrc.includes("e.key === 'Escape'"), '未处理 Escape')
})

test('BoardFilterCombobox 最多展示 12 条建议', () => {
  assert.ok(
    comboboxSrc.includes('MAX_SUGGESTIONS = 12'),
    '最多展示建议数应为 12',
  )
})

test('BoardFilterCombobox 行业展示将 - 渲染为 /', () => {
  assert.ok(
    comboboxSrc.includes('displayIndustryName') && comboboxSrc.includes("replace(/-/g, ' / ')"),
    '行业名称未将 - 渲染为 /',
  )
})

test('BoardFilterCombobox 支持清除按钮', () => {
  assert.ok(
    comboboxSrc.includes('comboboxClear') && comboboxSrc.includes('handleClear'),
    '未实现清除按钮',
  )
})

test('BoardFilterCombobox 解决 blur 先于 click 的问题', () => {
  // 应使用 mousedown preventDefault 或 blur 延迟
  assert.ok(
    comboboxSrc.includes('BLUR_COMMIT_DELAY_MS') || comboboxSrc.includes('onMouseDown'),
    '未解决 blur 先于 click 的问题',
  )
})

test('BoardFilterCombobox 提供 aria-combobox/listbox/option', () => {
  assert.ok(comboboxSrc.includes('role="combobox"'), '缺少 role="combobox"')
  assert.ok(comboboxSrc.includes('role="listbox"'), '缺少 role="listbox"')
  assert.ok(comboboxSrc.includes('role="option"'), '缺少 role="option"')
  assert.ok(comboboxSrc.includes('aria-expanded'), '缺少 aria-expanded')
  assert.ok(comboboxSrc.includes('aria-activedescendant'), '缺少 aria-activedescendant')
})

test('BoardFilterCombobox 点击外部关闭面板', () => {
  assert.ok(
    comboboxSrc.includes('handleClickOutside') || comboboxSrc.includes('mousedown'),
    '未实现点击外部关闭',
  )
})

test('BoardFilterCombobox 高亮命中关键词', () => {
  assert.ok(
    comboboxSrc.includes('highlightSegments') && comboboxSrc.includes('comboboxHighlight'),
    '未实现命中关键词高亮',
  )
})

test('BoardFilterCombobox 概念模式不逐字符请求后端', () => {
  // onChange 中不应直接调用 onChange（仅更新本地 state），清空除外
  assert.ok(
    !comboboxSrc.match(/onChange=\{[^}]*onChange\(/),
    'Combobox 不应逐字符调用 onChange 提交',
  )
})

test('BoardFilterCombobox 清空立即提交空值', () => {
  assert.ok(
    comboboxSrc.includes("v === ''") && comboboxSrc.includes("onChange('')"),
    '清空未立即提交空值',
  )
})

// ===== PR #77 收口：P0 修复契约（activeIndex=-1 / NFKC / useId / 排序 / 无结果 / 键盘可达）=====

test('PR#77: openPanel 设置 activeIndex=-1（不默认激活首条建议）', () => {
  // P0 修复：openPanel 必须 setActiveIndex(-1)，不能是 0
  // 旧行为 activeIndex = suggestions.length > 0 ? 0 : -1 会导致 Enter 误选首条完整路径
  assert.ok(
    comboboxSrc.includes('setActiveIndex(-1)'),
    'openPanel/closePanel 必须设置 activeIndex=-1',
  )
  // openPanel 函数体内不应出现 activeIndex = 0 模式
  assert.ok(
    !comboboxSrc.match(/openPanel[\s\S]{0,200}setActiveIndex\(0\)/),
    'openPanel 不应设置 activeIndex=0（会导致 Enter 误选首条建议）',
  )
})

test('PR#77: handleInputChange 在输入变化时重置 activeIndex=-1', () => {
  // 输入变化时必须重置高亮，防止残留的 activeIndex 导致 Enter 误选
  assert.ok(
    comboboxSrc.match(/handleInputChange[\s\S]{0,400}setActiveIndex\(-1\)/),
    'handleInputChange 必须在输入变化时 setActiveIndex(-1)',
  )
})

test('PR#77: Enter 未激活建议时 industry 提交关键词（不提交首条路径）', () => {
  // Enter 分支：activeIndex < 0 时应调用 commit(inputValue)，而非 selectSuggestion(suggestions[0])
  assert.ok(
    comboboxSrc.includes('activeIndex >= 0') && comboboxSrc.includes('commit(inputValue)'),
    'Enter 未激活建议时应提交当前输入关键词（industry 模式）',
  )
})

test('PR#77: normalizeInput 使用 NFKC 规范化（与后端一致）', () => {
  // 前端规范化必须包含 .normalize('NFKC')
  assert.ok(
    comboboxSrc.includes(".normalize('NFKC')"),
    'normalizeInput 必须使用 NFKC 规范化（与后端 _normalize_keyword 一致）',
  )
})

test('PR#77: 使用 React useId 生成 listbox/option id（避免多实例冲突）', () => {
  assert.ok(
    comboboxSrc.includes('useId'),
    '必须使用 React useId 生成唯一 id 前缀',
  )
  assert.ok(
    comboboxSrc.includes('listboxId'),
    '必须生成 listboxId 并用于 aria-controls 和 ul id',
  )
})

test('PR#77: 建议排序使用 suggestionRank（精确→前缀→包含）', () => {
  assert.ok(
    comboboxSrc.includes('suggestionRank'),
    '必须实现 suggestionRank 函数进行建议排序',
  )
  // rank 值：精确=0, 前缀=1, 包含=2
  assert.ok(
    comboboxSrc.includes('return 0') && comboboxSrc.includes('return 1') && comboboxSrc.includes('return 2'),
    'suggestionRank 应区分精确(0)/前缀(1)/包含(2) 三档',
  )
})

test('PR#77: 建议排序使用 localeCompare 稳定排序（zh-Hans-CN）', () => {
  assert.ok(
    comboboxSrc.includes("localeCompare") && comboboxSrc.includes("zh-Hans-CN"),
    '同 rank 内应使用 localeCompare(name, "zh-Hans-CN") 稳定排序',
  )
})

test('PR#77: 最多展示 12 条建议（MAX_SUGGESTIONS）', () => {
  assert.ok(
    comboboxSrc.includes('MAX_SUGGESTIONS = 12'),
    '最多展示建议数应为 12',
  )
})

test('PR#77: 清除按钮键盘可达（无 tabIndex={-1}）', () => {
  // 清除按钮不应有 tabIndex={-1}（会被 Tab 跳过）
  // 检查清除按钮上下文不含 tabIndex={-1}
  // 按钮使用 </button> 闭合，匹配从 comboboxClear 到 </button>
  const clearBtnMatch = comboboxSrc.match(/comboboxClear[\s\S]{0,400}?<\/button>/)
  assert.ok(clearBtnMatch, '未找到清除按钮')
  assert.ok(
    !clearBtnMatch![0].includes('tabIndex={-1}'),
    '清除按钮不应设置 tabIndex={-1}（必须键盘可达）',
  )
})

test('PR#77: 无匹配时显示盘迹风格"无匹配行业"/"未找到该概念"', () => {
  // industry 模式无匹配应显示"无匹配行业"，concept 模式显示"未找到该概念"
  assert.ok(
    comboboxSrc.includes('无匹配行业'),
    'industry 模式无匹配时应显示"无匹配行业"',
  )
  assert.ok(
    comboboxSrc.includes('未找到该概念'),
    'concept 模式无匹配时应显示"未找到该概念"',
  )
  // hasInputNoMatch 逻辑必须存在
  assert.ok(
    comboboxSrc.includes('hasInputNoMatch'),
    '必须实现 hasInputNoMatch 判断逻辑',
  )
})

test('PR#77: concept 无效输入保留原已提交值（不提交无效文本）', () => {
  // commit 函数中 concept 模式必须校验 conceptNameSet.has(normalized)
  assert.ok(
    comboboxSrc.match(/concept[\s\S]{0,100}conceptNameSet\.has\(normalized\)/),
    'concept 模式 commit 必须校验 conceptNameSet.has(normalized)，无效时保留原值',
  )
})

test('PR#77: conceptNameSet 使用 NFKC 规范化名称', () => {
  // conceptNameSet 构造时必须 NFKC 规范化，否则全角输入无法匹配
  assert.ok(
    comboboxSrc.match(/conceptNameSet[\s\S]{0,200}\.normalize\('NFKC'\)/),
    'conceptNameSet 必须使用 NFKC 规范化概念名称',
  )
})

test('PR#77: 行业长路径建议项有 title 属性（hover 显示完整文本）', () => {
  assert.ok(
    comboboxSrc.includes('title={mode === \'industry\' ? displayIndustryName(name) : name}'),
    '行业建议项必须有 title 属性显示完整路径',
  )
})

test('PR#77: ArrowDown/ArrowUp 打开面板时仅激活不提交', () => {
  // ArrowDown/ArrowUp 应 preventDefault + setActiveIndex，不应直接 commit
  assert.ok(
    comboboxSrc.match(/ArrowDown[\s\S]{0,200}setActiveIndex\(\(idx\)/),
    'ArrowDown 应更新 activeIndex 而非直接提交',
  )
})

// ===== PR #77 收口：长路径面板宽度 SCSS 契约 =====

test('PR#77: 行业面板宽度 360~480px（长路径不全部省略）', () => {
  // .comboboxIndustry .comboboxPanel 必须 min-width: 360px; max-width: 480px
  const industryPanelMatch = scssSrc.match(
    /\.comboboxIndustry\s+\.comboboxPanel\s*\{[^}]*\}/,
  )
  assert.ok(industryPanelMatch, '未找到 .comboboxIndustry .comboboxPanel 样式')
  const rule = industryPanelMatch![0]
  assert.ok(
    rule.includes('min-width: 360px'),
    '行业面板 min-width 必须 360px（长路径不全部省略）',
  )
  assert.ok(
    rule.includes('max-width: 480px'),
    '行业面板 max-width 必须 480px（受视口限制）',
  )
})

test('PR#77: 行业输入框宽度 220/240px（不含面板宽度）', () => {
  // .comboboxIndustry 宽度 220px，1920+ 240px
  const industryMatch = scssSrc.match(/\.comboboxIndustry\s*\{[^}]*\}/)
  assert.ok(industryMatch, '未找到 .comboboxIndustry 样式')
  assert.ok(
    industryMatch![0].includes('width: 220px'),
    '行业输入框宽度必须 220px',
  )
})

test('PR#77: 概念面板与输入框同宽（max 240px）', () => {
  const conceptPanelMatch = scssSrc.match(
    /\.comboboxConcept\s+\.comboboxPanel\s*\{[^}]*\}/,
  )
  assert.ok(conceptPanelMatch, '未找到 .comboboxConcept .comboboxPanel 样式')
  assert.ok(
    conceptPanelMatch![0].includes('max-width: 240px'),
    '概念面板 max-width 必须 240px',
  )
})
