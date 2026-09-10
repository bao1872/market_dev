// 回放帧工具（V1）— 纯函数，可被 node --test 直接运行。
//
// findCanonicalFrameIndex 是 Structure / Chip 等真实回放共用的「无未来」帧选择器：
// 只允许选取 endIndex <= 当前可见位置的最大 canonical 帧，禁止在播放到该帧之前
// 使用它的算法结果。组件里禁止各自复制一份二分实现。
export function findCanonicalFrameIndex(
  frames: ReadonlyArray<{ endIndex: number }>,
  visibleEndIndex: number,
): number {
  let lo = 0
  let hi = frames.length - 1
  let best = -1
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2)
    if (frames[mid].endIndex <= visibleEndIndex) {
      best = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return best
}
