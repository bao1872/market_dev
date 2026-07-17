// [BoardFilterCombobox] - 描述: 板块筛选可复用 Combobox（行业关键词 / 概念精确）
// CHANGE-20260716-007：替换浏览器原生 datalist，盘迹风格深色下拉。
// PR #77 收口：修复 Enter 误选首条建议、建议排序、NFKC、useId、无结果反馈、长路径面板宽度。
//
// 两种模式：
//  1. mode="industry"：行业关键词模式
//     - 输入任意关键词，Enter 提交关键词本身（不做精确校验）
//     - 本地过滤完整路径 "一级-二级-三级"，最多展示 12 条建议
//     - 展示格式 "一级 / 二级 / 三级"（将 - 渲染为 /）
//     - 点击建议项或 ArrowDown+Enter 提交命中的完整路径
//     - 高亮命中关键词
//  2. mode="concept"：概念精确模式
//     - 本地搜索概念目录，只提交选中的精确概念名称
//     - Enter 不提交无效文本（不在目录中），保留原已提交值
//
// 交互规则（PR #77 收口）：
//  - 打开面板/输入变化时 activeIndex=-1（不默认激活首条建议）
//  - 只有 ArrowDown/ArrowUp 或鼠标 hover 后才激活具体建议
//  - Enter：
//    - 未激活建议（activeIndex=-1）：industry 提交当前关键词；concept 仅在精确存在时提交
//    - 已主动激活建议：提交该建议完整路径/概念名
//  - 建议排序：精确匹配 → 前缀匹配 → 包含匹配 → 名称稳定排序
//  - 输入规范化：NFKC + trim + 折叠空白（与后端一致）
//  - Escape 关闭面板（不清空输入）
//  - 点击外部关闭面板
//  - 清除按钮（×）立即提交空值，键盘可达（无 tabIndex=-1）
//  - 无结果时显示盘迹风格"无匹配项"，不空白消失
//  - aria-combobox/listbox/option 完整 a11y，useId 生成唯一 id
//  - blur 延迟 150ms 触发，确保先处理 click 事件再 blur
//
// 样式：使用 MarketWorkspace.module.scss 共享变量，不引入新依赖。
// 输入框宽度 industry 220/240px、concept 160/200px；下拉面板允许 360~480px（受视口限制）。
import { useCallback, useId, useEffect, useMemo, useRef, useState } from 'react'
import type { MarketBoardItem } from '@/api/endpoints'
import styles from './MarketWorkspace.module.scss'

export type BoardFilterMode = 'industry' | 'concept'

interface BoardFilterComboboxProps {
  /** 当前受控值（industry: 关键词；concept: 精确概念名） */
  value: string
  /** 值变更回调（已 NFKC+trim；空串表示清空） */
  onChange: (value: string) => void
  /** 候选项（已按 type 过滤） */
  options: MarketBoardItem[]
  /** 模式：industry=关键词；concept=精确 */
  mode: BoardFilterMode
  /** placeholder 文案 */
  placeholder: string
  /** 是否禁用（boards.available=false 时禁用） */
  disabled?: boolean
  /** aria-label */
  ariaLabel: string
}

/** 最多展示的建议条数（PROMPT §3.3） */
const MAX_SUGGESTIONS = 12

/** blur 延迟：先处理 click 再触发 blur 提交（PROMPT §3.5） */
const BLUR_COMMIT_DELAY_MS = 150

/** 将行业路径中的 `-` 渲染为 `/`（仅显示，提交值不变） */
function displayIndustryName(name: string): string {
  return name.replace(/-/g, ' / ')
}

/** 高亮匹配子串：返回分段数组（matched=true 的段高亮） */
function highlightSegments(text: string, keyword: string): Array<{ text: string; matched: boolean }> {
  if (!keyword) return [{ text, matched: false }]
  const lower = text.toLowerCase()
  const kwLower = keyword.toLowerCase()
  const segments: Array<{ text: string; matched: boolean }> = []
  let cursor = 0
  let idx = lower.indexOf(kwLower, cursor)
  while (idx >= 0) {
    if (idx > cursor) {
      segments.push({ text: text.slice(cursor, idx), matched: false })
    }
    segments.push({ text: text.slice(idx, idx + kwLower.length), matched: true })
    cursor = idx + kwLower.length
    idx = lower.indexOf(kwLower, cursor)
  }
  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), matched: false })
  }
  return segments
}

/** 规范化输入：NFKC + trim + 内部空白折叠为单空格（与后端 _normalize_keyword 一致） */
function normalizeInput(raw: string): string {
  return raw.normalize('NFKC').trim().replace(/\s+/g, ' ')
}

/** 建议排序权重：精确匹配(0) → 前缀匹配(1) → 包含匹配(2) → 名称稳定排序 */
function suggestionRank(name: string, normalizedLower: string): number {
  const nameLower = name.toLowerCase()
  if (nameLower === normalizedLower) return 0
  if (nameLower.startsWith(normalizedLower)) return 1
  return 2
}

export function BoardFilterCombobox({
  value,
  onChange,
  options,
  mode,
  placeholder,
  disabled = false,
  ariaLabel,
}: BoardFilterComboboxProps) {
  // 本地输入：打字时仅更新本地 state，避免逐字符触发 URL/API 写入
  const [inputValue, setInputValue] = useState(value)
  // 面板是否展开
  const [open, setOpen] = useState(false)
  // 当前高亮选项索引（-1 表示未高亮；打开/输入时不自动激活，仅 ArrowUp/Down/hover 激活）
  const [activeIndex, setActiveIndex] = useState(-1)

  const containerRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // 用于 blur 延迟提交时判断是否已被 click 处理取消
  const blurTimerRef = useRef<number | null>(null)

  // useId 生成唯一 listbox/option id 前缀（避免多实例冲突）
  const reactId = useId()
  const listboxId = `${reactId}-board-filter-listbox`

  // 外部 value 变化（URL hydration / preset 应用 / 清空）同步到本地输入
  useEffect(() => {
    setInputValue(value)
  }, [value])

  // 候选名称列表（缓存避免重复 map）
  const optionNames = useMemo(() => options.map((b) => b.name), [options])

  // 本地过滤 + 排序：精确匹配 → 前缀匹配 → 包含匹配 → 名称稳定排序
  const suggestions = useMemo(() => {
    const normalized = normalizeInput(inputValue)
    if (!normalized) {
      // 空输入：展示前 MAX_SUGGESTIONS 条（按名称稳定排序）
      return optionNames.slice(0, MAX_SUGGESTIONS)
    }
    const lower = normalized.toLowerCase()
    const matched = optionNames
      .filter((name) => name.toLowerCase().includes(lower))
      .map((name) => ({ name, rank: suggestionRank(name, lower) }))
      .sort((a, b) => {
        if (a.rank !== b.rank) return a.rank - b.rank
        return a.name.localeCompare(b.name, 'zh-Hans-CN')
      })
      .map((item) => item.name)
    return matched.slice(0, MAX_SUGGESTIONS)
  }, [inputValue, optionNames])

  // 概念模式：精确值集合（用于 Enter 时校验，使用 NFKC 规范化后的名称）
  const conceptNameSet = useMemo(
    () => new Set(optionNames.map((n) => n.normalize('NFKC').trim().replace(/\s+/g, ' '))),
    [optionNames],
  )

  // 清理 blur 定时器（组件卸载时）
  useEffect(() => {
    return () => {
      if (blurTimerRef.current !== null) {
        window.clearTimeout(blurTimerRef.current)
      }
    }
  }, [])

  // 提交值（统一入口，已 NFKC+trim）
  const commit = useCallback(
    (next: string) => {
      const normalized = normalizeInput(next)
      // industry 模式：任意关键词都接受
      // concept 模式：仅接受目录中的精确值或空值
      if (mode === 'concept' && normalized !== '' && !conceptNameSet.has(normalized)) {
        // 概念无效：回退到上一个已提交值
        setInputValue(value)
        return
      }
      onChange(normalized)
    },
    [mode, conceptNameSet, onChange, value],
  )

  // 打开面板 + 重置高亮（PR #77 收口：activeIndex=-1，不默认激活首条）
  const openPanel = useCallback(() => {
    setOpen(true)
    setActiveIndex(-1)
  }, [])

  // 关闭面板
  const closePanel = useCallback(() => {
    setOpen(false)
    setActiveIndex(-1)
  }, [])

  // 选择某条建议（点击或 Enter 命中时调用）
  const selectSuggestion = useCallback(
    (name: string) => {
      setInputValue(name)
      commit(name)
      closePanel()
    },
    [commit, closePanel],
  )

  // 输入变化：仅更新本地 state + 重置高亮（-1）+ 展开面板
  // 清空（空串）立即提交
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const v = e.target.value
      setInputValue(v)
      setActiveIndex(-1)
      if (v === '') {
        // 清空立即提交（空串是明确意图）
        onChange('')
        closePanel()
        return
      }
      openPanel()
    },
    [onChange, openPanel, closePanel],
  )

  // 键盘交互
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'ArrowDown') {
        if (suggestions.length === 0) return
        e.preventDefault()
        if (!open) {
          openPanel()
          return
        }
        setActiveIndex((idx) => (idx + 1) % suggestions.length)
      } else if (e.key === 'ArrowUp') {
        if (suggestions.length === 0) return
        e.preventDefault()
        if (!open) {
          openPanel()
          return
        }
        setActiveIndex((idx) => (idx - 1 + suggestions.length) % suggestions.length)
      } else if (e.key === 'Enter') {
        e.preventDefault()
        if (open && activeIndex >= 0 && activeIndex < suggestions.length) {
          // 用户已主动激活建议（ArrowDown/Up/hover）：提交该项完整路径/概念名
          selectSuggestion(suggestions[activeIndex])
        } else {
          // 未激活建议（activeIndex=-1）：
          // industry 模式：提交当前输入关键词
          // concept 模式：仅当输入在目录中时提交，否则保留原值
          commit(inputValue)
          closePanel()
        }
      } else if (e.key === 'Escape') {
        e.preventDefault()
        closePanel()
      }
    },
    [suggestions, open, activeIndex, selectSuggestion, commit, inputValue, closePanel, openPanel],
  )

  // 焦点：展开面板
  const handleFocus = useCallback(() => {
    if (disabled) return
    openPanel()
  }, [disabled, openPanel])

  // blur：延迟提交，确保先处理建议项 click
  const handleBlur = useCallback(() => {
    if (blurTimerRef.current !== null) {
      window.clearTimeout(blurTimerRef.current)
    }
    blurTimerRef.current = window.setTimeout(() => {
      // blur 提交当前输入值（industry 任意 / concept 必须精确）
      commit(inputValue)
      closePanel()
      blurTimerRef.current = null
    }, BLUR_COMMIT_DELAY_MS)
  }, [commit, inputValue, closePanel])

  // 建议项点击：取消 blur 定时器，立即提交
  const handleSuggestionClick = useCallback(
    (name: string) => {
      if (blurTimerRef.current !== null) {
        window.clearTimeout(blurTimerRef.current)
        blurTimerRef.current = null
      }
      selectSuggestion(name)
    },
    [selectSuggestion],
  )

  // 建议项 hover：更新高亮索引（不影响键盘焦点）
  const handleSuggestionMouseEnter = useCallback((idx: number) => {
    setActiveIndex(idx)
  }, [])

  // 点击外部关闭面板
  useEffect(() => {
    if (!open) return
    const handleClickOutside = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        closePanel()
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [open, closePanel])

  // 清除按钮：立即提交空值（键盘可达，无 tabIndex=-1）
  const handleClear = useCallback(() => {
    if (blurTimerRef.current !== null) {
      window.clearTimeout(blurTimerRef.current)
      blurTimerRef.current = null
    }
    setInputValue('')
    onChange('')
    closePanel()
    // 重新聚焦输入框，便于继续输入
    inputRef.current?.focus()
  }, [onChange, closePanel])

  // 控件宽度：industry 220px / concept 180px（PROMPT §3.6）
  const containerClassName =
    mode === 'industry' ? styles.comboboxIndustry : styles.comboboxConcept

  // 显示用的建议项文本（industry 路径将 - 渲染为 /）
  const displayText = useCallback(
    (name: string) => (mode === 'industry' ? displayIndustryName(name) : name),
    [mode],
  )

  // 高亮关键词（仅 industry 模式，concept 模式不高亮）
  const highlightKeyword = mode === 'industry' ? normalizeInput(inputValue) : ''

  // active descendent id
  const activeOptionId =
    open && activeIndex >= 0 ? `${listboxId}-opt-${activeIndex}` : undefined

  // 是否有输入但无匹配建议（用于显示"无匹配项"）
  const hasInputNoMatch = open && suggestions.length === 0 && normalizeInput(inputValue) !== ''

  return (
    <div
      ref={containerRef}
      className={`${styles.combobox} ${containerClassName}`}
    >
      <input
        ref={inputRef}
        type="text"
        className={styles.comboboxInput}
        placeholder={placeholder}
        value={inputValue}
        onChange={handleInputChange}
        onKeyDown={handleKeyDown}
        onFocus={handleFocus}
        onBlur={handleBlur}
        disabled={disabled}
        aria-label={ariaLabel}
        role="combobox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-activedescendant={activeOptionId}
        autoComplete="off"
        spellCheck={false}
      />
      {inputValue !== '' && !disabled && (
        <button
          type="button"
          className={styles.comboboxClear}
          onClick={handleClear}
          aria-label="清除"
        >
          ×
        </button>
      )}
      {open && suggestions.length > 0 && (
        <ul
          id={listboxId}
          className={styles.comboboxPanel}
          role="listbox"
          aria-label={ariaLabel}
        >
          {suggestions.map((name, idx) => {
            const display = displayText(name)
            const segments = highlightSegments(display, highlightKeyword)
            return (
              <li
                key={name}
                id={`${listboxId}-opt-${idx}`}
                role="option"
                aria-selected={idx === activeIndex}
                className={
                  idx === activeIndex
                    ? `${styles.comboboxOption} ${styles.comboboxOptionActive}`
                    : styles.comboboxOption
                }
                title={mode === 'industry' ? displayIndustryName(name) : name}
                onMouseDown={(e) => {
                  // 阻止默认行为防止 input blur 在 click 之前触发
                  e.preventDefault()
                }}
                onClick={() => handleSuggestionClick(name)}
                onMouseEnter={() => handleSuggestionMouseEnter(idx)}
              >
                {segments.map((seg, i) =>
                  seg.matched ? (
                    <mark key={i} className={styles.comboboxHighlight}>
                      {seg.text}
                    </mark>
                  ) : (
                    <span key={i}>{seg.text}</span>
                  ),
                )}
              </li>
            )
          })}
        </ul>
      )}
      {hasInputNoMatch && (
        <ul
          id={listboxId}
          className={styles.comboboxPanel}
          role="listbox"
          aria-label={ariaLabel}
        >
          <li
            className={styles.comboboxOption}
            role="option"
            aria-selected={false}
            aria-disabled="true"
          >
            {mode === 'industry' ? '无匹配行业' : '未找到该概念'}
          </li>
        </ul>
      )}
    </div>
  )
}
