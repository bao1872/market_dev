# Runbook — Worker Pickup Admission (E2.1 P1-C)

部署期 after-close pickup 暂停操作面。所有 pause/status/release 统一走 canonical
admission service；**禁止** shell / 其它入口直接 `UPDATE worker_pickup_admission`。

## 0. 操作面

```bash
# 操作面封装（统一调用 backend/scripts/worker_pickup_admission.py）
scripts/ops/panji-admission acquire  --scope after_close_orchestrator --actor "operator:<who>" --reason "..."
scripts/ops/panji-admission status  --scope after_close_orchestrator
scripts/ops/panji-admission release --scope after_close_orchestrator --token <token>
scripts/ops/panji-admission verify-own --scope after_close_orchestrator --token <token>
```

- `acquire`：获取**本次** pause 所有权，输出 `{"acquired":true,"token":...}`。
  若已被他人/先前 pause 持有（不同 token），退出码非 0 —— 调用方**不得借用**，应停止并人工介入。
- `status`：读取 `installed / paused / pause_token / paused_by / reason / paused_at`。
- `release`：仅当 `--token` 匹配时释放 own pause；否则退出码非 0（不得解除他人 pause）。
- `verify-own`：部署 secondary gate 使用，校验 own pause 仍有效（paused 且 token 匹配）。

## 1. 部署临界区（由 panji-deploy.sh 自动执行，无需手动）

部署流程已内建：

1. 首个 after-close 任务门禁（`guard_active_after_close_jobs`）通过后，`acquire` own pause。
2. 紧邻第一笔实际 runtime mutation（`update_env_file`，文件层写入）之前执行
   **secondary pre-mutation gate**：`verify-own` 确认 own pause 仍有效且 `running=0`。
3. 任一失败 → 部署 fail-closed 停止（不 kill / 不 reset 业务任务）。
4. 部署完整成功 **或** 验证过的回滚成功后，`release` 本次 own pause。
5. 回滚验证失败 → 保持 paused（打印 `MANUAL_INTERVENTION_REQUIRED`），不释放。

## 2. 手动暂停 / 恢复（运维 / 紧急）

```bash
# 暂停 after-close pickup（operator 自行持有 token）
TOKEN=$(scripts/ops/panji-admission acquire --scope after_close_orchestrator \
        --actor "operator:$(whoami)" --reason "manual hold" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
echo "PAUSE TOKEN = ${TOKEN}"   # 务必记录，release 时要用

# 查看状态
scripts/ops/panji-admission status --scope after_close_orchestrator

# 恢复（必须用自己的 token）
scripts/ops/panji-admission release --scope after_close_orchestrator --token "${TOKEN}"
```

**所有权铁律**：只有持有自己 token 的 owner 才能 release。检测到 `paused_by` 是别人时，
不要尝试 release，联系该 owner 或走 MANUAL_INTERVENTION。

## 3. First-deploy 兼容（legacy worker 隔离）

新机制首次部署到 production 时，旧 worker 不读取 `worker_pickup_admission` 表。隔离靠
既有 worker lifecycle + 部署自身门禁，不复建 bootstrap subsystem：

1. **部署自身门禁保证无强杀**：`guard_active_after_close_jobs`（active-job gate）要求
   `running == 0 && queued == 0 && resume_queued == 0` 才允许任何 runtime mutation；
   否则部署在 mutation 之前 fail-closed 停止。**部署工具不会 kill/cancel/reset 正在推进的业务任务**，
   因此 progressing job 不会被部署强杀。
2. **旧 worker 停止接新 pickup**：`docker compose stop worker-after-close` 发送 SIGTERM，
   `worker.py::_after_close_poll_once` 在领取新任务前检查 `_shutdown`，收到 SIGTERM 后不再
   领取新 item，并 drain 当前 item 到 terminal（应用层不裸取消 progressing job）。
3. **running job 自然完成**：由于步骤 1 的部署门禁在 `running > 0` 时拒绝推进，部署会等待
   running 自然降到 0；旧 worker 的 drain 把当前 item 跑完。应用层 drain 不会强杀 progressing job。
4. **残留风险（如实声明）**：若单个 item 超过 Docker `stop_grace_period`，容器会被 SIGKILL，
   遗留 naked orphan running job；此时部署门禁因 `running > 0` 持续阻塞直到人工介入，
   **不会**由部署去强杀它。这正是既有硬规则"progressing job 不得为部署被 timeout 强杀"的体现。
5. **admission 表创建后再 acquire**：migration 093 创建 singleton row
   （`after_close_orchestrator`，active）；部署在 migration 之后才 acquire own pause，
   使随后启动的 admission-aware worker 在部署临界区内不领取新任务（首次部署队列本就为空，无 claim 可发生）。
6. admission-aware worker 启动后同事务读该行：active → 可 claim；paused → 不 claim；
   缺行 / 查询错误 → **FAIL CLOSED（不 claim）**。
7. 确认 `OLD_AFTER_CLOSE_PICKUP_DISABLED=TRUE` 且 `RUNNING_CONFLICT_COUNT=0` 后，
   才进入 admission-aware runtime。

## 4. 故障

- **acquire 失败（ADMISSION_ACQUIRE_FAILED）**：DB 不可用或已有他人 pause。停止部署，
  排查 DB 或联系现有 pause owner；不要借用别人的 pause 继续部署。
- **secondary gate 失败（ADMISSION_SECONDARY_GATE_FAILED）**：own pause 丢失或被抢占，
  或 running 不为 0。部署已 fail-closed 停止；检查 worker 是否仍在 claim、running 任务状态。
- **回滚验证失败**：保持 paused，打印 `MANUAL_INTERVENTION_REQUIRED`；人工确认后，
  持自己 token 的 owner 才能 release。
- **release 失败（ADMISSION_RELEASE_FAILED）**：**DEGRADED，非静默成功**。运行健康时不强制
  回滚健康候选，但 admission lifecycle 不完整：`MANUAL_INTERVENTION_REQUIRED=TRUE`，
  部署 workflow 不得报告完全 CLOSED。持自己 token 的 owner 必须手动 release 后再确认
  `OLD_AFTER_CLOSE_PICKUP_DISABLED=FALSE`。
