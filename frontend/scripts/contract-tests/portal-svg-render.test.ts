// [门户] - 描述: 指标原理页结构 SVG DOM 渲染测试（CHANGE-20260724-002）
// 用法：node --experimental-strip-types --test scripts/contract-tests/portal-svg-render.test.ts
//
// 目的：
//   加载 data-principles.js 并在最小化 DOM 环境中执行，
//   检查 structureSvg 中真实渲染的 SVG text 元素包含 HH / HL / LH / LL 四种标签。
//
//   禁止只检查 JS 源码字符串——必须执行 buildStructure() 逻辑后检查生成的 DOM 文本。
//
// 背景：
//   页面"标注关系"步骤说明声称展示 HH/HL/LH/LL，但若 structureRaw 数据整体单调上升，
//   zigzag(.025) 过滤后的标签算法只会生成 H/L/HH/HL，LH/LL 不会出现。
//   本测试确保 structureRaw 包含上升段+下降段，使四种标签真实渲染。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PORTAL_DIR = join(__dirname, '..', '..', 'public', 'portal')
const JS_PATH = join(PORTAL_DIR, 'assets', 'js', 'data-principles.js')

/**
 * 最小化 DOM mock：模拟 data-principles.js 需要的 document API。
 * 收集所有 createElementNS('text') 元素的 textContent，用于断言标签存在性。
 */
function createDomMock() {
  const textContents: string[] = []

  function makeElement(tag: string): any {
    const children: any[] = []
    const style: Record<string, string> = {}
    const classList = {
      toggle: () => {},
      add: () => {},
      remove: () => {},
      contains: () => false,
    }
    const attrs: Record<string, any> = {}
    const el: any = {
      tagName: tag,
      children,
      style,
      classList,
      dataset: {},
      innerHTML: '',
      setAttribute(k: string, v: any) { attrs[k] = v },
      getAttribute(k: string) { return attrs[k] },
      appendChild(child: any) { children.push(child); return child },
      addEventListener() {},
      removeEventListener() {},
      querySelectorAll() { return [] },
      querySelector() { return null },
    }
    Object.defineProperty(el, 'textContent', {
      get() { return el._textContent ?? '' },
      set(v: string) {
        el._textContent = String(v)
        // 仅收集 SVG text 元素的文本（标签 HH/HL/LH/LL/H/L）
        if (tag === 'text') textContents.push(String(v))
      },
      configurable: true,
    })
    return el
  }

  const elementsById: Record<string, any> = {}
  const documentMock: any = {
    getElementById(id: string) {
      if (!elementsById[id]) {
        // SVG 根元素用 'svg'，其他用 'div'
        elementsById[id] = makeElement(id.endsWith('Svg') ? 'svg' : 'div')
      }
      return elementsById[id]
    },
    createElementNS(_ns: string, tag: string) {
      return makeElement(tag)
    },
    addEventListener() {},
    hidden: false,
  }

  // setInterval / clearInterval mock（setupPlayer 使用）
  const globalThis_ = globalThis as any
  const origSetInterval = globalThis_.setInterval
  const origClearInterval = globalThis_.clearInterval

  return {
    document: documentMock,
    textContents,
    restore() {
      globalThis_.setInterval = origSetInterval
      globalThis_.clearInterval = origClearInterval
    },
  }
}

test('结构 SVG 渲染后真实包含 HH/HL/LH/LL 四种标签（CHANGE-20260724-002）', () => {
  const js = readFileSync(JS_PATH, 'utf8')

  const { document, textContents, restore } = createDomMock()

  // mock setInterval/clearInterval（setupPlayer 调用）
  ;(globalThis as any).setInterval = () => 0
  ;(globalThis as any).clearInterval = () => {}

  try {
    // 用 new Function 在沙箱中执行整个 IIFE 文件，注入 document
    const exec = new Function('document', js)
    exec(document)
  } finally {
    restore()
  }

  // 合并所有 SVG text 元素的文本内容
  const allText = textContents.join('|')

  // 断言四种标签真实出现在 SVG DOM 文本中
  assert.ok(allText.includes('HH'),
    `SVG text 元素应包含 HH 标签（实际文本片段：${allText.slice(0, 200)}...）`)
  assert.ok(allText.includes('HL'),
    `SVG text 元素应包含 HL 标签（实际文本片段：${allText.slice(0, 200)}...）`)
  assert.ok(allText.includes('LH'),
    `SVG text 元素应包含 LH 标签（实际文本片段：${allText.slice(0, 200)}...）`)
  assert.ok(allText.includes('LL'),
    `SVG text 元素应包含 LL 标签（实际文本片段：${allText.slice(0, 200)}...）`)
})

test('结构 SVG 渲染后标签集合至少包含 4 种不同标签类型', () => {
  const js = readFileSync(JS_PATH, 'utf8')

  const { document, textContents, restore } = createDomMock()
  ;(globalThis as any).setInterval = () => 0
  ;(globalThis as any).clearInterval = () => {}

  try {
    const exec = new Function('document', js)
    exec(document)
  } finally {
    restore()
  }

  // 结构标签集合应至少包含 HH/HL/LH/LL 四种（可能还有 H/L）
  const labelSet = new Set(textContents.filter(t => /^(H|L|HH|HL|LH|LL)$/.test(t)))
  assert.ok(labelSet.size >= 4,
    `结构标签种类应 >= 4（实际：${Array.from(labelSet).join(', ')}）`)
  assert.ok(labelSet.has('HH'), '标签集合应包含 HH')
  assert.ok(labelSet.has('HL'), '标签集合应包含 HL')
  assert.ok(labelSet.has('LH'), '标签集合应包含 LH')
  assert.ok(labelSet.has('LL'), '标签集合应包含 LL')
})
