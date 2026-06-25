# Checklist

## 主页布局与 N+1 修复

- [ ] 主页使用 `split-even` 两列等宽
- [ ] 子卡片有 `min-width: 0` 防撑宽
- [ ] KPI 只有三项：最新选股结果 / 监控自选股 / 今日策略事件
- [ ] 已删除 `useNotificationChannels` / `channelsQuery` / `feishuChannel` / `kpi4Status` / `kpi4Time`
- [ ] 已删除 `useQueries` + `getInstrumentById` N+1 循环
- [ ] 首页选股结果直接使用 `r.instrument_name` / `r.instrument_symbol` / `r.instrument_market`
- [ ] 首页 10 条结果 Network 请求从 11 次降为 1 次

## 虚假通知规则卡片清理

- [ ] 设置页已删除"用户通知规则"整张卡片
- [ ] 已删除 `cooldown` / `quietStart` / `quietEnd` / `pauseOnDelay` 本地 state
- [ ] 已删除 `pointerEvents: none` 逻辑
- [ ] "我的通知渠道"和飞书实测功能保留不变
- [ ] 添加了"用户级通知规则将在后续版本支持"占位说明

## 时区统一

- [ ] 后端 `app/core/time.py` 提供 `now_utc()` / `now_shanghai()` / `shanghai_business_date()` / `to_shanghai_iso()`
- [ ] 前端 `utils/datetime.ts` 提供 `formatShanghaiTime()` / `formatShanghaiDate()` / `shanghaiBusinessDate()`
- [ ] 全仓库无 `new Date().toISOString().slice(0, 10)` 用于 A 股业务日期
- [ ] 全仓库无未指定 `timeZone` 的 `toLocaleTimeString()` / `toLocaleString()`
- [ ] Docker compose 所有服务已设置 `TZ: Asia/Shanghai`
- [ ] 后端模块自测通过

## 交易时段判断

- [ ] `isInTradingHours()` 优先调用后端 `/market/status`
- [ ] Fallback 使用 `Intl.DateTimeFormat` 固定 `timeZone: 'Asia/Shanghai'`
- [ ] 不再依赖浏览器本地时区

## 任务页 live 与管理员增强

- [x] `useSchedulerJobRuns` 有 `refetchInterval: 10_000`
- [x] `refetchIntervalInBackground: false`
- [x] 管理员系统概览有 15s 轮询
- [x] 任务页显示最后心跳
- [x] 任务页显示租约到期时间
- [x] 任务页显示当前子任务/StrategyRun ID
- [x] 任务页显示最新处理 Bar 时间
- [x] 盘中监控"绿色"判定包含三项条件
- [x] 盘后"完成"判定包含四项条件

## 截图缓存

- [x] 按 `event_id + instrument_id + chart_version` 缓存 PNG
- [x] 缓存 TTL 5-15 分钟
- [x] 文本 delivery 重试不触发新截图
- [x] 图片 delivery 重试优先复用缓存

## 文档体系

- [ ] 新增 `docs/产品与业务规则.md`
- [ ] 新增 `docs/系统架构.md`
- [ ] 新增 `docs/策略与指标口径.md`
- [ ] 新增 `docs/API与事件契约.md`
- [ ] 新增 `docs/定时任务与运行手册.md`
- [ ] 新增 `docs/部署与回滚.md`
- [ ] 新增 `docs/开发与测试.md`
- [ ] 新增 `docs/运维排障.md`
- [ ] 新增 `docs/安全规范.md`
- [ ] 每份文档顶部有元数据（Commit / 负责人 / 事实来源 / 自动-人工维护）
- [ ] `tools/update_docs.py` 支持 `--check` 模式

## 测试账号

- [ ] 专用普通用户账号已创建（不启用 MFA）
- [ ] 专用管理员账号已创建（不启用 MFA）
- [ ] 测试环境 URL 已确认可公网访问
- [ ] 账号密码与预期数据说明已整理

## 测试与验证

- [ ] 后端 `pytest tests/ -q` 0 failed, 0 error
- [ ] 前端 `npx tsc --noEmit` 无错误
- [ ] 前端 `npm run build` 无错误
- [ ] 前端 `npm run lint` 0 errors
- [ ] 首页等宽两列 + 3 KPI（浏览器验证）
- [ ] 设置页无通知规则卡（浏览器验证）
- [ ] 时间显示固定上海时区（浏览器验证）
- [ ] 任务页 10s 自动刷新（浏览器验证）

## 部署与推送

- [ ] 已评估并执行必要的服务重建/重启
- [ ] `/api/health/ready` 返回 `{"status":"ready"}`
- [ ] `/api/version` git_sha 与最新提交一致
- [ ] 最新代码已 push 到 `origin/main`
- [ ] 测试账号信息已提供给用户
