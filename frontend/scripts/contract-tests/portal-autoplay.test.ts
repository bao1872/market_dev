// [门户] - 描述: 指标原理页动画自动播放状态机测试（CHANGE-20260724-002）
// 用法：node --experimental-strip-types --test scripts/contract-tests/portal-autoplay.test.ts
//
// 目的：
//   在最小化 DOM 环境中执行 data-principles.js，验证 setupPlayer() 自动播放状态机：
//   1. 默认播放按钮为暂停状态（wantsPlay=true）
//   2. 时间推进后步骤自动变化
//   3. 手动切换后继续播放并重置计时
//   4. 主动暂停后 IntersectionObserver 不会擅自恢复
//   5. 页面隐藏/恢复符合播放意图
//   6. prefers-reduced-motion 下不自动轮播
//
//   禁止只检查源码中是否有 playing=true——必须执行 IIFE 后检查真实 DOM 行为。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PORTAL_DIR = join(__dirname, '..', '..', 'public', 'portal')
const JS_PATH = join(PORTAL_DIR, 'assets', 'js', 'data-principles.js')

interface SandboxOptions {
  reducedMotion?: boolean
  hasIntersectionObserver?: boolean
  initialHidden?: boolean
}

interface Sandbox {
  document: any
  window: any
  timers: Array<{ id: number; fn: () => void; ms: number }>
  activeTimerIds: Set<number>
  ioCallbacks: Array<(entries: any[]) => void>
  visibilityHandlers: Array<() => void>
  playAriaLabels: { c: string[]; s: string[] }
  stepActiveIndices: { c: number[]; s: number[] }
  restore: () => void
  advanceTimers: (ms: number) => void
  triggerIntersection: (prefix: 'c' | 's', ratio: number) => void
  triggerVisibility: (hidden: boolean) => void
  clickButton: (id: string) => void
  getActiveStepIndex: (prefix: 'c' | 's') => number
}

/**
 * 创建沙箱环境：mock document/window/setInterval/IntersectionObserver/matchMedia。
 * 收集 play 按钮 aria-label 变化、step 按钮 active 状态变化，用于断言。
 */
function createSandbox(opts: SandboxOptions = {}): Sandbox {
  const reducedMotion = opts.reducedMotion ?? false
  const hasIO = opts.hasIntersectionObserver ?? true
  const initialHidden = opts.initialHidden ?? false

  const timers: Array<{ id: number; fn: () => void; ms: number }> = []
  const activeTimerIds = new Set<number>()
  const ioCallbacks: Array<(entries: any[]) => void> = []
  const visibilityHandlers: Array<() => void> = []
  const playAriaLabels: { c: string[]; s: string[] } = { c: [], s: [] }
  const stepActiveIndices: { c: number[]; s: number[] } = { c: [], s: [] }

  let timerCounter = 0
  const g = globalThis as any

  function makeElement(id?: string): any {
    const children: any[] = []
    const attrs: Record<string, any> = {}
    const classes = new Set<string>()
    const classList = {
      toggle(cls: string, force?: boolean) {
        const has = classes.has(cls)
        const target = force === undefined ? !has : force
        if (target) classes.add(cls); else classes.delete(cls)
      },
      add(c: string) { classes.add(c) },
      remove(c: string) { classes.delete(c) },
      contains(c: string) { return classes.has(c) },
    }
    const el: any = {
      tagName: 'div',
      children,
      style: {},
      classList,
      dataset: {},
      innerHTML: '',
      _classes: classes,
      _attrs: attrs,
      _clickHandler: null as ((ev: any) => void) | null,
      setAttribute(k: string, v: any) {
        attrs[k] = v
        if (k === 'aria-label' && id && (id === 'cPlay' || id === 'sPlay')) {
          playAriaLabels[id[0] as 'c' | 's'].push(String(v))
        }
      },
      getAttribute(k: string) { return attrs[k] ?? null },
      appendChild(child: any) { children.push(child); return child },
      addEventListener(type: string, handler: (ev: any) => void) {
        if (type === 'click') el._clickHandler = handler
      },
      removeEventListener() {},
      querySelectorAll(sel: string) {
        if (sel === '.ip-step-btn') return el._stepBtns || []
        return []
      },
      querySelector() { return null },
      closest(sel: string) {
        if (sel === '.ip-lab') return el._lab || el
        return null
      },
      click() {
        if (el._clickHandler) el._clickHandler({ currentTarget: el })
      },
    }
    Object.defineProperty(el, 'textContent', {
      get() { return attrs._textContent || '' },
      set(v: string) { attrs._textContent = String(v) },
      configurable: true,
    })
    return el
  }

  const elementsById: Record<string, any> = {}
  const documentMock: any = {
    getElementById(id: string) {
      if (!elementsById[id]) {
        const el = makeElement(id)
        if (id.endsWith('Svg')) el.tagName = 'svg'
        // Steps 容器：预置 step buttons + lab 父元素
        if (id === 'cSteps' || id === 'sSteps') {
          const prefix = id[0] as 'c' | 's'
          const stepCount = prefix === 'c' ? 6 : 5
          el._stepBtns = []
          el._lab = makeElement(id + 'Lab')
          for (let i = 0; i < stepCount; i++) {
            const btn = makeElement()
            btn.dataset.step = String(i)
            if (i === 0) btn._classes.add('active')
            btn._classes.add('ip-step-btn')
            el._stepBtns.push(btn)
          }
          el._prefix = prefix
        }
        // Play 按钮初始 aria-label
        if (id === 'cPlay' || id === 'sPlay') {
          el._attrs['aria-label'] = '开始自动播放'
        }
        elementsById[id] = el
      }
      return elementsById[id]
    },
    createElementNS(_ns: string, tag: string) {
      const el = makeElement()
      el.tagName = tag
      return el
    },
    addEventListener(type: string, handler: () => void) {
      if (type === 'visibilitychange') visibilityHandlers.push(handler)
    },
    hidden: initialHidden,
  }

  const windowMock: any = {
    matchMedia(query: string) {
      return {
        matches: query.includes('prefers-reduced-motion') ? reducedMotion : false,
        addEventListener() {},
        removeEventListener() {},
      }
    },
  }
  // 仅在支持 IntersectionObserver 时挂载（否则 'IntersectionObserver' in window 应为 false）
  if (hasIO) {
    windowMock.IntersectionObserver = class {
      cb: (entries: any[]) => void
      constructor(cb: (entries: any[]) => void) {
        this.cb = cb
        ioCallbacks.push(cb)
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  }

  // 保存原始全局变量
  const origSetInterval = g.setInterval
  const origClearInterval = g.clearInterval
  const origDocument = g.document
  const origWindow = g.window
  const origMatchMedia = g.matchMedia
  const origIntersectionObserver = g.IntersectionObserver

  g.setInterval = (fn: () => void, ms: number) => {
    const id = ++timerCounter
    timers.push({ id, fn, ms })
    activeTimerIds.add(id)
    return id
  }
  g.clearInterval = (id: number) => {
    activeTimerIds.delete(id)
  }
  g.document = documentMock
  g.window = windowMock
  g.matchMedia = windowMock.matchMedia
  if (hasIO) {
    g.IntersectionObserver = windowMock.IntersectionObserver
  } else {
    g.IntersectionObserver = undefined
  }

  function recordActiveStep(prefix: 'c' | 's') {
    const stepsEl = elementsById[prefix + 'Steps']
    if (!stepsEl || !stepsEl._stepBtns) return
    for (let i = 0; i < stepsEl._stepBtns.length; i++) {
      if (stepsEl._stepBtns[i]._classes.has('active')) {
        stepActiveIndices[prefix].push(i)
        return
      }
    }
    stepActiveIndices[prefix].push(-1)
  }

  function advanceTimers(ms: number) {
    const snapshot = timers.filter((t) => activeTimerIds.has(t.id))
    for (const t of snapshot) {
      const triggers = Math.floor(ms / t.ms)
      for (let i = 0; i < triggers; i++) t.fn()
    }
    recordActiveStep('c')
    recordActiveStep('s')
  }

  function triggerIntersection(prefix: 'c' | 's', ratio: number) {
    const stepsEl = elementsById[prefix + 'Steps']
    const lab = stepsEl?._lab || stepsEl
    const entry = { target: lab, intersectionRatio: ratio }
    for (const cb of ioCallbacks) cb([entry])
    recordActiveStep(prefix)
  }

  function triggerVisibility(hidden: boolean) {
    documentMock.hidden = hidden
    for (const handler of visibilityHandlers) handler()
  }

  function clickButton(id: string) {
    const el = elementsById[id]
    if (el && el._clickHandler) {
      el._clickHandler({ currentTarget: el })
    } else if (el && el.click) {
      el.click()
    }
    recordActiveStep('c')
    recordActiveStep('s')
  }

  function getActiveStepIndex(prefix: 'c' | 's'): number {
    const stepsEl = elementsById[prefix + 'Steps']
    if (!stepsEl || !stepsEl._stepBtns) return -1
    for (let i = 0; i < stepsEl._stepBtns.length; i++) {
      if (stepsEl._stepBtns[i]._classes.has('active')) return i
    }
    return -1
  }

  return {
    document: documentMock,
    window: windowMock,
    timers,
    activeTimerIds,
    ioCallbacks,
    visibilityHandlers,
    playAriaLabels,
    stepActiveIndices,
    restore() {
      g.setInterval = origSetInterval
      g.clearInterval = origClearInterval
      g.document = origDocument
      g.window = origWindow
      g.matchMedia = origMatchMedia
      g.IntersectionObserver = origIntersectionObserver
    },
    advanceTimers,
    triggerIntersection,
    triggerVisibility,
    clickButton,
    getActiveStepIndex,
  }
}

/** 执行 JS 文件 */
function execJs(sandbox: Sandbox) {
  const js = readFileSync(JS_PATH, 'utf8')
  const exec = new Function('document', 'window', js)
  exec(sandbox.document, sandbox.window)
}

test('1. 默认播放按钮为暂停状态（wantsPlay=true，非 reduced-motion）', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: false })
  try {
    execJs(sandbox)
    // syncPlayButton 初始调用后，wantsPlay=true，aria-label 应为"暂停自动播放"
    const cLabels = sandbox.playAriaLabels.c
    const sLabels = sandbox.playAriaLabels.s
    assert.ok(cLabels.length > 0, 'cPlay 按钮应被 syncPlayButton 设置 aria-label')
    assert.ok(sLabels.length > 0, 'sPlay 按钮应被 syncPlayButton 设置 aria-label')
    assert.equal(cLabels[0], '暂停自动播放',
      `cPlay 初始 aria-label 应为"暂停自动播放"（实际：${cLabels[0]}）`)
    assert.equal(sLabels[0], '暂停自动播放',
      `sPlay 初始 aria-label 应为"暂停自动播放"（实际：${sLabels[0]}）`)
  } finally {
    sandbox.restore()
  }
})

test('2. 时间推进后步骤会自动变化', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: false })
  try {
    execJs(sandbox)
    // 不支持 IntersectionObserver 时 inViewport=true，应自动创建 timer
    assert.ok(sandbox.activeTimerIds.size > 0,
      '应创建至少一个 timer 用于自动播放')
    // 初始步骤为 0
    const beforeStep = sandbox.getActiveStepIndex('c')
    assert.equal(beforeStep, 0, '初始步骤应为 0')
    // 推进 6000ms（一步）
    sandbox.advanceTimers(6000)
    const afterStep = sandbox.getActiveStepIndex('c')
    assert.ok(afterStep !== beforeStep,
      `推进 6000ms 后步骤应变化（before=${beforeStep}, after=${afterStep}）`)
    assert.equal(afterStep, 1,
      `推进 6000ms 后步骤应为 1（实际：${afterStep}）`)
  } finally {
    sandbox.restore()
  }
})

test('3. 手动切换后继续播放并重置计时', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: false })
  try {
    execJs(sandbox)
    const timersBefore = sandbox.timers.length
    // 点击下一步
    sandbox.clickButton('cNext')
    // 点击后应重新创建 timer（restart），且 wantsPlay 仍为 true
    assert.ok(sandbox.timers.length > timersBefore,
      '手动切换后应重新创建 timer（重置计时）')
    // play 按钮 aria-label 应仍为暂停状态
    const lastCLabel = sandbox.playAriaLabels.c[sandbox.playAriaLabels.c.length - 1]
    assert.equal(lastCLabel, '暂停自动播放',
      `手动切换后 play 按钮应仍为暂停状态（实际：${lastCLabel}）`)
    // 步骤应已变化
    const step = sandbox.getActiveStepIndex('c')
    assert.equal(step, 1, `点击下一步后步骤应为 1（实际：${step}）`)
  } finally {
    sandbox.restore()
  }
})

test('4. 主动暂停后 IntersectionObserver 不会擅自恢复', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: true })
  try {
    execJs(sandbox)
    // 初始进入视口（ratio >= 0.35）
    sandbox.triggerIntersection('c', 0.5)
    assert.ok(sandbox.activeTimerIds.size > 0, '进入视口后应有活跃 timer')
    // 点击暂停
    sandbox.clickButton('cPlay')
    // 暂停后 wantsPlay=false，timer 应被清除
    assert.equal(sandbox.activeTimerIds.size, 0,
      '主动暂停后不应有活跃 timer')
    // 模拟 IntersectionObserver 离开再进入
    sandbox.triggerIntersection('c', 0)
    sandbox.triggerIntersection('c', 0.5)
    // wantsPlay 仍为 false，不应创建 timer
    assert.equal(sandbox.activeTimerIds.size, 0,
      '主动暂停后 IntersectionObserver 重新进入视口不应恢复 timer')
    // play 按钮 aria-label 应为"开始自动播放"
    const lastCLabel = sandbox.playAriaLabels.c[sandbox.playAriaLabels.c.length - 1]
    assert.equal(lastCLabel, '开始自动播放',
      `暂停后 play 按钮应为"开始自动播放"（实际：${lastCLabel}）`)
  } finally {
    sandbox.restore()
  }
})

test('5. 页面隐藏/恢复符合播放意图', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: false })
  try {
    execJs(sandbox)
    // 初始有 timer（wantsPlay=true, inViewport=true, documentVisible=true）
    assert.ok(sandbox.activeTimerIds.size > 0, '初始应有活跃 timer')
    // 页面隐藏
    sandbox.triggerVisibility(true)
    assert.equal(sandbox.activeTimerIds.size, 0,
      '页面隐藏后应停止计时')
    // 页面恢复（wantsPlay 仍为 true）
    sandbox.triggerVisibility(false)
    assert.ok(sandbox.activeTimerIds.size > 0,
      '页面恢复且 wantsPlay=true 时应恢复计时')
  } finally {
    sandbox.restore()
  }
})

test('6. prefers-reduced-motion 下默认不自动轮播', () => {
  const sandbox = createSandbox({ reducedMotion: true, hasIntersectionObserver: false })
  try {
    execJs(sandbox)
    // reduced-motion 时 wantsPlay=false，不应创建 timer
    assert.equal(sandbox.activeTimerIds.size, 0,
      'prefers-reduced-motion 下不应自动创建 timer')
    // play 按钮应为"开始自动播放"状态
    const cLabels = sandbox.playAriaLabels.c
    assert.equal(cLabels[0], '开始自动播放',
      `reduced-motion 下 cPlay 初始应为"开始自动播放"（实际：${cLabels[0]}）`)
    // 手动控制仍可用：点击 play 后应创建 timer
    sandbox.clickButton('cPlay')
    assert.ok(sandbox.activeTimerIds.size > 0,
      'reduced-motion 下手动点击 play 后应创建 timer')
  } finally {
    sandbox.restore()
  }
})

test('7. 两组动画状态独立', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: false })
  try {
    execJs(sandbox)
    // 暂停第一组
    sandbox.clickButton('cPlay')
    // c 组 timer 应被清除，但 s 组应仍活跃
    // 由于两组动画各自管理 timer，暂停 c 后 s 应仍播放
    assert.ok(sandbox.activeTimerIds.size > 0,
      '暂停第一组动画后第二组应继续播放（状态独立）')
    // sPlay 按钮应仍为暂停状态
    const lastSLabel = sandbox.playAriaLabels.s[sandbox.playAriaLabels.s.length - 1]
    assert.equal(lastSLabel, '暂停自动播放',
      `第二组动画 play 按钮应仍为暂停状态（实际：${lastSLabel}）`)
    // 再暂停第二组
    sandbox.clickButton('sPlay')
    assert.equal(sandbox.activeTimerIds.size, 0,
      '暂停两组动画后不应有活跃 timer')
  } finally {
    sandbox.restore()
  }
})

test('8. IntersectionObserver 离开视口暂停计时，再次进入后继续', () => {
  const sandbox = createSandbox({ reducedMotion: false, hasIntersectionObserver: true })
  try {
    execJs(sandbox)
    // 进入视口
    sandbox.triggerIntersection('c', 0.5)
    assert.ok(sandbox.activeTimerIds.size > 0, '进入视口后应有活跃 timer')
    // 离开视口
    sandbox.triggerIntersection('c', 0.1)
    assert.equal(sandbox.activeTimerIds.size, 0,
      '离开视口后应停止计时')
    // 再次进入视口（wantsPlay 仍为 true）
    sandbox.triggerIntersection('c', 0.5)
    assert.ok(sandbox.activeTimerIds.size > 0,
      '再次进入视口后应恢复计时')
  } finally {
    sandbox.restore()
  }
})
