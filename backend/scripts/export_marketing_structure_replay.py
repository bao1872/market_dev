"""导出中际旭创真实结构回放 frozen JSON（营销门户 V1.3）。

一次性离线导出：读取中际旭创（300308）真实 qfq 日线，复用生产 SMC 链路
（非 reimplement），为每个回放帧生成「截至该历史前缀」的 canonical SMC 展示 DTO，
保证无未来函数（no look-ahead）：结构确认只使用该帧之前已存在的 bar。

V1.3 关键变化 —— 显式分离 VISIBLE 与 WARMUP：
  - VISIBLE_BARS = 500：真正展示的近两年日线（bars 输出即这 500 根）。
  - WARMUP_BARS  = 300：只用于算法 warmup（ATR200 等），不直接展示。
  - 每个 canonical 帧对「300 warmup + 当前 visible 前缀」做 SMC 计算，但
    display_bars=visible_end、total_bars=prefix_end（>display），
    由生产 view adapter 把结构索引重基准到展示窗口，前端 K 线从第 1 根 \u2192 第 500 根平滑推进。
  - 回放从 START_VISIBLE_BARS=30 根开始，到 500 根结束，共 CANONICAL_FRAME_COUNT=101 帧。

用法（注册 verify runtime，只读 bz_stock，不写库）：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.export_marketing_structure_replay

复用链路（与生产 chart-snapshot 逐行对齐）：
  - MarketDataAggregationService.get_bars(adj='qfq', completed_only=True)
        -> 真实 qfq 日线（production indicator_service 同款 deterministic 查询）
  - CanonicalComputationService.compute(algorithm_id='smc', bars=<prefix_df>,
        as_of=<该帧日期>, display_bars=<visible_end>) -> 展示窗口 DTO（smc_view_adapter）
  - smc 图层描述符复制自 indicator_service 的 layers.append（同一 shape）

输出：
  frontend/public/marketing-media/zhongji-xuchuang-300308-1d-2y.json
  （营销门户 StructureStory 按 MARKETING_MEDIA.structureReplay 读取；轻部署只 serve
   /marketing-assets/media/，故放在 media 而非 data。）

安全边界：只读 DB；不 start worker / 不写 bz_stock / 不触发 backfill 写库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.canonical_computation_service import CanonicalComputationService
from app.services.market_data_aggregation_service import MarketDataAggregationService

# ===== 导出参数（V1.3：VISIBLE 与 WARMUP 显式分离）=====
SYMBOL = "300308"          # 中际旭创
MARKET = "SZ"              # 300xx 为创业板（深圳）
VISIBLE_BARS = 500         # 真正展示的近两年日线约 500 根
WARMUP_BARS = 300          # 仅用于 SMC 算法 warmup（ATR200 等），不直接展示
START_VISIBLE_BARS = 30    # 第一帧从该 visible 前缀开始（约 30 根）
CANONICAL_FRAME_COUNT = 101  # 结构计算状态帧数（与视觉播放解耦）
LOAD_BARS = VISIBLE_BARS + WARMUP_BARS  # 从库加载根数

TIME_FRAME = "1d"
ADJ = "qfq"
SCHEMA_VERSION = 1

# 与 indicator_service 的 layers.append 完全一致的 smc 图层描述符（生产单一来源）。
# 只读展示元数据，非算法复制；FVG 由生产 adapter 排除，字段不变。
SMC_LAYER: dict[str, Any] = {
    "strategy_id": "smc",
    "strategy_name": "SMC",
    "layer_id": "smc",
    "layer_name": "SMC",
    "renderer": "smc",
    "pane": "price",
    "color": None,
    "direction_colored": True,
    "direction_up_color": "#FF4D4F",   # A 股红涨
    "direction_down_color": "#22C55E", # A 股绿跌
    "fields": [
        "events", "order_blocks", "equal_highs_lows",
        "trailing", "swing_bias", "pivots", "time", "view",
    ],
    "hover_fields": [],
}


def _sanitize(value: Any) -> Any:
    """把 numpy / pandas 标量递归转成纯 python 可 JSON 序列化对象。"""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item") and isinstance(value, (int, float, bool)):
        try:
            return value.item()
        except Exception:  # noqa: BLE001 - numpy 标量 .item() 常规转换
            pass
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _bar_to_json(idx: pd.Timestamp, row: pd.Series) -> dict[str, Any]:
    return {
        "time": idx.isoformat(),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
    }


async def _resolve_instrument(session: Any, symbol: str, market: str) -> Instrument:
    row = (
        await session.execute(
            select(Instrument).where(
                Instrument.symbol == symbol,
                Instrument.market == market,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"找不到标的 {symbol}.{market}")
    return row


async def _load_bars(session: Any, instrument_id: Any, limit: int) -> pd.DataFrame:
    """取真实 qfq 已完成日线（deterministic，与 production indicator_service 一致）。"""
    mdas = MarketDataAggregationService()
    result = await mdas.get_bars(
        session,
        instrument_id,
        timeframe=TIME_FRAME,
        adj=ADJ,
        completed_only=True,
        limit=limit,
    )
    if result.bars.empty:
        raise SystemExit(f"{SYMBOL} 无可用已完成 qfq 日线")
    needed = ["open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in result.bars.columns:
            raise SystemExit(f"bars 缺列: {col}")
    df = result.bars.sort_index()
    return df.iloc[-limit:]


def _plan_frames(end_indexes: list[int]) -> None:
    """前置校验：endIndex 严格单调递增、落入 visible 范围。"""
    if len(end_indexes) != CANONICAL_FRAME_COUNT:
        raise ValueError(f"帧数应为 {CANONICAL_FRAME_COUNT}，实际 {len(end_indexes)}")
    if end_indexes[0] > 40:
        raise ValueError(f"第一帧应从接近 0 开头（<=40），实际 {end_indexes[0]}")
    if end_indexes[-1] != VISIBLE_BARS:
        raise ValueError(f"最后一帧必须是 {VISIBLE_BARS}，实际 {end_indexes[-1]}")
    prev = 0
    for e in end_indexes:
        if e <= prev:
            raise ValueError(f"endIndex 必须严格单调递增: {prev} -> {e}")
        prev = e


async def export() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        inst = await _resolve_instrument(session, SYMBOL, MARKET)
        history_df = await _load_bars(session, inst.id, LOAD_BARS)
        if len(history_df) < LOAD_BARS:
            raise SystemExit(
                f"可用日线不足 {LOAD_BARS} 根（VISIBLE+WARMUP），实际 {len(history_df)}"
            )

        # 显式分离：真正展示的近两年 = 最近 VISIBLE_BARS 根；其余只做 warmup。
        visible_df = history_df.iloc[-VISIBLE_BARS:]
        visible_offset = len(history_df) - len(visible_df)

        # 帧 endIndex（visible 坐标，从 START 平滑推进到 VISIBLE_BARS）。
        span = VISIBLE_BARS - START_VISIBLE_BARS
        end_indexes = [
            round(
                START_VISIBLE_BARS
                + i * span / (CANONICAL_FRAME_COUNT - 1)
            )
            for i in range(CANONICAL_FRAME_COUNT)
        ]
        # 严格单调（取整可能产生相等），fail-closed。
        cleaned: list[int] = []
        for e in end_indexes:
            if not cleaned or e > cleaned[-1]:
                cleaned.append(e)
        cleaned[-1] = VISIBLE_BARS
        _plan_frames(cleaned)
        end_indexes = cleaned

        frames: list[dict[str, Any]] = []
        for visible_end in end_indexes:
            prefix_end = visible_offset + visible_end
            prefix = history_df.iloc[:prefix_end]
            frame_as_of = prefix.index[-1].date().isoformat()

            # 生产统一入口：CanonicalComputationService.compute(algorithm_id="smc")
            result = await CanonicalComputationService.compute(
                algorithm_id="smc",
                instrument_id=inst.id,
                as_of=frame_as_of,
                bars=prefix,
                display_bars=visible_end,
            )
            smc_dto = _sanitize(result.payload)
            # fail-closed：display_bars 必须 = 该帧展示窗口长，total_bars 必须 = 前缀长，
            # 证明 SMC 只用前缀窗口（无未来 bar），且展示与 bars 对齐、存在历史 warmup。
            view = smc_dto.get("view", {})
            if int(view.get("display_bars", -1)) != visible_end:
                raise SystemExit(
                    f"帧 {visible_end} display_bars 不对齐: {view.get('display_bars')} (期望 {visible_end})"
                )
            if int(view.get("total_bars", -1)) != prefix_end:
                raise SystemExit(
                    f"帧 {visible_end} total_bars 不对齐: {view.get('total_bars')} (期望 {prefix_end})"
                )
            frames.append(
                {
                    "endIndex": visible_end,
                    "endTime": visible_df.index[visible_end - 1].isoformat(),
                    "smc": smc_dto,
                }
            )

        bars = [
            _bar_to_json(idx, row)
            for idx, row in visible_df.iterrows()
        ]

        return {
            "schemaVersion": SCHEMA_VERSION,
            "instrument": {"symbol": inst.symbol, "name": inst.name},
            "timeframe": TIME_FRAME,
            "adj": ADJ,
            "firstVisibleDate": bars[0]["time"],
            "lastVisibleDate": bars[-1]["time"],
            "generatedAt": datetime.now(UTC).isoformat(),
            "provenance": {
                "source": "production canonical smc (CanonicalComputationService)",
                "gitSha": (os.environ.get("TARGET_SHA", "") or "dev-local"),
            },
            "bars": bars,
            "smcLayer": SMC_LAYER,
            "frames": frames,
        }


def _validate(payload: dict[str, Any]) -> None:
    """fail-closed 校验导出产物，防止把非法结构提交上去。"""
    n = len(payload["bars"])
    if n != VISIBLE_BARS:
        raise ValueError(f"bars 应为 {VISIBLE_BARS}，实际 {n}")
    times = [b["time"] for b in payload["bars"]]
    if times != sorted(times):
        raise ValueError("bars time 必须单调递增")
    if len(payload["frames"]) != CANONICAL_FRAME_COUNT:
        raise ValueError(f"帧数应为 {CANONICAL_FRAME_COUNT}")
    prev = 0
    for f in payload["frames"]:
        e = int(f["endIndex"])
        if e <= prev or e > n:
            raise ValueError(f"帧 endIndex 非法: {e}")
        if f["endTime"] != times[e - 1]:
            raise ValueError(f"帧 {e} endTime 与 bars 不对齐")
        view = f["smc"]["view"]
        if int(view["display_bars"]) != e:
            raise ValueError(f"帧 {e} display_bars 与 endIndex 不一致")
        if int(view["total_bars"]) <= int(view["display_bars"]):
            raise ValueError(f"帧 {e} 缺少历史 warmup：total_bars 必须 > display_bars")
        prev = e
    if payload["frames"][0]["endIndex"] > 40:
        raise ValueError("第一帧应从接近 0 开头（<=40）")
    if payload["frames"][-1]["endIndex"] != n:
        raise ValueError("最后一帧 endIndex 必须等于 bars 长度")


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="导出中际旭创真实结构回放 JSON")
    parser.add_argument("--out", default=str(
        Path(__file__)
        .resolve()
        .parents[2]
        / "frontend/public/marketing-media/zhongji-xuchuang-300308-1d-2y.json"
    ))
    args = parser.parse_args()

    payload = await export()
    _validate(payload)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "utf-8")
    tmp.replace(out)
    print(f"导出完成: {out}  bars={len(payload['bars'])} frames={len(payload['frames'])}")


if __name__ == "__main__":
    asyncio.run(main_async())