import type { ProfileBin } from '../demo/chipConsensusStory'
import styles from '../marketing.module.scss'

type Props = {
  profile: ProfileBin[]
  minPrice: number
  maxPrice: number
  height: number
  /** 当前主要成交密集价：该 bin 用 brand 实色高亮 */
  highlightPrice: number | null
}

// 教学用成交分布：纯 SVG，不引第三方图库；颜色只走 CSS token（--brand）。
export function VolumeProfile({ profile, minPrice, maxPrice, height, highlightPrice }: Props) {
  const maxVolume = Math.max(...profile.map((item) => item.volume), 1)
  const range = Math.max(maxPrice - minPrice, 0.01)

  return (
    <svg
      className={styles.profileSvg}
      viewBox={`0 0 180 ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="成交分布"
    >
      {profile.map((bin) => {
        const y = ((maxPrice - bin.price) / range) * height
        const width = (bin.volume / maxVolume) * 165
        const highlighted = highlightPrice !== null && bin.price === highlightPrice

        return (
          <rect
            key={bin.price}
            className={highlighted ? styles.profileBinMax : styles.profileBin}
            x={180 - width}
            y={y - 4}
            width={width}
            height={8}
            rx={2}
          />
        )
      })}
    </svg>
  )
}
