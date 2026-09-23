// [PANJI-REVIEW-UI-RUNTIME-PARITY-FIX] §1/§13-A CSS Module 绑定回归门（静态源码契约，无 DOM）。
//
// 根因：frontend/vite.config.ts 配置 css.modules.localsConvention = 'camelCaseOnly'，
// 因此 SCSS 选择器 .detail-layout / .scope-rail / .level-btn ... 在 TS 里只能通过
// styles.detailLayout / styles.scopeRail / styles.levelBtn 访问；
// 形如 styles['detail-layout'] 的 kebab 查找在运行期解析为 undefined → className 不绑定 → 样式全失效。
//
// 本测试提供两道闸门：
//   A. 全 feature 不得再出现 styles['kebab-case'] / styles["kebab-case"] 查找；
//   B. 每个静态 styles.<camelCase> 引用都必须能在 dashboard.module.scss 找到对应的 .kebab 类，
//      防止「组件引用了 SCSS 里根本不存在的类」这类同样会让样式失效的回归。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const FEATURE_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const SCSS_PATH = join(FEATURE_DIR, 'dashboard.module.scss')

const KEBAB_LOOKUP = /styles\[['"][\w-]+['"]\]/
const STATIC_MODULE_REF = /styles\.([A-Za-z][A-Za-z0-9]*)/g

function collectTsx(dir: string, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    const st = statSync(full)
    if (st.isDirectory()) {
      if (name === '__tests__') continue
      collectTsx(full, acc)
    } else if (name.endsWith('.tsx') && !name.endsWith('.test.tsx')) {
      acc.push(full)
    }
  }
  return acc
}

/** 抓取 SCSS 里所有形如 .foo 的类选择器名（含嵌套 compound，作为 superset 足以避免漏检）。 */
function collectScssClasses(scss: string): Set<string> {
  const set = new Set<string>()
  const re = /\.([a-zA-Z][\w-]*)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(scss)) !== null) set.add(m[1])
  return set
}

function camelToKebab(s: string): string {
  return s.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)
}

const tsxFiles = collectTsx(FEATURE_DIR)
const scssSource = readFileSync(SCSS_PATH, 'utf8')
const scssClasses = collectScssClasses(scssSource)

test('A. CSS Module 绑定契约：全 feature 无 kebab-case 查找（localsConvention=camelCaseOnly）', () => {
  const offenders: string[] = []
  for (const f of tsxFiles) {
    const lines = readFileSync(f, 'utf8').split('\n')
    lines.forEach((line, i) => {
      if (KEBAB_LOOKUP.test(line)) offenders.push(`${f}:${i + 1}: ${line.trim()}`)
    })
  }
  assert.deepEqual(
    offenders,
    [],
    '以下 TSX 仍用 styles[\'kebab-case\'] 访问 CSS Module，在 camelCaseOnly 下运行期解析为 undefined（根因）。应改为 styles.kebabCase。',
  )
})

test('A-已知根因示例必须被上述规则捕获', () => {
  const samples = [
    "styles['detail-layout']",
    'styles["scope-rail"]',
    "styles['level-btn']",
    "styles['search-input']",
    "styles['review-head']",
  ]
  for (const s of samples) {
    assert.ok(KEBAB_LOOKUP.test(s), `回归规则应捕获 ${s}`)
  }
})

test('B. 每个静态 styles.<camelCase> 都必须在 dashboard.module.scss 存在对应类', () => {
  const missing: string[] = []
  for (const f of tsxFiles) {
    const src = readFileSync(f, 'utf8')
    let m: RegExpExecArray | null
    STATIC_MODULE_REF.lastIndex = 0
    while ((m = STATIC_MODULE_REF.exec(src)) !== null) {
      const ident = m[1]
      const kebab = camelToKebab(ident)
      if (!scssClasses.has(kebab)) {
        missing.push(`${f}: styles.${ident} → .${kebab} 在 dashboard.module.scss 不存在`)
      }
    }
  }
  assert.deepEqual(
    missing,
    [],
    '组件引用了 SCSS 中不存在的 CSS Module 类（camelCaseOnly 下会解析为 undefined）。请在 SCSS 补该类，或改用已存在的类。',
  )
})
