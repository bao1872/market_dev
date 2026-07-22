# 生产验收证据

本目录存放生产验收证据，必须绑定到最终 merge SHA 和镜像 SHA。

PRD V2.0 §7.1 / §7.3 规则 8 定义。最后更新：CP-19。

## 验收记录列表

_待部署后填充。当前分支 `fix/production-pipeline-stability-v1` 完成本地验收后，需用户批准部署，部署后在此目录记录生产验收证据。_

## 验收记录模板

```markdown
# 生产验收记录: <YYYY-MM-DD> <分支名>

- **部署日期**: YYYY-MM-DD HH:MM (Asia/Shanghai)
- **分支**: <branch-name>
- **Merge SHA**: <40-char SHA>
- **镜像 SHA (12 位)**: <SHA-tag>
- **镜像 digest**: sha256:<...>
- **部署执行人**: <name>
- **批准消息**: `批准分支部署，完成真实生产验收；merge前仍需停止。`

## 部署范围

| 服务 | 旧 SHA | 新 SHA | 镜像标签 |
|------|--------|--------|----------|
| backend | <old> | <new> | market-dev-backend:<new-tag> |
| frontend | <old> | <new> | market-dev-frontend:<new-tag> |
| worker | <old> | <new> | market-dev-worker:<new-tag> |

## Migration

- 本次新增 migration: `<revision>_<description>.py`
- upgrade 验证: ✅ / ❌
- downgrade 验证（如执行回滚测试）: ✅ / ❌ / N/A

## 关键功能验收

### 1. 后端 API
- `GET /api/v1/health`: <status>
- `GET /api/v1/instruments/<id>/chart-snapshot`: <status>（Atomic Snapshot 单 MDAS 验证）
- `GET /api/v1/stocks/<symbol>/context`: <status>（nodeAvailability 5 态验证）
- `POST /api/v1/stock-detail-feishu`: <status>（三类 indicator_view 验证）

### 2. 前端
- `/market` 列表加载: ✅ / ❌
- `/stock/:symbol` 详情页 K 线渲染: ✅ / ❌
- 五周期切换（1d/15m/1h/1w/1mo）无 mismatch: ✅ / ❌
- Capture 页面（`/capture/stock/:symbol`）三种 indicator_view 渲染: ✅ / ❌

### 3. 飞书投递
- 手动分享（`POST /api/v1/stock-detail-feishu`）: ✅ / ❌
- 盘后自动投递（`after_close` 编排）: ✅ / ❌ / 未到盘后时间
- 三类 indicator_view 图片独立投递: ✅ / ❌

### 4. 盘后任务
- after_close 编排全链路: ✅ / ❌ / 未到盘后时间
- DSA published run: ✅ / ❌
- snapshot 发布: ✅ / ❌
- factor 重建（如有公司行为）: ✅ / ❌ / N/A

## 性能与资源

- 后端 API p99 延迟: <ms>
- 前端首屏加载时间: <ms>
- 内存占用（peak）: backend <MB> / frontend <MB> / worker <MB>
- 磁盘占用: <GB>

## 回滚验证（如执行）

- 回滚到 SHA: <rollback-SHA>
- 回滚原因: <reason>
- 回滚后服务状态: ✅ / ❌
- Migration downgrade: ✅ / ❌ / N/A

## 遗留问题

- <问题描述与跟踪 issue>

## 关联

- 《待部署报告 V3》
- ADR-0001 / ADR-0002（如适用）
- CHANGE-YYYYMMDD-NNN（如适用）
```

## 规则

- **禁止绑定到中间 commit**：必须绑定最终 merge SHA（部署后从 `git rev-parse HEAD` 获取）
- **必须包含镜像 SHA**：从 `docker images` 获取，便于回滚定位
- **必须包含批准消息**：用户明确回复 `批准分支部署，完成真实生产验收；merge前仍需停止。`
- **部署门禁**：未收到明确批准消息不得部署（AGENTS §九）
- **验收证据必须真实**：禁止伪造验收结果，所有 ✅ 必须有对应日志/截图/用户反馈支撑
- **失败必须记录**：任何 ❌ 必须记录失败原因、影响范围、修复方案

## 状态

当前分支 `fix/production-pipeline-stability-v1` HEAD：`<待部署后填充>`
待用户批准部署后，按上述模板创建验收记录文件 `evidence-<YYYY-MM-DD>-<branch-name>.md`。
