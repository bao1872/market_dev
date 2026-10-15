# 历史只读策略资产发布（PANJI-STRATEGY-ASSET-PUBLISH-001）

本 Runbook 只描述一件事：把**已经通过 C 阶段校验的固定历史只读 JSON 资产**发布到生产主机
固定目录 `/opt/panji-strategy-data/release/<asof>_<releaseid16>/`。

它不是通用文件上传工具，也不是部署入口。范围外的操作必须另行授权。

## 1. 唯一入口与唯一实现

| 角色 | 路径 | 运行位置 |
|---|---|---|
| 本地唯一用户入口 | `scripts/ops/panji-strategy-history-publish <FULL_SHA> [--dry-run]` | 本地 |
| 服务器端唯一实现 | `scripts/deploy/panji-strategy-history-publish.sh <FULL_SHA> [--dry-run]` | `panji-prod` |
| SSH 入口 | `scripts/ops/panji-prod-ssh`（唯一，禁止第二通道） | 本地 |
| 安全测试 | `scripts/deploy/panji-strategy-history-publish.test.sh` | 本地 |

## 2. 传输通道：Git 版本化资产

资产随代码一起受版本控制，位于：

```text
deploy/assets/strategy-history/2026-08-17_89a5be0b28d1dac9/
├── asset_index.json            # 校验索引（唯一校验真源：SHA256 / 身份 / 状态 / 大小）
├── candidate_snapshot.json
├── candidate_shard_0000.json
└── evaluation_snapshot.json
```

本地入口先校验目标 SHA 的 Git 对象中确实存在这 4 个文件与服务器端实现脚本，再让服务器
`fetch → checkout --detach <SHA>`，从**受版本控制的工作树**读取资产。

不使用 `scp`、不使用 `docker cp`、不使用 stdin 注入脚本、不使用临时 SSH 命令。

固定批次只允许上述 4 个文件；禁止 `.pkl` 模型权重、数据库文件、密钥或任何未登记文件。
已登记资产总大小约 795 KiB（`asset_index.json` 记录每个文件的 `size_bytes` 与归档
`bundle_sha256`；Runner 另设单文件 32 MiB / 总计 64 MiB 上限，超出立即停止）。

## 3. 明确不做（越权即失败）

- 不写数据库，不改 `AUDITED` / `PUBLISHED` / `STAGING` 原始状态；
- 不创建 `active_release.json`，不激活模型，不改现役模型指针；
- 不删除既有生产资产，失败只清理本次 Runner 创建的暂存目录；
- 不重启容器、不触发部署（部署由 `scripts/ops/panji-test-deploy` 独立执行）；
- 发布完成后**始终恢复服务器原始 REF**，避免后续部署把本 SHA 误判为"上一真实运行 SHA"。

## 4. 前置条件

1. 当前分支为 `dev`，工作树只含本次任务文件；
2. 目标 SHA 已 push 且是 `origin/dev` 祖先；
3. 本地通过：`bash scripts/deploy/panji-strategy-history-publish.test.sh`；
4. 生产主机 `/opt/panji-strategy-data/release/<leaf>` 不存在，或存在但内容完全一致。

## 5. 操作步骤

```bash
# 1) 本地安全测试（必须先过）
bash scripts/deploy/panji-strategy-history-publish.test.sh

# 2) dry-run：只校验与输出计划，零资产写入
scripts/ops/panji-strategy-history-publish <FULL_SHA> --dry-run

# 3) 正式发布（仅 dry-run 通过后）
scripts/ops/panji-strategy-history-publish <FULL_SHA>

# 4) 发布后只读核验（SHA 必须与 asset_index.json 完全一致）
scripts/ops/panji-prod-ssh 'cd /opt/panji-strategy-data/release/2026-08-17_89a5be0b28d1dac9 \
  && sha256sum candidate_snapshot.json candidate_shard_0000.json evaluation_snapshot.json; \
  ls -la /opt/panji-strategy-data; \
  test -e /opt/panji-strategy-data/active_release.json && echo POINTER_EXISTS || echo NO_ACTIVE_POINTER'
```

期望 dry-run 输出 `PLAN_ONLY`；正式发布输出 `PUBLISHED_OR_IDEMPOTENT`，且
`model_activated=false`、`active_release_pointer_written=false`。

## 6. 目标目录与权限

```text
/opt/panji-strategy-data/
└── release/
    └── 2026-08-17_89a5be0b28d1dac9/      # 0555
        ├── candidate_snapshot.json       # 0444
        ├── candidate_shard_0000.json     # 0444
        └── evaluation_snapshot.json      # 0444
```

暂存目录 `/opt/panji-strategy-data/.staging/panji-history-publish-<pid>-<ts>/` 在成功或失败后
都会被清理，不应有残留。该目录已被 `docker-compose.prod.yml` / `docker-compose.live.yml`
以 `:ro` 只读挂载，无需修改 Compose。

## 7. 失败处理

| 错误码 | 含义 | 处理 |
|---|---|---|
| `BAD_SHA` / `BAD_OPTION` | 参数错误 | 使用 `origin/dev` 40 位完整 SHA 重试 |
| `WORKTREE_SHA_MISMATCH` | 服务器工作树与目标 SHA 不一致 | 检查自举流程，不得手工改工作树 |
| `ASSET_SRC_MISSING` / `MISSING_ASSET_FILE` | 目标 SHA 未登记资产 | 确认资产已提交并 push |
| `EXTRA_ASSET_FILE` / `NON_REGULAR_ASSET_FILE` | 源目录出现未登记文件或软链接 | 停止，核对资产来源 |
| `INDEX_EXTRA_KEYS` | 索引键集合被注入/穿越 | 停止，核对索引来源 |
| `SHA_MISMATCH` / `SIZE_MISMATCH` | 资产与索引登记不一致 | 停止，不得改索引迁就文件 |
| `SOURCE_STATUS_INVALID` / `EVALUATION_STATUS_INVALID` | 审核状态非 AUDITED | 停止，不得伪造状态 |
| `STOCK_COUNT_MISMATCH` / `CANDIDATE_COUNT_MISMATCH` / `BATCH_DAYS_MISMATCH` | 资产身份不符 | 停止 |
| `ASSET_CONFLICT_REFUSE_OVERWRITE` | 目标已存在且内容存在差异 | **停止**，人工核对，不得覆盖 |
| `FILE_TOO_LARGE` / `TOTAL_TOO_LARGE` | 超出大小上限 | 停止 |

任何失败都不会留下半写入的生产资产：暂存目录被清理，既有资产保持原样。

## 8. 与部署的关系

资产发布只放文件，不部署代码。资产发布成功后仍需按
`docs/runbooks/development-deployment.md` 执行：

```bash
scripts/ops/panji-test-deploy <FULL_SHA> --dry-run
scripts/ops/panji-test-deploy <FULL_SHA>
```

历史快照成功部署**不等于**模型获得发布资格。模型管理保持只读，自动训练与自动发布保持关闭。
