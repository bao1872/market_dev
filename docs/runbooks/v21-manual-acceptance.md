# V2.1 远程手动验收 Runbook

> 本 Runbook 描述交付给你（用户）集中手动验收的步骤。它只说明如何执行验收，不重新定义产品行为（行为真源见 `docs/prd/31-after-close-product-closure-v2.1.md`）。
> 验收环境是 `panji-prod` 上的独立验证栈（`rules/80` DS-110/111/112），本地只发起 SSH Tunnel。

## 0. 前置条件（由执行代理在 Phase 5/6 完成并交付）

交付一次性给全以下内容：

- **目标 SHA**：`origin/dev` 精确 commit（40 位）。
- **SSH Tunnel 命令**：`ssh -N -L <local_port>:127.0.0.1:<server_port> panji-prod`（端口仅绑定服务器回环）。
- **访问 URL**：`http://127.0.0.1:<local_port>/`（仅经 Tunnel）。
- **verify admin 登录方式**：专用 `verify_admin` 账户（不写 `bz_stock`，只作用于验证库）。
- **测试交易日**：例如 `T-0`（验收日）、`T-1..T-120` 历史窗口。
- **测试股票**：约 30–50 只，覆盖四类场景。
- **四种场景**（见 §3）：A 完整成功 / B 异步增强 / C 降级 / D 治理与恢复。
- **页面清单**（见 §2）与每页预期状态（见 §4）。
- **已知限制**：诚实列出未达到的验收项。

## 1. 建立 Tunnel

```bash
ssh -N -L 8080:127.0.0.1:8080 panji-prod
# 浏览器打开 http://127.0.0.1:8080/
```

确认：页面可加载；无请求进入 `bz_stock`（验证栈独立 DB + Redis 隔离）。

## 2. 页面清单

| 路径 | 用途 |
|---|---|
| `/admin/data-production` | 九节点、closure、lineage、heartbeat、lease、child jobs |
| `/admin/tasks` | cancel / reconcile / resume / full restart / granular restart（全部 10 个 boundary） |
| `/market` | 只读正式 stock_core pointer |
| `/stock/:symbol` | source run、freshness、chip 状态、null 原因 |
| `/review` | 不等待 chip，显示 core+aggregation lineage |
| `/auction` | structure-only / hybrid / composite |
| `/boards` | 行业 L1/L2/L3 与 concept 分离 |

错误状态通用页：loading / null / degraded / failed / retryable。

## 3. 四类场景数据准备（由 seed CLI 生成，非一次性脚本）

`scripts/verify/seed_v21_verify_data.py` 必须可重跑生成：

- **场景 A 完整成功**：stock_core ready + dsa_projection ready + state_events ready + chip ready + auction composite + board_aggregation ready + review ready → closure `fully_ready`。
- **场景 B 异步增强**：stock_core ready + review ready + chip running + auction structure_only → closure `core_ready`。
- **场景 C 降级**：board_facts ready_reused + chip partial + auction hybrid → closure `degraded_ready`。
- **场景 D 治理与恢复**：publication missing + lease lost + retryable child + granular restart + reconcile。

## 4. 每页预期状态

- **Admin Data Production**：九节点状态与 §3 场景一致；lineage 链接可点；heartbeat/lease 实时；child jobs 列表显示 parent_job_run_id。
- **Admin Tasks**：granular restart 下拉包含全部 10 个 boundary，点击后产生对应 `SchedulerJobRun` 且不返回 501。
- **Market**：只读取正式 stock_core pointer，不显示未发布数据。
- **Stock Detail**：显示 source run id、freshness、chip 状态、null 原因（如适用）。
- **Review**：显示 core+aggregation lineage；chip 为 external enhancement 标注，不阻塞页面。
- **Auction**：structure-only/hybrid/composite 三种显式区分；hybrid 不得伪装 composite。
- **Board**：行业 L1/L2/L3 与 concept 分开展示，不混用。

## 5. 问题记录格式

集中反馈时按以下分类与格式记录：

```text
[P0] 页面不可用 / 数据错误 / 动作错误
  - 页面：/admin/tasks
  - 操作：granular restart -> chip
  - 预期：新建 ChipConsensusRun
  - 实际：返回 501
  - 截图/requestId：

[P1] 状态表达 / 字段缺失 / 交互不完整
[P2] 布局 / 文案 / 视觉
```

P0 = 阻断验收；P1 = 状态/字段/交互；P2 = 视觉。批量反馈后由执行代理统一修复（Phase 8），不逐条部署。
