# Tasks

## 任务 1：主页布局修正与 N+1 查询消除

- [x] 1.1 主页 `split-2` 改为 `split-even`，子卡片加 `min-width: 0`
- [x] 1.2 删除第 4 张"通知渠道"KPI 及相关 `useNotificationChannels` / `channelsQuery` / `feishuChannel` / `kpi4Status` / `kpi4Time` 代码
- [x] 1.3 KPI 改为三项：最新选股结果 / 监控自选股 / 今日策略事件
- [x] 1.4 删除 `useQueries` + `getInstrumentById` N+1 循环，直接使用 `r.instrument_name` / `r.instrument_symbol` / `r.instrument_market`
- [x] 1.5 `global.scss` 新增 `.index-main-panel { min-width: 0; }`
- [x] 1.6 验证：`npx tsc --noEmit` 无错误，首页渲染两列等宽、3 张 KPI

## 任务 2：删除虚假"用户通知规则"卡片

- [x] 2.1 删除设置页整张"用户通知规则"卡（`cooldown` / `quietStart` / `quietEnd` / `pauseOnDelay`）
- [x] 2.2 删除相关本地 state 与 `pointerEvents: none` 逻辑
- [x] 2.3 保留"我的通知渠道"和飞书实测功能不变
- [x] 2.4 添加占位说明"用户级通知规则将在后续版本支持"
- [x] 2.5 验证：`npx tsc --noEmit` 无错误，设置页不再显示通知规则卡

## 任务 3：统一时区处理

- [x] 3.1 后端新增 `backend/app/core/time.py`：`now_utc()` / `now_shanghai()` / `shanghai_business_date()` / `to_shanghai_iso()`
- [x] 3.2 前端新增 `frontend/src/utils/datetime.ts`：`formatShanghaiTime()` / `formatShanghaiDate()` / `shanghaiBusinessDate()`
- [x] 3.3 全局搜索并替换 `new Date().toISOString().slice(0, 10)` 为 `shanghaiBusinessDate()`
- [x] 3.4 全局搜索并替换未指定 `timeZone` 的 `toLocaleTimeString()` / `toLocaleString()`
- [x] 3.5 确认 Docker compose 中所有服务已设置 `TZ: Asia/Shanghai`
- [x] 3.6 后端模块自测：`python -m app.core.time`
- [x] 3.7 验证：`pytest tests/ -q` 0 failed，`npx tsc --noEmit` 无错误

## 任务 4：交易时段判断修正

- [x] 4.1 确认后端 `/market/status` 端点存在且使用上海时区
- [x] 4.2 前端 `isInTradingHours()` 改为优先调用后端 `/market/status`
- [x] 4.3 前端 fallback 使用 `Intl.DateTimeFormat` 固定 `timeZone: 'Asia/Shanghai'`
- [x] 4.4 验证：在不同浏览器时区下交易时段判断一致

## 任务 5：任务页 live 与管理员增强

- [x] 5.1 `useSchedulerJobRuns` 增加 `refetchInterval: 10_000`，`refetchIntervalInBackground: false`
- [x] 5.2 管理员系统概览增加 15s 轮询
- [x] 5.3 任务页增加显示：最后心跳、租约到期时间、当前子任务/StrategyRun ID、最新处理 Bar 时间
- [x] 5.4 盘中监控"绿色"判定逻辑：worker-monitor 心跳 < 90s + session=running + source_bar_time ≤ 120s
- [x] 5.5 盘后"完成"判定逻辑：bars_scheduler succeeded + DSA published + failed_count=0
- [x] 5.6 验证：`npx tsc --noEmit` 无错误，任务页 10s 自动刷新

## 任务 6：截图缓存

- [x] 6.1 `stock_capture_service` 按 `event_id + instrument_id + chart_version` 缓存 PNG，TTL 5-15 分钟
- [x] 6.2 文本 delivery 重试不触发新截图
- [x] 6.3 图片 delivery 重试优先复用缓存
- [x] 6.4 验证：`pytest tests/ -q` 0 failed

## 任务 7：文档体系

- [x] 7.1 新增 `docs/产品与业务规则.md`、`docs/系统架构.md`、`docs/策略与指标口径.md`、`docs/API与事件契约.md`、`docs/定时任务与运行手册.md`、`docs/部署与回滚.md`、`docs/开发与测试.md`、`docs/运维排障.md`、`docs/安全规范.md`
- [x] 7.2 每份文档顶部记录：最后验证 Commit、负责人、事实来源、自动/人工维护
- [x] 7.3 `tools/update_docs.py` 增加 `--check` 模式
- [x] 7.4 运行 `python tools/update_docs.py` 更新自动生成文档

## 任务 8：测试账号创建

- [x] 8.1 创建专用普通用户账号（不启用 MFA）
- [x] 8.2 创建专用管理员账号（不启用 MFA）
- [x] 8.3 确认测试环境可从公网访问
- [x] 8.4 整理测试环境 URL、账号、密码、预期数据说明

## 任务 9：端到端验证

- [x] 9.1 后端 `pytest tests/ -q` 315 passed, 0 failed, 0 error
- [x] 9.2 前端 `npx tsc --noEmit` + `npm run build` + `npm run lint` 无错误（0 errors, 14 warnings 既有）
- [x] 9.3 首页等宽两列 + 3 KPI + 无 N+1 查询（代码已删除 useQueries 循环）
- [x] 9.4 设置页无"用户通知规则"卡（已删除整张卡片）
- [x] 9.5 时间显示固定上海时区（formatShanghaiTime 全仓库替换）
- [x] 9.6 任务页 10s 自动刷新（refetchInterval: 10_000）

## 任务 10：部署、重启与推送

- [x] 10.1 已重建 backend / frontend / worker-capture 镜像并重启全部服务
- [x] 10.2 使用外部 env 文件重启相关服务
- [x] 10.3 `/api/health/ready` 返回 `{"status":"ready"}`，`/api/version` git_sha=63dcc594
- [x] 10.4 commit 并 push 到 `origin/main`（80c4a1c）
- [x] 10.5 测试账号信息已提供给用户

# Task Dependencies

- 任务 4 依赖 任务 3（时区模块）
- 任务 6 依赖 已有的 delivery 链路（已完成）
- 任务 8 可与 任务 1-7 并行
- 任务 9 依赖 任务 1-8
- 任务 10 依赖 任务 9
- 任务 1、2、3、5、7 可并行
