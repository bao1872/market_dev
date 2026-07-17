// [BoardFilterCombobox] - 描述: 板块筛选可复用 Combobox（行业关键词 / 概念精确）
// CHANGE-20260716-007：替换浏览器原生 datalist，盘迹风格深色下拉。
//
// 两种模式：
//  1. mode="industry"：行业关键词模式
//     - 输入任意关键词，Enter 提交关键词本身（不做精确校验）
//     - 本地过滤完整路径 "一级-二级-三级"，最多展示 12 条建议
//     - 展示格式 "一级 / 二级 / 三级"（将 - 渲染为 /）
//     - 点击建议项提交命中的完整路径（便于一键精确筛选）
//     - 高亮命中关键词
//  2. mode="concept"：概念精确模式
//     - 本地搜索概念目录，只提交选中的精确概念名称
//     - Enter 不提交无效文本（不在目录中）
//
// 通用交互：
//  - ArrowUp/ArrowDown 导航建议项
//  - Enter 提交（industry: 当前输入或选中项；concept: 仅选中项）
//  - Escape 关闭面板（不清空输入）
//  - 点击外部关闭面板
//  - 清除按钮（×）立即提交空值
//  - aria-combobox/listbox/option 完整 a11y
//  - blur 延迟 150ms 触发，确保先处理 click 事件再 blur
//
// 样式：使用 MarketWorkspace.module.scss 共享变量，不引入新依赖。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MarketBoardItem } from '@/api/endpoints'
import styles from './MarketWorkspace.module.scss'

export type BoardFilterMode = 'industry' | 'concept'

interface BoardFilterComboboxProps {
  /** 当前受控值（industry: 关键词；concept: 精确概念名） */
  value: string
  /** 值变更回调（已 trim；空串表示清空） */
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

/** 规范化输入：trim + 内部空白折叠为单空格（用于本地过滤与提交） */
function normalizeInput(raw: string): string {
  return raw.trim().replace(/\s+/g, ' ')
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
  // 当前高亮选项索引（-1 表示未高亮）
  const [activeIndex, setActiveIndex] = useState(-1)

  const containerRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // 用于 blur 延迟提交时判断是否已被 click 处理取消
  const blurTimerRef = useRef<number | null>(null)

  // 外部 value 变化（URL hydration / preset 应用 / 清空）同步到本地输入
  useEffect(() => {
    setInputValue(value)
  }, [value])

  // 候选名称列表（缓存避免重复 map）
  const optionNames = useMemo(() => options.map((b) => b.name), [options])

  // 本地过滤：基于当前输入值的前缀/包含匹配
  const suggestions = useMemo(() => {
    const normalized = normalizeInput(inputValue)
    if (!normalized) {
      // 空输入：展示前 MAX_SUGGESTIONS 条（便于浏览）
      return optionNames.slice(0, MAX_SUGGESTIONS)
    }
    const lower = normalized.toLowerCase()
    // 包含匹配（不区分大小写）
    const matched = optionNames.filter((name) => name.toLowerCase().includes(lower))
    return matched.slice(0, MAX_SUGGESTIONS)
  }, [inputValue, optionNames])

  // 概念模式：精确值集合（用于 Enter 时校验）
  const conceptNameSet = useMemo(
    () => new Set(optionNames),
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

  // 提交值（统一入口，已 trim）
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

  // 打开面板 + 重置高亮
  const openPanel = useCallback(() => {
    setOpen(true)
    setActiveIndex(suggestions.length > 0 ? 0 : -1)
  }, [suggestions.length])

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

  // 输入变化：仅更新本地 state + 重置高亮 + 展开面板
  // 清空（空串）立即提交
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const v = e.target.value
      setInputValue(v)
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
          // 选中项高亮时：提交该项完整路径
          selectSuggestion(suggestions[activeIndex])
        } else {
          // industry 模式：提交当前输入关键词
          // concept 模式：仅当输入在目录中时提交
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

  // 清除按钮：立即提交空值
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

  // combo id 唯一性（基于 mode）
  const listboxId = `board-filter-listbox-${mode}`

  // active descendent id
  const activeOptionId =
    open && activeIndex >= 0 ? `${listboxId}-opt-${activeIndex}` : undefined

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
          tabIndex={-1}
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
    </div>
  )
}
