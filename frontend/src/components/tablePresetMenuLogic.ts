// [Presets] - 描述: TablePresetMenu 保存逻辑（纯函数，便于独立测试）
import type { TableViewPresetCreateRequest } from '@/api/endpoints'

/** 保存 preset 需要的上下文 */
export interface SavePresetContext {
  name: string
  tableId: string
  strategyKey: string | null | undefined
  payload: Record<string, unknown>
  isDefault: boolean
  createMutation: { mutateAsync: (payload: TableViewPresetCreateRequest) => Promise<unknown> }
  presetsQuery: { refetch: () => Promise<unknown> }
  toast: { show: (title: string, msg: string) => void }
  setEditingName: (value: string) => void
  setSaveError: (msg: string | null) => void
}

/** 保存当前配置为新 preset（可独立测试的核心逻辑） */
export async function savePreset(ctx: SavePresetContext): Promise<void> {
  const trimmed = ctx.name.trim()
  if (!trimmed) {
    ctx.toast.show('保存配置', '请输入配置名称')
    return
  }
  try {
    await ctx.createMutation.mutateAsync({
      table_id: ctx.tableId,
      strategy_key: ctx.strategyKey ?? null,
      name: trimmed,
      config: ctx.payload,
      is_default: ctx.isDefault,
    })
    ctx.toast.show('保存配置', `已保存「${trimmed}」`)
    ctx.setEditingName('')
    ctx.setSaveError(null)
    await ctx.presetsQuery.refetch()
  } catch (e: unknown) {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '保存失败'
    ctx.setSaveError(msg)
    ctx.toast.show('保存配置失败', msg)
  }
}
