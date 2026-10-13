# CHANGE-20261013-001 — Frontend Source-only Live Refresh 内存门禁细分

- 状态：implemented_pending_runtime_validation（GPT 提供代码；部署后核验仍待执行）
- 类型：受保护治理规则 + 部署脚本资源门禁调整
- 背景：8GB 物理机上 `MemAvailable=3813 MiB`，原 4096 MiB 通用门槛阻断只更新前端静态资源的「波段」Live Refresh。
- 当前代码基线：`cc5c823ee93705d426c7cef17ae2c7f5248c09b1`；目标 SHA 由 IDE 提交后回传。

## 合同变更

- 仅严格纯前端 source-only、已有 Live Mount、没有 backend/capture/env/Compose/Migration 变化的部署使用独立 `PANJI_FRONTEND_REFRESH_MIN_MEM_MB`（默认/最低 3584 MiB）。
- 这一路径将临时 Vite Docker build 限制为 `--memory=2g --memory-swap=2g`；任何构建失败均 fail-closed。
- 其他部署保留 `PANJI_MIN_MEM_MB` 默认/最低 4096 MiB；运行时资源、Docker Compose 容器配额与任何数据库配置不变。
- 内存检查仍在必要的 fence 之后和首笔 runtime mutation 之前；dry-run 不读取主机内存，除非显式使用现有 test seam。
- 部署仍只接受 origin/dev exact SHA、通过 `scripts/ops/panji-test-deploy`，不新增入口。

## 预期测试与真实验收

- 增加基于真实部署函数的 14 项轻量合同测试；原有部署静态/行为测试全量保留。
- 首次真实 Live Refresh 前必须 dry-run 并核对旧 runtime → 新 SHA 的全部差异；若分类超出 frontend-only 或内存不足必须停止，禁止临时调低门槛。
- 成功后核对 `RUNTIME_SHA`、版本接口、frontend 静态构建 manifest、容器状态和「波段」真实 UI。
- 操作允许短暂的可控服务中断，但不会无差别重启/重建；数据库写入、Migration、数据操作未授权。
