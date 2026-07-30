# CHANGE-20260730-015：SSH 目标漂移防复发治理

状态：生效（代码已提交；scripts/ops/panji-prod-ssh + scripts/ops/panji-prod-preflight 已落库；preflight 已实际验证通过）
日期：2026-07-30
类型：architecture + governance
领域：部署运维 / TRAE 工作协议 / 生产安全

## 1. 背景

2026-07-30 本轮 P0 闭环执行中，发生 `CompactFake` 上下文压缩后：

- 前半段已多次通过 `ssh panji-prod` 成功连接生产服务器（hostname=VM-0-5-ubuntu，43.136.118.82）；
- 压缩后 TRAE 重新猜测 SSH 别名：先尝试 `root@panji-server`（不存在），再读取 `~/.ssh/config` 选用旧别名 `55-server`（解析到 120.234.137.109:5622，非盘迹生产）；
- SSH 失败后 TRAE 错误得出"生产服务器不可达"结论，停止部署、Review、行情修复和页面验收；
- 实际上 `panji-prod` 一直可用，本轮未验证其失败。

## 2. 根因

1. AGENTS.md 之前只有自然语言说明，没有可执行连接约束；
2. 没有禁止使用其他 SSH 别名；
3. 没有要求连接前校验 `ssh -G` 解析出的 HostName/User/Port；
4. 没有仓库内统一 SSH 包装脚本；
5. Compact 后子代理重新读局部文件或 `~/.ssh/config`，可能选择旧别名；
6. 连接测试用了 `2>&1 | head`：未启用 `pipefail` 时即使 SSH 失败，整个管道也可能因为 `head` 成功而返回状态 0，错误命令被误判为 success；
7. SSH 失败后又重复执行两次同类连接测试，没有回到已成功的 `panji-prod`。

## 3. 修改内容

### 3.1 新增仓库脚本

- `scripts/ops/panji-prod-ssh`：唯一允许的盘迹生产 SSH 入口，固定 `ssh panji-prod`，禁止 `--host` 等覆盖参数，不输出私钥；
- `scripts/ops/panji-prod-preflight`：三阶段校验
  - 阶段 1：解析 `ssh -G panji-prod` 与权威参数逐项比对（HostName=43.136.118.82、User=root、Port=22）；
  - 阶段 2：实际 SSH 连接，校验 hostname、`/root/web_dev`、`/etc/market-dev/market.env`、Compose 项目 `web_dev`、`trading-backend` 容器；
  - 阶段 3：捕获 repo HEAD、alembic、磁盘、MemAvailable、容器列表；
  - 任一阶段失败 exit 1，不得继续；不使用可能掩盖退出码的管道（先存变量再单独裁剪）。

### 3.2 更新 `~/.ssh/config`（不在仓库内）

- 备份为 `~/.ssh/config.panji-backup-20260730-113821`；
- 在 `55-server` 块上方加 `# DEPRECATED-PANJI-DO-NOT-USE` 注释（指向 120.234.137.109，非盘迹生产）；
- 保留 `55-server` 别名用于历史运维，但盘迹操作禁止使用；
- `panji-server` 不在 `~/.ssh/config`（默认 fallback，无需修改）；
- 不删除或修改其他无关 Host 配置；
- `chmod 600 ~/.ssh/config`。

### 3.3 规则更新

- `rules/80-deployment-data-safety.md` 新增 "生产服务器 SSH SSOT" 章节：唯一入口、权威参数、preflight 要求、禁止行为；
- `rules/70-trae-cn.md` Compact 恢复规则补充：Compact/子代理恢复后必须读取账本 + `docs/maps/80-system-runtime.md` §2 + 运行 preflight，禁止猜测 SSH 别名；
- `AGENTS.md` §8 基础安全边界新增一条：禁止使用 `panji-server`/`55-server`/原始 IP 或任何非 `panji-prod` 别名访问生产。

## 4. 关键差异

| 项 | 修改前 | 修改后 |
|---|---|---|
| 生产 SSH 入口 | 任意别名 / IP / `~/.ssh/config` 选择 | 仅 `scripts/ops/panji-prod-ssh`（固定 `panji-prod`） |
| 部署前校验 | 无统一脚本 | 必须运行 `scripts/ops/panji-prod-preflight`，三阶段通过 |
| SSH 退出码 | 可能被 `\| head` 掩盖 | 先 `OUTPUT=$(ssh ...); RC=$?` 再单独裁剪 |
| Compact 恢复 | 重新发现服务器入口 | 必须读账本 + preflight，禁止猜测别名 |
| `55-server` | 无标记 | 加 `DEPRECATED-PANJI-DO-NOT-USE` 注释 |

## 5. 影响范围

- **行为影响**：未来 TRAE 或开发者访问生产必须通过 `scripts/ops/panji-prod-ssh`，部署前必须 preflight；
- **契约影响**：无 API/数据契约变化；
- **结构影响**：新增 `scripts/ops/` 目录；
- **运行方式**：部署工作流入口收紧，但不改变部署脚本本身（`scripts/deploy/panji-deploy.sh`）。

## 6. 验证

- `scripts/ops/panji-prod-preflight` 实际运行通过：
  - 阶段 1：`ssh -G panji-prod` 解析 host=43.136.118.82、user=root、port=22 ✓
  - 阶段 2：远程 hostname=VM-0-5-ubuntu，`/root/web_dev` OK，`/etc/market-dev/market.env` OK，Compose 项目 web_dev running(15)，`trading-backend` 容器 OK ✓
  - 阶段 3：repo HEAD=9aea736、alembic=076_market_review_workbench、disk 30G、MemAvailable 4.79GB、15 容器运行 ✓
- 未运行任何破坏性操作；
- `~/.ssh/config` 备份与权限确认。

## 7. 相关

- 关联 PRD：无（运维治理，非产品行为）
- 关联 Maps：`docs/maps/80-system-runtime.md` §2（权威参数源）
- 关联 Rules：`rules/80-deployment-data-safety.md`、`rules/70-trae-cn.md`、`AGENTS.md` §8
- 关联提交：本轮新增 1 个 SSH 治理提交（不改写 54fe3a2/c53f772 历史）
- 关联事故：2026-07-30 P0 闭环因 SSH 目标漂移误判停止（已恢复，preflight 验证 panji-prod 可用）
