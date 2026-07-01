"""自选股历史穿越事件回测：复用 BollingerMonitor/VolumeNodeMonitor 的 calculate_state + detect_events。

回测方法：
- 对每只自选股，获取日线+15分钟线行情
- 逐日回溯（最近60个交易日），构造 MarketDataContext
- 用日线收盘价构造模拟1分钟线（prev=前一日收盘, cur=当日收盘）
- 调用 BollingerMonitor.calculate_state() + detect_events() 检测BB穿越
- 调用 VolumeNodeMonitor.calculate_state() + detect_events() 检测Node穿越
- 收集每只股票最近一次BB穿越和Node穿越事件及触发时间

注意：日线级别穿越检测精度为"收盘"，盘中实时监控精度为"分钟"（1分钟线）。
穿越检测逻辑完全复用策略运行时，未自己写计算。

用法：
    cd /root/web_dev/backend
    python scripts/test_monitor_backtest.py
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
logger = logging.getLogger("test_monitor_backtest")

USER_ID = uuid.UUID("b4ce72ca-f81d-4a52-a16f-402af9b660c8")

# 回溯交易日数
_LOOKBACK_DAYS = 60


async def main() -> None:
    from zoneinfo import ZoneInfo

    import pandas as pd
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models.instrument import Instrument
    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.models.watchlist import UserWatchlistItem
    from app.repositories.bar_repository import fetch_15min_bars, fetch_daily_bars
    from app.strategy.runtime import MarketDataContext, StrategyLoader

    cst = ZoneInfo("Asia/Shanghai")

    async with AsyncSessionLocal() as db:
        # ===== 1. 获取自选股列表（active=True，排除指数） =====
        wl_stmt = (
            select(UserWatchlistItem.instrument_id)
            .where(UserWatchlistItem.user_id == USER_ID, UserWatchlistItem.active.is_(True))
        )
        wl_result = await db.execute(wl_stmt)
        instrument_ids = [row[0] for row in wl_result.all()]

        inst_stmt = select(Instrument.id, Instrument.symbol, Instrument.name, Instrument.market).where(
            Instrument.id.in_(instrument_ids),
        )
        inst_result = await db.execute(inst_stmt)
        instruments = []
        for row in inst_result.all():
            sym = row[1] or ""
            mkt = row[3] or ""
            if (mkt == "SH" and sym.startswith("000")) or (mkt == "SZ" and sym.startswith("399")):
                continue
            instruments.append({"id": row[0], "symbol": row[1], "name": row[2]})

        print(f"自选股数量: {len(instruments)}（排除指数后）")

        # ===== 2. 加载监控策略运行时（复用现有逻辑） =====
        def_stmt = select(StrategyDefinition).where(StrategyDefinition.kind == "monitor")
        def_result = await db.execute(def_stmt)
        definitions = list(def_result.scalars().all())

        bb_runtime = None
        node_runtime = None

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

            try:
                runtime = await StrategyLoader.load(version)
            except Exception as exc:
                logger.warning("加载策略失败 %s: %s", defn.strategy_key, exc)
                continue

            if defn.strategy_key == "bb_monitor":
                bb_runtime = runtime
            elif defn.strategy_key == "volume_node_monitor":
                node_runtime = runtime

        if bb_runtime is None:
            print("ERROR: BB监控策略未加载")
            return
        if node_runtime is None:
            print("ERROR: Node监控策略未加载")
            return

        print(f"策略运行时加载成功: BB={bb_runtime.__class__.__name__}, Node={node_runtime.__class__.__name__}")

        # ===== 3. 逐股票回溯穿越事件（复用 calculate_state + detect_events） =====
        today = datetime.now(UTC).date()
        all_results: list[dict] = []

        for inst in instruments:
            inst_id = inst["id"]
            symbol = inst["symbol"]
            name = inst["name"]

            # 获取日线行情
            bars_daily = await fetch_daily_bars(
                db, inst_id,
                start_date=today - timedelta(days=_LOOKBACK_DAYS + 250),
                end_date=today,
            )
            if bars_daily.empty or len(bars_daily) < 30:
                print(f"  {name}({symbol}): 日线数据不足 ({len(bars_daily)}根)")
                continue

            # 获取15分钟线（Node检测需要）
            now_naive = datetime.now(UTC).replace(tzinfo=None)
            bars_15min = pd.DataFrame()
            try:
                bars_15min = await fetch_15min_bars(
                    db, inst_id,
                    start_time=now_naive - timedelta(days=800),
                    end_time=now_naive,
                )
            except Exception:
                pass

            # 逐日回溯，用日线收盘价构造模拟1分钟线
            bb_event = None
            node_event = None

            for i in range(len(bars_daily) - 1, 1, -1):
                if bb_event is not None and node_event is not None:
                    break

                df_slice = bars_daily.iloc[:i + 1].copy()
                cur_close = float(df_slice.iloc[-1]["close"])
                prev_close = float(df_slice.iloc[-2]["close"])
                bar_date = df_slice.index[-1]

                # 构造模拟1分钟线：2根bar，prev和cur
                # 日线级别穿越：prev=前一日收盘, cur=当日收盘
                fake_minute = pd.DataFrame(
                    {"open": [prev_close, cur_close], "high": [prev_close, cur_close],
                     "low": [prev_close, cur_close], "close": [prev_close, cur_close],
                     "volume": [0, 0], "amount": [0, 0]},
                    index=pd.DatetimeIndex([bar_date - timedelta(days=1), bar_date]),
                )

                context = MarketDataContext(
                    instrument_id=inst_id,
                    symbol=symbol,
                    bars_daily=df_slice,
                    bars_15min=bars_15min if not bars_15min.empty else None,
                    bars_minute=fake_minute,
                    trade_date=bar_date.date() if hasattr(bar_date, "date") else today,
                    bar_time=bar_date.to_pydatetime() if hasattr(bar_date, "to_pydatetime") else datetime.now(UTC),
                )

                # BB穿越检测（复用 BollingerMonitor.calculate_state + detect_events）
                if bb_event is None:
                    try:
                        bb_state = await bb_runtime.calculate_state(context)
                        bb_events = await bb_runtime.detect_events(context, None, bb_state)
                        if bb_events:
                            ev = bb_events[0]
                            payload = ev.payload or {}
                            bb_event = {
                                "date": bar_date.strftime("%Y-%m-%d") if hasattr(bar_date, "strftime") else str(bar_date),
                                "type": ev.event_type,
                                "payload": payload,
                            }
                    except Exception as exc:
                        logger.debug("BB检测失败 %s %s: %s", symbol, bar_date, exc)

                # Node穿越检测（复用 VolumeNodeMonitor.calculate_state + detect_events）
                if node_event is None:
                    try:
                        node_state = await node_runtime.calculate_state(context)
                        node_events = await node_runtime.detect_events(context, None, node_state)
                        if node_events:
                            ev = node_events[0]
                            payload = ev.payload or {}
                            node_event = {
                                "date": bar_date.strftime("%Y-%m-%d") if hasattr(bar_date, "strftime") else str(bar_date),
                                "type": ev.event_type,
                                "payload": payload,
                            }
                    except Exception as exc:
                        logger.debug("Node检测失败 %s %s: %s", symbol, bar_date, exc)

            result = {"instrument_id": inst_id, "symbol": symbol, "name": name, "bb": bb_event, "node": node_event}
            all_results.append(result)

        # ===== 4. 汇总输出 =====
        print("\n" + "=" * 80)
        print("自选股历史穿越事件回测结果（日线级别穿越检测）")
        print("说明：盘中实时监控用1分钟线做穿越判断，触发时间精确到分钟")
        print("      日线回测用日线收盘价模拟1分钟线，触发时间标注为'收盘'")
        print("=" * 80)

        bb_count = sum(1 for r in all_results if r["bb"] is not None)
        node_count = sum(1 for r in all_results if r["node"] is not None)
        print(f"自选股: {len(all_results)} 只 | BB穿越: {bb_count} 只 | Node穿越: {node_count} 只")
        print()

        for r in all_results:
            print(f"--- {r['name']}({r['symbol']}) ---")
            if r["bb"]:
                ev = r["bb"]
                p = ev["payload"]
                emoji = {"bb_upper_touch": "🔴", "bb_mid_touch": "🟠", "bb_lower_touch": "🟢"}.get(ev["type"], "📌")
                label = {"bb_upper_touch": "布林上轨穿越", "bb_mid_touch": "布林中轨穿越", "bb_lower_touch": "布林下轨穿越"}.get(ev["type"], ev["type"])
                print(f"  {emoji} {label}")
                print(f"    触发时间: {ev['date']} 收盘")
                if p.get("price") is not None:
                    print(f"    现价: {p['price']:.2f}  边界: {p.get('boundary', 0):.2f}  偏离: {p.get('dev_pct', 0):+.2f}%")
                snap = p.get("bb_snapshot") or {}
                if snap.get("bb_upper") is not None:
                    print(f"    BB: 上{snap['bb_upper']:.2f} 中{snap['bb_mid']:.2f} 下{snap['bb_lower']:.2f}")
                    if snap.get("bb_width") is not None:
                        print(f"    BB宽度: {snap['bb_width']:.4f}  BB位置: {snap.get('bb_pos', '-')}")
            else:
                print("  无BB穿越事件（近60个交易日）")

            if r["node"]:
                ev = r["node"]
                p = ev["payload"]
                print("  🟣 节点集群穿越")
                print(f"    触发时间: {ev['date']} 收盘")
                if p.get("price") is not None:
                    print(f"    现价: {p['price']:.2f}  边界: {p.get('boundary', 0):.2f}  偏离: {p.get('dev_pct', 0):+.2f}%")
            else:
                print("  无Node穿越事件（近60个交易日）")
            print()


if __name__ == "__main__":
    asyncio.run(main())
