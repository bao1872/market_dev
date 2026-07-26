# 00 核心治理

## 事实源

冲突时优先级：

1. 用户当前明确要求；
2. 当前任务指定 commit 的代码；
3. `rules/`；
4. `maps/MANIFEST.md`；
5. `maps/current/`；
6. `maps/code/`；
7. 最新 CHANGE；
8. 测试和运行 evidence；
9. `maps/work/`；
10. archive 和旧聊天。

## 当前阶段原则

- 优先短反馈环路；
- 自动化默认路径，人工操作作为补充；
- 不为尚未存在的多人生产团队引入无必要流程；
- 但数据库、migration、volume 和秘密边界不能放松。

## 单一代码源

- Work 和 CN 都必须把正式修改提交 GitHub；
- `/root/web_dev` 的临时修复必须最终 commit + push；
- `/opt/panji-deploy` 和 `/opt/panji-live` 不产生业务代码；
- 自动部署只部署 GitHub commit。

## 分层

- API：认证、参数、响应；
- Service：业务状态、事务、资格、幂等；
- Repository：数据访问；
- Kernel：计算；
- Adapter：外部系统；
- 前端：ViewModel 与展示，不重算后端业务。

## 时间和因果

- 业务时间 Asia/Shanghai；
- 历史和盘后必须 point-in-time；
- causal、confirmed_delay、hindsight、label 严格分离。
