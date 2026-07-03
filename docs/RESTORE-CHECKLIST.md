# Restore Checklist：换机器/新对话后如何确认已恢复项目上下文

> 目的：判断一个新 agent 是否已经理解当前项目，而不是只读了 README。

## 1. Git 与运行基线

- [ ] 能说出当前 main 基线：`40dd2287f0962910d2e272c468b3e5054abddaaf`；
- [ ] 能说出 PR #3 做了什么：治理基线修复、docs consistency 真校验、outbox target_channel_id 测试；
- [ ] 能区分当前 docs 包生成基线和旧 current docs 的实现核对基线 `ddca659b8c9d64b6a414da0b4bbd6f80f704aef1`；
- [ ] 能说明本 docs v2 还未应用到仓库，需要 Draft PR。

## 2. 产品理解

- [ ] 能说出产品主线：发现机会 → 验证机会 → 持续追踪；
- [ ] 能说出不做自动交易、不接券商、不承诺收益；
- [ ] 能区分访客、有效会员、到期会员、管理员、Worker；
- [ ] 能说出当前不支持多策略组合。

## 3. 权限与订阅

- [ ] 能说明 active member + active subscription 才有核心功能资格；
- [ ] 能说明到期用户可登录、续期、历史消息只读，但不能新建监控和通知；
- [ ] 能说明管理员不需要 Subscription 但受 Admin API 限制；
- [ ] 能说明 Capture Token 与普通 Access Token 隔离。

## 4. 核心链路

- [ ] 能画出盘后行情 → StrategyRun → DSA → published_run_id；
- [ ] 能画出 watchlist → monitor → StrategyEvent → Outbox → Delivery → Feishu；
- [ ] 能画出手动个股详情分享 → target_channel_id → Outbox → Delivery；
- [ ] 能画出 Capture Worker → /capture/stock/:symbol → image message。

## 5. 代码地图

- [ ] 能定位 FastAPI 入口 `backend/app/main.py`；
- [ ] 能定位统一 Worker 入口 `backend/app/worker.py`；
- [ ] 能定位前端路由 `frontend/src/App.tsx`；
- [ ] 能定位 compose 编排 `docker-compose.prod.yml`；
- [ ] 能按模块找到 API/Service/Repository/Model/测试。

## 6. 剩余风险

- [ ] 能说出 ALIGN-010：飞书真实 E2E/partial_failed/仅重试图片仍需生产验证；
- [ ] 能说出 ALIGN-012：Admin 页面生产 E2E 未完全验证；
- [ ] 能说出 ALIGN-015：CORE_ONLY/服务健康与业务能力匹配需验证；
- [ ] 能说出生产审计发现 worker_heartbeats 僵尸 running 记录；
- [ ] 能说出 Ruff/Mypy 全仓历史债务是非阻断展示，不应混入业务 PR。

## 7. 任务准备

如果新 agent 无法完成以上 80% 检查，不允许它直接改代码。
