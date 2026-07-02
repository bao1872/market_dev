"""端到端监控流程测试脚本：验证合并卡片+PNG图片推送。

用法：
    cd /root/web_dev/backend
    python scripts/test_monitor_e2e.py

功能：
1. 调用 MonitorBatchService.execute_monitor_cycle() 执行完整监控周期
2. 验证合并卡片格式（概览行+逐股票详情+BB快照+触发时间）
3. 验证 PNG 图片推送
4. 额外推送模拟事件卡片验证可视化效果
"""

import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("test_monitor_e2e")

USER_ID = uuid.UUID("b4ce72ca-f81d-4a52-a16f-402af9b660c8")


async def main() -> None:
    from sqlalchemy import select

    from app.constants.user_facing_labels import get_event_label, get_field_label
    from app.db import AsyncSessionLocal
    from app.models.instrument import Instrument
    from app.models.notification import NotificationChannel
    from app.models.watchlist import UserWatchlistItem
    from app.repositories.bar_repository import fetch_daily_bars
    from app.schemas.notification import NotificationMessageDTO
    from app.services.monitor_batch_service import (
        _EVENT_EMOJI,
        _SEVERITY_TEMPLATE,
        MonitorBatchService,
    )
    from app.services.notification_service import create_message, deliver_message

    async with AsyncSessionLocal() as db:
        # ===== Step 1: 执行 MonitorBatchService 完整监控周期 =====
        service = MonitorBatchService()
        result = await service.execute_monitor_cycle(db)
        await db.commit()

        print("\n" + "=" * 60)
        print("MonitorBatchService 执行结果")
        print("=" * 60)
        print(f"监控标的数: {result.total_instruments}")
        print(f"状态计算数: {result.total_states_computed}")
        print(f"检测事件数: {result.total_events_detected}")
        print(f"写入事件数: {result.total_events_written}")
        print(f"通知创建数: {result.total_notifications_created}")
        if result.errors:
            print(f"错误数: {len(result.errors)}")
            for err in result.errors[:5]:
                print(f"  - {err[:200]}")

        # ===== Step 2: 模拟多股票多事件合并卡片 =====
        # 用真实数据构造 BB + Node 事件，模拟合并卡片
        wl_stmt = (
            select(UserWatchlistItem.instrument_id)
            .where(UserWatchlistItem.user_id == USER_ID)
        )
        wl_result = await db.execute(wl_stmt)
        instrument_ids = [row[0] for row in wl_result.all()]

        inst_stmt = select(Instrument.id, Instrument.symbol, Instrument.name).where(
            Instrument.id.in_(instrument_ids[:3])
        )
        inst_result = await db.execute(inst_stmt)
        instruments = {row.id: {"symbol": row.symbol, "name": row.name} for row in inst_result.all()}

        # 查询飞书渠道
        ch_stmt = select(NotificationChannel).where(
            NotificationChannel.user_id == USER_ID,
            NotificationChannel.status == "active",
        )
        ch_result = await db.execute(ch_stmt)
        channels = list(ch_result.scalars().all())
        if not channels:
            print("无活跃飞书渠道")
            return
        channel = channels[0]

        # 模拟 3 只股票各 1-2 个事件的合并卡片
        from zoneinfo import ZoneInfo
        cst = ZoneInfo("Asia/Shanghai")
        now_cst = datetime.now(cst)

        # 构建模拟事件数据
        simulated_events = []
        for i, (inst_id, info) in enumerate(instruments.items()):
            # BB 事件
            simulated_events.append({
                "instrument_id": inst_id,
                "symbol": info["symbol"],
                "name": info["name"],
                "event_type": ["bb_upper_touch", "bb_mid_touch", "bb_lower_touch"][i % 3],
                "boundary": 100.0 + i * 10,
                "price": 101.0 + i * 10,
                "dev_pct": 0.5 + i * 0.1,
                "bb_upper": 102.0 + i * 10,
                "bb_mid": 95.0 + i * 10,
                "bb_lower": 88.0 + i * 10,
                "bb_width": 0.15,
                "bb_pos": 0.7 + i * 0.05,
                "event_time": now_cst.replace(hour=10, minute=15 + i * 5, second=0, microsecond=0),
            })
            # Node 事件（仅第1、3只股票）
            if i % 2 == 0:
                simulated_events.append({
                    "instrument_id": inst_id,
                    "symbol": info["symbol"],
                    "name": info["name"],
                    "event_type": "node_cluster_touch",
                    "boundary": 98.0 + i * 10,
                    "price": 98.5 + i * 10,
                    "dev_pct": 0.3 + i * 0.1,
                    "event_time": now_cst.replace(hour=10, minute=20 + i * 5, second=0, microsecond=0),
                })

        # 构建合并卡片 DTO（使用 MonitorBatchService._build_merged_card_dto 逻辑）
        trigger_counts = {
            "bb_upper_touch": 0, "bb_mid_touch": 0,
            "bb_lower_touch": 0, "node_cluster_touch": 0,
        }
        for ev in simulated_events:
            trigger_counts[ev["event_type"]] += 1

        # 概览行 - [advice.md 第二节] 通俗化：上轨/中轨/下轨/节点 → 波动上沿/价格中枢/波动下沿/密集区
        overview = (
            f"自选股 {len(instrument_ids)} 只 | 触发 {len(instruments)} 只\n"
            f"{get_field_label('bb_upper_short')} {trigger_counts['bb_upper_touch']} | "
            f"{get_field_label('bb_mid_short')} {trigger_counts['bb_mid_touch']} | "
            f"{get_field_label('bb_lower_short')} {trigger_counts['bb_lower_touch']} | "
            f"{get_field_label('node_cluster_short')} {trigger_counts['node_cluster_touch']}"
        )

        # 逐股票详情
        facts = []
        prev_inst_id = None
        for ev in simulated_events:
            if ev["instrument_id"] != prev_inst_id:
                if prev_inst_id is not None:
                    facts.append({"key": "_hr", "label": "", "value": "---"})
                facts.append({"key": "股票", "label": "股票", "value": f"**{ev['name']} {ev['symbol']}**"})
                prev_inst_id = ev["instrument_id"]

            emoji = _EVENT_EMOJI.get(ev["event_type"], "📌")
            label = get_event_label(ev["event_type"])
            # [advice.md 第二节] 边界标签通俗化：上轨/中轨/下轨/节点 → 近期波动上沿/中枢/下沿/成交密集区
            boundary_label = {
                "bb_upper_touch": get_field_label("bb_upper"),
                "bb_mid_touch": get_field_label("bb_mid"),
                "bb_lower_touch": get_field_label("bb_lower"),
                "node_cluster_touch": "成交密集区",
            }.get(ev["event_type"], "边界")

            facts.append({
                "key": ev["event_type"],
                "label": f"{emoji} {label}",
                "value": f"现价 {ev['price']:.2f} | {boundary_label} {ev['boundary']:.2f} | 偏离 {ev['dev_pct']:+.2f}%",
            })

            # BB 上下文 - [advice.md 第二节] 通俗化：BB/上/中/下/宽度/位置 → 通俗文案
            if ev["event_type"] in ("bb_upper_touch", "bb_mid_touch", "bb_lower_touch"):
                facts.append({
                    "key": f"bb_ctx_{ev['event_type']}",
                    "label": f"{get_field_label('bb_upper')}/{get_field_label('bb_mid')}/{get_field_label('bb_lower')}",
                    "value": f"{get_field_label('bb_upper')}{ev['bb_upper']:.2f} {get_field_label('bb_mid')}{ev['bb_mid']:.2f} {get_field_label('bb_lower')}{ev['bb_lower']:.2f} | 带宽{ev['bb_width']:.4f} {get_field_label('position')}{ev['bb_pos']:.3f}",
                })

        # 时间线
        timeline = []
        for ev in simulated_events:
            emoji = _EVENT_EMOJI.get(ev["event_type"], "📌")
            label = get_event_label(ev["event_type"])
            timeline.append({
                "time": ev["event_time"].isoformat(),
                "label": f"{emoji} {ev['name']} {label}",
            })

        # 最严重级别
        max_sev = "info"
        for ev in simulated_events:
            sev = {"danger": 3, "warn": 2, "info": 1}.get(
                {"bb_upper_touch": "danger", "bb_mid_touch": "warn", "bb_lower_touch": "info", "node_cluster_touch": "warn"}.get(ev["event_type"], "info"),
                1,
            )
            if sev > {"danger": 3, "warn": 2, "info": 1}.get(max_sev, 1):
                max_sev = {"bb_upper_touch": "danger", "bb_mid_touch": "warn", "bb_lower_touch": "info", "node_cluster_touch": "warn"}.get(ev["event_type"], "info")

        earliest_time = min(ev["event_time"] for ev in simulated_events)

        # [advice.md 第十一节遗留清理] 新建消息改用 MONITOR_EVENT，禁止生成 MONITOR_MEMBER_EVENT
        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title=f"BB+节点监控 {now_cst.strftime('%H:%M')}",
            summary=overview,
            facts=facts,
            timeline=timeline,
            resource_refs={
                "header_severity": max_sev,
                "simulated": True,
                "total_instruments": str(len(instrument_ids)),
                "triggered_count": str(len(instruments)),
            },
            data_time=earliest_time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 推送合并卡片
        import hashlib
        ts_suffix = now_cst.strftime("%Y%m%d%H%M%S")
        idem_key = hashlib.sha256(
            f"monitor_merged_card:{ts_suffix}".encode()
        ).hexdigest()

        message = await create_message(
            db=db,
            user_id=USER_ID,
            message_dto=dto,
            source_type="monitor_merged_card_test",
            idempotency_key=idem_key,
        )
        await db.commit()

        delivery = await deliver_message(
            db=db,
            message_id=message.id,
            channel_id=channel.id,
        )
        await db.commit()

        print()
        print(f"合并卡片投递: status={delivery.status}")
        print(f"概览: {overview}")
        print(f"事件数: {len(simulated_events)}")
        print(f"Header颜色: {_SEVERITY_TEMPLATE.get(max_sev, 'blue')}")
        print(f"触发时间: {earliest_time.strftime('%H:%M')} ~ {max(ev['event_time'] for ev in simulated_events).strftime('%H:%M')}")

        # ===== Step 3: 测试 PNG 图片推送 =====
        test_id = instrument_ids[0]
        inst_info = instruments.get(test_id, {})
        symbol = inst_info.get("symbol", "UNKNOWN")
        name = inst_info.get("name", "UNKNOWN")

        try:
            from app.services.monitor_chart_renderer import render_monitoring_chart
            from app.strategy.monitors.bollinger_monitor import _load_bollinger_module

            today = now_cst.date()
            now_naive = now_cst.replace(tzinfo=None)
            bars_daily = await fetch_daily_bars(
                db, test_id,
                start_date=today - timedelta(days=250),
                end_date=today,
            )

            if not bars_daily.empty and len(bars_daily) >= 25:
                bb_module = _load_bollinger_module()
                bb_mid, bb_upper, bb_lower = bb_module.bollinger(bars_daily, 20, 2.0)

                png_path = await render_monitoring_chart(
                    df=bars_daily,
                    bb_mid=bb_mid,
                    bb_upper=bb_upper,
                    bb_lower=bb_lower,
                    profile=None,
                    symbol=symbol,
                    stock_name=name,
                )

                if png_path:
                    from app.services.channel_adapter import get_adapter
                    adapter = get_adapter(channel.adapter_type)
                    with open(png_path, "rb") as f:
                        image_bytes = f.read()
                    img_result = await adapter.send_image_bytes(image_bytes, channel.target_config)
                    print(f"\nPNG图片推送: success={img_result.success}")
                    if not img_result.success:
                        print(f"  error: {img_result.error_code} - {img_result.error_message}")

                    # 清理临时文件
                    import os
                    try:
                        os.unlink(png_path)
                    except OSError:
                        pass
                else:
                    print("\nPNG渲染返回None（可能plotly未安装）")
            else:
                print("\n日线数据不足，跳过PNG测试")
        except Exception as exc:
            print(f"\nPNG测试失败: {exc}")

        print("\n测试完成")


if __name__ == "__main__":
    asyncio.run(main())
