# 自动部署实施计划

## Phase 1：文档和现状审计

- 完成 CN 当前 WIP 收尾；
- 核对 main/dev 和当前 runtime SHA；
- 检查现有 Actions；
- 审查 `deploy_live_runtime.sh`；
- 核对 Compose 服务和健康检查。

## Phase 2：服务器目录

- 克隆 `/opt/panji-deploy`；
- 初始化 `/opt/panji-live` 合同；
- 保留 `/root/web_dev` 为开发目录；
- 禁止部署脚本使用 `/root/web_dev`。

## Phase 3：部署用户和 Key

- 创建 `panji-deploy` 用户；
- 添加 forced-command SSH key；
- 禁止 shell、PTY、转发；
- 配置最小 sudo 入口；
- Key 不可读取 market.env。

## Phase 4：固定脚本

- gateway 只接收 SHA；
- 验证 SHA 属于 origin/dev；
- 获取 flock；
- 分类变更；
- migration/high-risk 阻塞；
- 调用已有 live deploy；
- 验证 runtime SHA；
- 失败恢复 previous SHA。

## Phase 5：GitHub Actions

- push dev trigger；
- quick CI；
- SSH fixed command；
- concurrency；
- secrets；
- workflow 修改单独审查。

## Phase 6：测试

1. docs-only；
2. frontend-only；
3. backend-only；
4. combined；
5. migration blocked；
6. invalid SHA；
7. concurrent push；
8. failed health rollback；
9. CN manual rerun；
10. `/root/web_dev` dirty 不影响自动部署。

## Phase 7：收口

- CHANGE；
- rules/maps；
- evidence；
- main 阶段稳定点。
