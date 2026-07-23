# Runbook: 飞书图片投递问题排查

- **触发条件**: 飞书消息卡片显示但图片缺失 / 图片模糊 / 图片内容与文字不匹配 / 用户报告"图裂了"
- **前置条件**: 已读 AGENTS §七.6（飞书）+ §七.13（Atomic Snapshot 单 MDAS 读取）/ 拥有 `feishu_delivery_service` / `stock_capture_service` 调试权限
- **影响范围**: Capture 截图 → 飞书上传 → 卡片渲染链路
- **预计恢复时间**: 10-30 分钟

## 症状识别

| 症状 | 可能原因 |
|------|----------|
| 卡片有，图片完全缺失 | Capture 失败 / 飞书 upload failed / `image_definitively_failed` |
| 卡片有，图片显示破损图标 | 飞书 image_key 失效 / 上传成功但渲染失败 |
| 图片模糊 | `device_scale_factor` 设置错误 / viewport 尺寸错误 |
| 图片内容与文字不符 | `indicator_view` 不匹配 / 缓存键碰撞 / 三链不一致 |
| 仅文字成功，无图片 | `card_status=success` 但 `image_status!=success`（AGENTS §61.5） |

## 排查步骤

1. **定位失败投递**：

```bash
docker compose exec backend python -c "
from app.db.session import SessionLocal
from app.repositories.notification_repository import NotificationMessageRepository
db = SessionLocal()
repo = NotificationMessageRepository(db)
msgs = repo.list_recent_by_status(status='failed', limit=10, channel='feishu_platform_app')
for m in msgs:
    print(f'{m.id} | target={m.target_channel_id} | card={m.card_status} | image={m.image_status} | reason={m.failure_reason}')
"
```

2. **检查 Capture Job 状态**：

```bash
docker compose exec backend python -c "
from app.services.stock_capture_service import get_capture_job_status
# 替换 event_id
status = get_capture_job_status(event_id='evt-xxx')
print(status)
"
```

3. **检查 Capture 缓存**：

```bash
ls -lt /var/lib/capture_cache/ | head -10
# 或
docker compose exec backend ls -lt /app/capture_cache/ | head -10
```

4. **检查 Playwright 截图日志**：

```bash
docker compose logs backend --since 1h | grep -E "capture|playwright|screenshot" | tail -50
```

## 修复操作

### 操作 1: 重新触发 Capture（缓存已损坏）

```bash
# 清除该 event 的 Capture 缓存
docker compose exec backend python -c "
from app.services.stock_capture_service import _CACHE_DIR
import os, glob
# 替换 event_id
event_id = 'evt-xxx'
for f in glob.glob(os.path.join(_CACHE_DIR, f'{event_id}_*')):
    print(f'removing {f}')
    os.remove(f)
"

# 重新触发投递
docker compose exec backend python -c "
from app.services.feishu_delivery_service import redeliver_event
redeliver_event(event_id='evt-xxx')
"
```

**预期输出**: Capture 重新生成 + 飞书重新上传 + 用户收到新卡片。
**异常处理**: 如 Capture 仍失败，检查 Playwright chromium 是否可用、`device_scale_factor` 配置、viewport 1440×2560 是否正确。

### 操作 2: 修复 indicator_view 不匹配

如文字卡片显示"筹码共识价"但图片显示"布林带"，是 `indicator_view` 参数不匹配。

```bash
docker compose exec backend python -c "
from app.db.session import SessionLocal
from app.repositories.notification_repository import NotificationMessageRepository
db = SessionLocal()
repo = NotificationMessageRepository(db)
# 查看该消息的 resource_refs
m = repo.get_by_id(message_id='msg-xxx')
print(f'payload.indicator_view={m.payload.get(\"indicator_view\")}')
print(f'resource_refs={m.resource_refs}')
"
```

### 操作 3: 修复 `image_definitively_failed` 状态

AGENTS §61.5 要求：`card_status=success` 但 `image_status!=success` 时必须标记 `failed` 或 `pending`，禁止 `partial_failed` 或 `success` 掩盖。

```bash
docker compose exec backend python -c "
from app.db.session import SessionLocal
from app.repositories.notification_repository import NotificationMessageRepository
db = SessionLocal()
repo = NotificationMessageRepository(db)
# 将错误的 partial_failed 修正为 failed
repo.update_status(message_id='msg-xxx', status='failed', failure_reason='image_definitively_failed')
db.commit()
print('updated')
"
```

## 验证

1. **用户确认**: 飞书群收到正确图片，内容与文字匹配。
2. **DB 状态**: `card_status=success` AND `image_status=success` AND `status=success`。
3. **Capture 缓存命中**: 重新查看同一 event 应命中缓存（无需重新截图）。

## 防止复发

- Capture 缓存键必须包含 `indicator_view`（AGENTS §62.10），避免不同 view 缓存碰撞
- `MobileIndicatorStage` 必须设置 `data-testid="stock-detail-capture"` + `data-render-ready="true"` + `data-indicator-view="<view>"`，Playwright 等待 Ready 后再截图
- 飞书状态机测试 `test_state_machine.py` / `test_stock_detail_feishu_status.py` 必须覆盖 `image_definitively_failed` 场景
- Playwright E2E（CP-18）覆盖飞书舞台 1440×2560 + 90 根 bar + 股票名/发送时间场景

## 关联

- CHANGE-20260720-001（三类独立飞书图片 + IndicatorView 共享枚举）
- CHANGE-20260721-001（移动飞书舞台 1440×2560）
- AGENTS §七.6（飞书）+ §七.13（Atomic Snapshot 单 MDAS）+ §61.5（图片整体成功判定）
- ADR-0001（Atomic Snapshot 单 MDAS 读取）
