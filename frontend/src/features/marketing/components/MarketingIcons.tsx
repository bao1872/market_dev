// 营销门户内联 SVG 图标（无第三方图标依赖，无 emoji）。
// 统一 stroke=currentColor，颜色由 CSS 控制（品牌绿用于 active / CTA / focus）。
import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

const base = (props: IconProps): IconProps => ({
  width: 24,
  height: 24,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
  ...props,
})

export function IconRadar(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4.5" />
      <path d="M12 12 L19 7" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconSliders(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4 7h11M19 7h1" />
      <circle cx="17" cy="7" r="2" />
      <path d="M4 17h1M9 17h11" />
      <circle cx="7" cy="17" r="2" />
    </svg>
  )
}

export function IconEye(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="2.6" />
    </svg>
  )
}

export function IconBookmark(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 4h12v16l-6-4-6 4V4Z" />
    </svg>
  )
}

export function IconPulse(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 12h4l2-6 4 12 2-6h6" />
    </svg>
  )
}

export function IconBell(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2Z" />
      <path d="M10 20a2 2 0 0 0 4 0" />
    </svg>
  )
}

export function IconTrend(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 17l6-6 4 4 8-8" />
      <path d="M21 7v4h-4" />
    </svg>
  )
}

export function IconFilter(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 5h18l-7 8v6l-4 2v-8L3 5Z" />
    </svg>
  )
}

export function IconBolt(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />
    </svg>
  )
}

export function IconLayers(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3 2 9l10 6 10-6-10-6Z" />
      <path d="M2 15l10 6 10-6" />
    </svg>
  )
}

export function IconArrowRight(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  )
}

export function IconArrowDown(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 5v14M6 13l6 6 6-6" />
    </svg>
  )
}

export function IconPlay(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M7 5l12 7-12 7V5Z" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconPause(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor" stroke="none" />
      <rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconPrev(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M15 6l-6 6 6 6" />
    </svg>
  )
}

export function IconNext(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M9 6l6 6-6 6" />
    </svg>
  )
}

export function IconReplay(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4 12a8 8 0 1 1 2.5 5.8" />
      <path d="M4 12V7M4 12h5" />
    </svg>
  )
}

export function IconXueqiu(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="12" r="9" />
      <path d="M7.5 9.5c2.5-1.2 5.5-.4 6.8 1.8" />
      <path d="M16.5 14.5c-2.5 1.2-5.5.4-6.8-1.8" />
    </svg>
  )
}

export function IconQQ(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 3c3.3 0 5.5 2.2 5.5 5.2 0 1.4.7 2.3 1.3 3.4.4.8.4 1.4.1 1.5-.5.2-1.6-1-1.9-1.4-.4 1.6-1.9 2.8-3.6 2.8-1 0-1.9-.3-2.6-.9" />
      <path d="M12 3c-3.3 0-5.5 2.2-5.5 5.2 0 1.4-.7 2.3-1.3 3.4-.4.8-.4 1.4-.1 1.5.5.2 1.6-1 1.9-1.4.4 1.6 1.9 2.8 3.6 2.8 1 0 1.9-.3 2.6-.9" />
    </svg>
  )
}

// 场景图标映射（StrategyLab 复用）
export const SCENARIO_ICONS = {
  trend: IconTrend,
  filter: IconFilter,
  bolt: IconBolt,
  layers: IconLayers,
} as const

export type ScenarioIconKey = keyof typeof SCENARIO_ICONS

// 流程步骤图标映射（Workflow 6 步复用）
export const STEP_ICONS = {
  radar: IconRadar,
  sliders: IconSliders,
  eye: IconEye,
  bookmark: IconBookmark,
  pulse: IconPulse,
  bell: IconBell,
} as const

export type StepIconKey = keyof typeof STEP_ICONS
