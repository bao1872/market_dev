/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_GIT_SHA: string
  readonly VITE_BUILD_TIME: string
  // [门户] - 描述: 内测申请表单地址（未配置时门户显示"申请通道暂未配置"）
  readonly VITE_BETA_APPLY_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
