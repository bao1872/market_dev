"""导出中际旭创真实结构回放 frozen JSON（营销门户 V1.2）。

一次性离线导出：读取中际旭创（300308）近两年真实 qfq 日线，复用生产 SMC 链路
（非 reimplement），为每个回放帧生成「截至该历史前缀」的 canonical SMC 展示 DTO，
保证无未来函数（no look-ahead）：结构确认只使用该帧之前已存在的 bar。

用法（注册 verify runtime，只读 bz_stock，不写库）：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.export_marketing_structure_replay

复用链路（与生产 chart-snapshot 逐行对齐）：
  - MarketDataAggregationService.get_bars(adj='qfq', completed_only=True)
        -> 真实 qfq 日线（production indicator_service 同款 deterministic 查询）
  - CanonicalComputationService.compute(algorithm_id='smc', bars=<prefix_df>,
        display_bars=<len(prefix_df)>) -> 展示窗口 DTO（smc_view_adapter）
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

# ===== 导出参数 =====
SYMBOL = "300308"          # 中际旭创
MARKET = "SZ"              # 300xx 为创业板（深圳）
TARGET_BARS = 500          # 近两年日线：约 500 根（约 2 年）
FRAME_COUNT = 51           # 回放帧数 -> playback interval = 50s/(FRAME_COUNT-1)
SMC_WARMUP_START = 250     # SMC 需 warmup（ATR200 等），从该前缀开始出稳定结构

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
    # 只取最近 TARGET_BARS 根（近两年）
    return df.iloc[-limit:]


def _plan_frames(end_indexes: list[int]) -> None:
    """前置校验：endIndex 严格单调递增、范围合法。"""
    if len(end_indexes) != FRAME_COUNT:
        raise ValueError(f"帧数应为 {FRAME_COUNT}，实际 {len(end_indexes)}")
    prev = 0
    for e in end_indexes:
        if e <= prev:
            raise ValueError(f"endIndex 必须严格单调递增: {prev} -> {e}")
        prev = e


async def export(symbol: str, market: str, target_bars: int) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        inst = await _resolve_instrument(session, symbol, market)
        full_df = await _load_bars(session, inst.id, target_bars)
        n = len(full_df)
        if n < SMC_WARMUP_START:
            raise SystemExit(f"可用日线不足 {SMC_WARMUP_START} 根，实际 {n}")

        # 帧 endIndex：从 warmup 起点均匀推进到 N（约 51 帧，每帧间隔约 5 根）。
        step_total = max(1, n - SMC_WARMUP_START)
        step = max(1, math.floor(step_total / (FRAME_COUNT - 1)))
        end_indexes = list(range(SMC_WARMUP_START, n + 1, step))
        if len(end_indexes) > FRAME_COUNT:
            end_indexes = end_indexes[:FRAME_COUNT]
        if end_indexes[-1] != n:
            end_indexes[-1] = n
        # 单调性修正（step 兜底后仍可能因取整产生非严格递增，仅发生在 step=1 且重叠时）
        end_indexes = list(dict.fromkeys(end_indexes))
        _plan_frames(end_indexes)

        today = datetime.now(UTC).date()
        frames: list[dict[str, Any]] = []
        for e in end_indexes:
            prefix = full_df.iloc[:e]
            # 生产统一入口：CanonicalComputationService.compute(algorithm_id="smc")
            result = await CanonicalComputationService.compute(
                algorithm_id="smc",
                instrument_id=inst.id,
                as_of=str(today),
                bars=prefix,
                display_bars=e,
            )
            smc_dto = _sanitize(result.payload)
            # fail-closed：display_bars/total_bars 必须等于该前缀长度，offset 为 0，
            # 证明 SMC 只用前缀窗口（无未来 bar），索引与展示 bars 对齐。
            view = smc_dto.get("view", {})
            if int(view.get("display_bars", -1)) != e or int(view.get("total_bars", -1)) != e:
                raise SystemExit(
                    f"帧 {e} SMC 窗口不对齐: display_bars={view.get('display_bars')} "
                    f"total_bars={view.get('total_bars')} (期望 {e})"
                )
            frames.append(
                {
                    "endIndex": e,
                    "endTime": prefix.index[-1].isoformat(),
                    "smc": smc_dto,
                }
            )

        bars = [
            _bar_to_json(idx, row)
            for idx, row in full_df.iterrows()
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
    if n < SMC_WARMUP_START:
        raise ValueError(f"bars 不足 warmup: {n}")
    times = [b["time"] for b in payload["bars"]]
    if times != sorted(times):
        raise ValueError("bars time 必须单调递增")
    prev = 0
    for f in payload["frames"]:
        e = int(f["endIndex"])
        if e > n or e <= prev:
            raise ValueError(f"帧 endIndex 非法: {e}")
        if f["endTime"] != times[e - 1]:
            raise ValueError(f"帧 {e} endTime 与 bars 不对齐")
        if int(f["smc"]["view"]["display_bars"]) != e:
            raise ValueError(f"帧 {e} display_bars 与 endIndex 不一致")
        prev = e
    if len(payload["frames"]) != FRAME_COUNT:
        raise ValueError(f"帧数应为 {FRAME_COUNT}")


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="导出中际旭创真实结构回放 JSON")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--market", default=MARKET)
    parser.add_argument("--bars", type=int, default=TARGET_BARS)
    parser.add_argument(
        "--out",
        default=str(
            Path(__file__)
            .resolve()
            .parents[2]
            / "frontend/public/marketing-media/zhongji-xuchuang-300308-1d-2y.json"
        ),
    )
    args = parser.parse_args()

    payload = await export(args.symbol, args.market, args.bars)
    _validate(payload)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "utf-8")
    tmp.replace(out)
    print(f"导出完成: {out}  bars={len(payload['bars'])} frames={len(payload['frames'])}")


if __name__ == "__main__":
    asyncio.run(main_async())