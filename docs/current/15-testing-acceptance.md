> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 15 测试与验收设计

## 1. 测试数据库

所有数据库集成测试只使用 PostgreSQL 测试库和真实 Alembic。禁止 SQLite、aiosqlite、内存数据库、测试手写生产 Schema和模块级 db_session 覆盖。

## 2. 测试层级

- Unit：纯函数、算法和状态转换，不连数据库；
- Integration：PostgreSQL、ORM、Service、事务、锁和 Worker；
- API：认证、资格、所有权、响应和错误；
- Frontend：Adapter、路由、状态和交互；
- E2E：用户操作到数据库、消息、飞书和截图；
- Deployment：Compose、迁移、健康、版本和 Worker。

## 3. 关键回归

### 趋势选股

- total/computable universe 口径正确；
- 不按方向、强弱或 matched 过滤；
- 每个 computable 股票有结果；
- failed 与 skipped 分开；
- `partial_failed` 不能发布；
- result_count=succeeded_count；
- 无筛选时 source_total=result_count；
- 分页不改变全量结果。

### 权限和订阅

覆盖 active、expired、no-subscription、disabled、admin 和用户 A/B。到期用户趋势、自选和个股接口 403；账户、套餐、续期和历史消息只读可访问；续期后原数据恢复。

### 盘中监控

四类用户加入同一股票，只有有效会员进入 instrument_user_map；只处理完成 1m Bar；同一策略、股票和 Bar 评估一次；投递前再次检查资格。

### 行情聚合

- DB 有历史但尾部缺失时补齐；
- 盘中返回 partial Bar；
- 盘后不重复拉实时；
- 外部源失败返回 degraded；
- 页面、指标和截图的 source_bar_times/hash/as_of 一致。

### 飞书

- 文字和图片都成功；
- 截图失败形成 partial_failed；
- 图片上传/发送失败可仅重试图片；
- 重试不重复文字；
- 用户只能查询自己的状态。

### 管理和任务

- 管理按钮调用真实 API；
- 审计日志完整；
- run key、heartbeat、lease、stale recovery；
- Admin Jobs 显示真实任务；
- Worker Git SHA 一致。

## 4. 缺陷和 xfail

每个缺陷修复必须先有失败测试。禁止删除测试、降低断言或无条件 skip。xfail 必须登记真实 issue、owner 和 expires，XPASS 或过期项阻断 CI。

## 5. 文档一致性

执行：

```bash
python tools/update_docs.py --check
python tools/check_architecture.py
python tools/check_docs_consistency.py
python tools/check_test_allowlist.py
```

并确认每次修改有相关 current 文档、CHANGE、CHANGELOG 和 Alignment 更新。

## 6. 完成门禁

- PostgreSQL 全量 pytest 0 failed/0 error；
- Alembic upgrade/downgrade/upgrade；
- 前端 TypeScript、lint、build、contract tests；
- Docker Compose config；
- **Ruff New Files（阻断）**：相对 base 新增的 Python 文件必须零错误；
- **Ruff Baseline Regression（阻断）**：当前全仓库诊断集合相对 `tools/quality_baselines/ruff.json` 基线不得新增问题、不得增加数量；
- **Ruff Full Repository Report（非阻断）**：执行全仓库扫描并上传 JSON 报告 artifact，仅展示剩余历史债务；
- mypy `app` 阻断；
- GitHub Actions 针对最终 HEAD 全部 blocking jobs success；
- 没有未登记的代码—文档冲突。
