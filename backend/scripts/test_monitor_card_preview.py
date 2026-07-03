"""模拟监控事件飞书卡片推送：用真实数据构造 BB 穿越和 Node 穿越事件卡片。

用法：
    cd /root/web_dev/backend
    python scripts/test_monitor_card_preview.py

功能：
1. 从自选股中选取一只股票
2. 用新版 BollingerMonitor/VolumeNodeMonitor 的 calculate_state 获取真实 BB/Node 数据
3. 模拟穿越事件（构造合理的 prev_close/cur_close）
4. 按旧版 monitoring.py 的卡片格式构建飞书卡片
5. 推送两张卡片到飞书（BB 穿越卡片 + Node 穿越卡片）
"""

import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("test_monitor_card_preview")

USER_ID = uuid.UUID("b4ce72ca-f81d-4a52-a16f-402af9b660c8")


def _fmt_price(val) -> str:
    if val is None:
        return "-"
    return f"{val:.2f}"


def _fmt_pct(val) -> str:
    if val is None:
        return "-"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def _calc_deviation_pct(price: float, boundary: float) -> float | None:
    if boundary is None or boundary == 0:
        return None
    return round((price - boundary) / boundary * 100, 2)


async def main() -> None:
    import pandas as pd
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models.instrument import Instrument
    from app.models.notification import NotificationChannel
    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.models.watchlist import UserWatchlistItem
    from app.repositories.bar_repository import fetch_15min_bars, fetch_daily_bars
    from app.schemas.notification import NotificationMessageDTO
    from app.services.notification_service import create_message, deliver_message
    from app.strategy.runtime import MarketDataContext, StrategyLoader

    async with AsyncSessionLocal() as db:
        # ===== 查询自选股 =====
        wl_stmt = (
            select(UserWatchlistItem.instrument_id)
            .where(UserWatchlistItem.user_id == USER_ID)
        )
        wl_result = await db.execute(wl_stmt)
        instrument_ids = [row[0] for row in wl_result.all()]

        inst_stmt = select(Instrument.id, Instrument.symbol, Instrument.name).where(
            Instrument.id.in_(instrument_ids)
        )
        inst_result = await db.execute(inst_stmt)
        instruments = {row.id: {"symbol": row.symbol, "name": row.name} for row in inst_result.all()}

        test_id = instrument_ids[0]
        inst_info = instruments.get(test_id, {})
        symbol = inst_info.get("symbol", "UNKNOWN")
        name = inst_info.get("name", "UNKNOWN")
        logger.info("测试标的: %s(%s)", name, symbol)

        # ===== 拉取行情 =====
        from zoneinfo import ZoneInfo
        cst = ZoneInfo("Asia/Shanghai")
        now_cst = datetime.now(cst)
        now_utc = now_cst.astimezone(UTC)
        now_naive = now_cst.replace(tzinfo=None)
        today = now_cst.date()

        bars_daily = await fetch_daily_bars(
            db, test_id,
            start_date=today - timedelta(days=250),
            end_date=today,
        )
        bars_15min = pd.DataFrame()
        try:
            bars_15min = await fetch_15min_bars(
                db, test_id,
                start_time=now_naive - timedelta(days=800),
                end_time=now_naive,
            )
        except Exception as exc:
            logger.warning("15min行情拉取失败: %s", exc)

        logger.info("行情数据: daily=%d 15min=%d", len(bars_daily), len(bars_15min))

        if bars_daily.empty or len(bars_daily) < 25:
            logger.error("日线数据不足")
            return

        # ===== 加载监控策略，计算真实状态 =====
        def_stmt = select(StrategyDefinition).where(StrategyDefinition.kind == "monitor")
        def_result = await db.execute(def_stmt)
        definitions = list(def_result.scalars().all())

        bb_state = None
        node_state = None

        for defn in definitions:
            ver_stmt = (
                select(StrategyVersion)
                .where(
                    StrategyVersion.strategy_definition_id == defn.id,
                    StrategyVersion.status == "released",
                )
                .order_by(StrategyVersion.released_at.desc())
                .limit(1)
            )
            ver_result = await db.execute(ver_stmt)
            version = ver_result.scalar_one_or_none()
            if version is None:
                continue

            strategy_key = defn.strategy_key
            try:
                runtime = await StrategyLoader.load(version)
            except Exception as exc:
                logger.warning("加载策略失败 %s: %s", strategy_key, exc)
                continue

            context = MarketDataContext(
                instrument_id=test_id,
                symbol=symbol,
                bars_daily=bars_daily,
                bars_15min=bars_15min if not bars_15min.empty else None,
                bars_minute=None,
                trade_date=today,
                bar_time=now_utc,
            )

            try:
                state = await runtime.calculate_state(context)
            except Exception as exc:
                logger.warning("calculate_state 失败 %s: %s", strategy_key, exc)
                continue

            if strategy_key == "bb_monitor":
                bb_state = state.state
                logger.info("BB状态: %s", {k: v for k, v in bb_state.items() if not isinstance(v, (dict, list))})
            elif strategy_key == "volume_node_monitor":
                node_state = state.state
                logger.info("Node状态: %s", {k: v for k, v in node_state.items() if not isinstance(v, (dict, list))})

        # ===== 查询飞书渠道 =====
        ch_stmt = select(NotificationChannel).where(
            NotificationChannel.user_id == USER_ID,
            NotificationChannel.status == "active",
        )
        ch_result = await db.execute(ch_stmt)
        channels = list(ch_result.scalars().all())
        if not channels:
            logger.error("无活跃飞书渠道")
            return
        channel = channels[0]

        now_str = now_cst.strftime("%Y-%m-%d %H:%M")

        # ===== 卡片1: BB 穿越事件 =====
        if bb_state:
            ref_upper = bb_state.get("bb_upper")
            ref_mid = bb_state.get("bb_mid")
            ref_lower = bb_state.get("bb_lower")
            current_price = bb_state.get("current_price")
            bb_width = bb_state.get("bb_width")
            bb_pos = bb_state.get("bb_pos")

            # 如果 current_price 为 None（非交易时段），用日线最后收盘价
            if current_price is None:
                current_price = float(bars_daily.iloc[-1]["close"])

            # 模拟上轨穿越
            bb_event_type = "bb_upper_touch"
            bb_event_label = "布林上轨穿越"
            bb_emoji = "🔴"

            if ref_upper is not None:
                cur_price_bb = ref_upper + 0.5
                prev_close_bb = ref_upper - 1.0
            else:
                cur_price_bb = current_price * 1.03
                prev_close_bb = current_price * 0.99
                ref_upper = current_price * 1.02

            dev_pct_bb = _calc_deviation_pct(cur_price_bb, ref_upper)

            # [advice.md 第十一节遗留清理] 新建消息改用 MONITOR_EVENT，禁止生成 MONITOR_MEMBER_EVENT
            bb_dto = NotificationMessageDTO(
                message_type="MONITOR_EVENT",
                template_key="monitor_event",
                template_version="1.1.0",
                title=f"BB+节点监控 {now_str}",
                summary="自选股 19 只 | 触发 1 只\n上轨 1 | 中轨 0 | 下轨 0 | 节点 0",
                facts=[
                    {"key": "股票", "label": "股票", "value": f"{name} {symbol}"},
                    {"key": "事件", "label": "事件", "value": f"{bb_emoji} {bb_event_label}"},
                    {"key": "现价", "label": "现价", "value": _fmt_price(cur_price_bb)},
                    {"key": "上轨", "label": "上轨", "value": _fmt_price(ref_upper)},
                    {"key": "中轨", "label": "中轨", "value": _fmt_price(ref_mid)},
                    {"key": "下轨", "label": "下轨", "value": _fmt_price(ref_lower)},
                    {"key": "偏离度", "label": "偏离度", "value": _fmt_pct(dev_pct_bb)},
                    {"key": "BB宽度", "label": "BB宽度", "value": f"{bb_width:.4f}" if bb_width else "-"},
                    {"key": "BB位置", "label": "BB位置", "value": f"{bb_pos:.3f}" if bb_pos else "-"},
                    {"key": "穿越条件", "label": "穿越条件",
                     "value": f"prev_close({prev_close_bb:.2f}) < ref_upper({ref_upper:.2f}) <= cur_close({cur_price_bb:.2f})"},
                ],
                timeline=[
                    {"time": now_cst.isoformat(), "label": f"{bb_emoji} {bb_event_label} 上轨={_fmt_price(ref_upper)}"},
                ],
                resource_refs={"instrument_id": str(test_id), "event_type": bb_event_type, "simulated": True},
                data_time=now_cst.strftime("%Y-%m-%d %H:%M:%S"),
            )
        else:
            logger.warning("BB状态为空，跳过BB卡片")
            bb_dto = None

        # ===== 卡片2: Node 穿越事件 =====
        if node_state:
            poc_price_raw = node_state.get("poc_price")
            current_price = node_state.get("current_price")
            position_0_1 = node_state.get("position_0_1")

            # 从 upper_node/lower_node 提取筹码峰价格（dict: price_mid/price_low/price_high）
            upper_node = node_state.get("upper_node")
            lower_node = node_state.get("lower_node")

            # poc_price 也是 dict
            poc_price = poc_price_raw.get("price_mid") if isinstance(poc_price_raw, dict) else poc_price_raw

            # 提取 peak_price（优先用 upper_node，因为当前价在节点下方穿越上来）
            peak_price = None
            if upper_node and isinstance(upper_node, dict):
                peak_price = upper_node.get("price_mid")
            elif lower_node and isinstance(lower_node, dict):
                peak_price = lower_node.get("price_mid")

            # 如果 current_price 为 None，用日线最后收盘价
            if current_price is None:
                current_price = float(bars_daily.iloc[-1]["close"])

            # 如果没有 peak_price，用 POC 价格
            if peak_price is None:
                peak_price = poc_price if poc_price else current_price * 1.01

            # 模拟节点穿越
            node_event_type = "node_cluster_touch"
            node_event_label = "节点集群穿越"
            node_emoji = "🟣"

            cur_price_node = peak_price + 0.3
            prev_close_node = peak_price - 0.5
            dev_pct_node = _calc_deviation_pct(cur_price_node, peak_price)

            # 构建节点信息文本
            node_info_parts = []
            if upper_node and isinstance(upper_node, dict):
                node_info_parts.append(f"上方节点: {_fmt_price(upper_node.get('price_mid'))} [{_fmt_price(upper_node.get('price_low'))}-{_fmt_price(upper_node.get('price_high'))}]")
            if lower_node and isinstance(lower_node, dict):
                node_info_parts.append(f"下方节点: {_fmt_price(lower_node.get('price_mid'))} [{_fmt_price(lower_node.get('price_low'))}-{_fmt_price(lower_node.get('price_high'))}]")

            node_facts = [
                {"key": "股票", "label": "股票", "value": f"{name} {symbol}"},
                {"key": "事件", "label": "事件", "value": f"{node_emoji} {node_event_label}"},
                {"key": "现价", "label": "现价", "value": _fmt_price(cur_price_node)},
                {"key": "节点价", "label": "节点价", "value": _fmt_price(peak_price)},
                {"key": "偏离度", "label": "偏离度", "value": _fmt_pct(dev_pct_node)},
                {"key": "POC", "label": "POC", "value": _fmt_price(poc_price)},
                {"key": "位置(0-1)", "label": "位置(0-1)", "value": f"{position_0_1:.3f}" if position_0_1 else "-"},
                {"key": "穿越条件", "label": "穿越条件",
                 "value": f"prev_close({prev_close_node:.2f}) <= peak({peak_price:.2f}) < cur_close({cur_price_node:.2f})"},
            ]
            for i, info in enumerate(node_info_parts):
                node_facts.append({"key": f"节点{i+1}", "label": f"节点{i+1}", "value": info})

            # [advice.md 第十一节遗留清理] 新建消息改用 MONITOR_EVENT，禁止生成 MONITOR_MEMBER_EVENT
            node_dto = NotificationMessageDTO(
                message_type="MONITOR_EVENT",
                template_key="monitor_event",
                template_version="1.1.0",
                title=f"BB+节点监控 {now_str}",
                summary="自选股 19 只 | 触发 1 只\n上轨 0 | 中轨 0 | 下轨 0 | 节点 1",
                facts=node_facts,
                timeline=[
                    {"time": now_cst.isoformat(), "label": f"{node_emoji} {node_event_label} 节点={_fmt_price(peak_price)}"},
                ],
                resource_refs={"instrument_id": str(test_id), "event_type": node_event_type, "simulated": True},
                data_time=now_cst.strftime("%Y-%m-%d %H:%M:%S"),
            )
        else:
            logger.warning("Node状态为空，跳过Node卡片")
            node_dto = None

        # ===== 推送卡片 =====
        import hashlib
        ts_suffix = now_cst.strftime("%Y%m%d%H%M%S")

        for label, dto in [("BB穿越", bb_dto), ("Node穿越", node_dto)]:
            if dto is None:
                continue
            logger.info("推送卡片: %s", label)

            # 生成唯一幂等键（含时间戳，避免重复推送被去重）
            idem_key = hashlib.sha256(
                f"monitor_card_preview:{label}:{dto.resource_refs.get('event_type', '')}:{ts_suffix}".encode()
            ).hexdigest()

            message = await create_message(
                db=db,
                user_id=USER_ID,
                message_dto=dto,
                source_type="monitor_card_preview",
                idempotency_key=idem_key,
            )
            await db.commit()

            delivery = await deliver_message(
                db=db,
                message_id=message.id,
                channel_id=channel.id,
            )
            await db.commit()

            logger.info("卡片 %s 投递结果: status=%s", label, delivery.status)

        # ===== 结果汇总 =====
        print("\n" + "=" * 60)
        print("飞书卡片推送完成")
        print("=" * 60)
        print(f"标的: {name}({symbol})")
        print()

        if bb_state:
            print("卡片1 - BB穿越事件:")
            print(f"  参考线: 上轨={_fmt_price(bb_state.get('bb_upper'))} 中轨={_fmt_price(bb_state.get('bb_mid'))} 下轨={_fmt_price(bb_state.get('bb_lower'))}")
            print(f"  BB宽度={bb_state.get('bb_width')} BB位置={bb_state.get('bb_pos')}")
            if bb_state.get('bb_upper') is not None:
                print(f"  模拟价格: prev_close={prev_close_bb:.2f} cur_close={cur_price_bb:.2f}")
                print(f"  穿越条件: prev_close < ref_upper <= cur_close → {prev_close_bb < bb_state['bb_upper'] <= cur_price_bb}")
            print()

        if node_state:
            print("卡片2 - Node穿越事件:")
            print(f"  POC={_fmt_price(poc_price)}")
            print(f"  位置(0-1)={node_state.get('position_0_1')}")
            if upper_node and isinstance(upper_node, dict):
                print(f"  上方节点: {_fmt_price(upper_node.get('price_mid'))} [{_fmt_price(upper_node.get('price_low'))}-{_fmt_price(upper_node.get('price_high'))}]")
            if lower_node and isinstance(lower_node, dict):
                print(f"  下方节点: {_fmt_price(lower_node.get('price_mid'))} [{_fmt_price(lower_node.get('price_low'))}-{_fmt_price(lower_node.get('price_high'))}]")
            print(f"  模拟价格: prev_close={prev_close_node:.2f} cur_close={cur_price_node:.2f}")
            print(f"  穿越条件: prev_close <= peak < cur_close → {prev_close_node <= peak_price < cur_price_node}")
            print()

        print("请检查飞书是否收到卡片")


if __name__ == "__main__":
    asyncio.run(main())
