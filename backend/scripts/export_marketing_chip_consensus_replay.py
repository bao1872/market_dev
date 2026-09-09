"""导出近岸蛋白真实筹码共识回放 frozen JSON（Marketing Chip Consensus Replay V1）。

一次性离线 read-only exporter：把营销门户 `ChipConsensusStory` 从 synthetic 教学模型
升级为「近岸蛋白 688137.SH 真实历史数据 + production node_cluster 算法 + 因果回放」。

复用链路（与生产 chart 详情逐行对齐，禁止 Marketing 重算筹码）：
  - 展示 K 线：MarketDataAggregationService.get_bars(1d, qfq, completed_only=True, limit=250)
  - 每个 frame 的算法输入：production NodeClusterInputProvider.get_inputs(
        end_date=frame_date, adjustment_as_of=frame_date)
      -> 只返回 <= frame_date 的已完成 qfq bar（无未来函数）
      -> point-in-time 复权（不因未来除权事件改变历史价）
  - frame.node：production `_compute_independent_node_cluster`（indicator_service 详情链
    同一 serialization helper，内部经 CanonicalComputationService.compute('node_cluster')），
    输出 profile_rows / profile_meta / peak_rows / node_regions / price_state / availability。
  - frame.summary：只从 production DTO 提取（is_poc node 的 low/mid/high + 多空量），
    不重算。

硬门（FAIL CLOSED）：
  - 每个 frame 的 node_inputs.availability 必须 == "available"（禁止混入 degraded）。
  - 每个 frame 的算法输入最新日线/15m 日期必须 <= frame_date（无未来函数断言）。
  - 导出的 node DTO 必须为 available 且 profile_rows / node_regions 非空。

参数（业主 M1–M6 合同）：
  VISIBLE_BARS = 250（2025-08-29 → 2026-09-09，由数据最后 250 根决定）
  CANONICAL_FRAMES = 64（frame_ends(30, 250, 64)，deterministic 补齐）
  目标日期由数据库真实最后 250 个交易日决定，不硬编码起止。

禁止：DB 写 / backfill / 新 API / backend runtime 改动 / node_cluster 算法改动。

用法（注册 verify runtime，只读 bz_stock，不写库）：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.export_marketing_chip_consensus_replay

输出：
  frontend/public/marketing-data/nearshore-protein-688137-chip-consensus-1d-250d.json
  （Marketing 按 MARKETING_MEDIA.chipConsensusReplay 读取；轻部署 copy 到
   dist-marketing-site/data/ 后 serve /marketing-assets/data/。）
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.indicator_service import _compute_independent_node_cluster
from app.services.market_data_aggregation_service import MarketDataAggregationService
from app.services.node_cluster_input_provider import NodeClusterInputProvider

# ===== 导出参数（业主 M1–M6 合同）=====
SYMBOL = "688137"        # 近岸蛋白
MARKET = "SH"            # 科创板（上海）
VISIBLE_BARS = 250       # 展示窗口：250 个交易日
START_VISIBLE_BARS = 30  # 第一帧从第 30 根 visible bar 开始
CANONICAL_FRAME_COUNT = 64  # canonical 关键帧数（业主从 48 提高到 64）
FRAME_COUNT_MIN = 60     # 允许 60~68（rounding 去重 + deterministic 补齐容差）
FRAME_COUNT_MAX = 68

TIME_FRAME = "1d"
ADJ = "qfq"
SCHEMA_VERSION = 1

# 与前端 StrategyChart 的 LAYERS.node（strategy-manifest.ts）同 shape 的 node 图层描述符。
# 只读展示元数据，非算法复制；node 渲染由生产 DTO 驱动。
NODE_LAYER: dict[str, Any] = {
    "strategy_id": "node_cluster",
    "strategy_name": "筹码共识价",
    "layer_id": "node",
    "layer_name": "成交量节点",
    "renderer": "price_zone",
    "pane": "price",
    "color": "#4f7cff",
    "direction_colored": False,
    "direction_up_color": None,
    "direction_down_color": None,
    "fields": [
        "profile_rows", "profile_meta", "peak_rows", "node_regions",
        "node_regions_hash", "state", "price_state", "availability",
        "degraded_reason",
    ],
    "hover_fields": [],
}


# =============================================================================
# 通用工具
# =============================================================================


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


def _clean(value: Any) -> float | None:
    """把 NaN/Inf/None 统一成 JSON 可序列化的 float|None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def frame_ends(start: int, end: int, count: int) -> list[int]:
    """生成严格单调递增的帧 endIndex 列表（M2 合同）。

    - np.linspace(start, end, count) -> round -> 去重（rounding 可能产生碰撞）。
    - 去重后若不足 count，用 deterministic 补齐策略（在最大 gap 中插入中点），
      禁止随机。最后一位强制 = end。
    """
    values = np.linspace(start, end, count)
    ends = sorted({int(round(x)) for x in values})
    # deterministic 补齐：反复在最大 gap 中插入中点（等值则取靠左 gap）。
    while len(ends) < count:
        best_gap = -1
        best_i = -1
        for i in range(len(ends) - 1):
            gap = ends[i + 1] - ends[i]
            if gap > best_gap:
                best_gap = gap
                best_i = i
        if best_gap <= 1:
            raise RuntimeError(
                f"frame_ends 无法补齐到 {count} 个严格递增 endIndex（span={end - start}）"
            )
        mid = (ends[best_i] + ends[best_i + 1]) // 2
        if mid == ends[best_i]:
            mid += 1
        ends.insert(best_i + 1, mid)
    ends[-1] = end
    assert ends[0] >= 25, f"第一帧应 >=25，实际 {ends[0]}"
    assert ends[-1] == end, f"最后一帧必须 == {end}，实际 {ends[-1]}"
    assert all(b > a for a, b in zip(ends, ends[1:])), "endIndex 必须严格递增"
    return ends


# =============================================================================
# 数据加载与逐帧计算
# =============================================================================


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


async def _load_visible_bars(session: Any, instrument_id: Any, limit: int) -> pd.DataFrame:
    """取真实 qfq 已完成日线（展示窗口，与 production indicator_service 同款查询）。"""
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
    if len(df) < limit:
        raise SystemExit(f"可用日线不足 {limit} 根，实际 {len(df)}")
    return df.iloc[-limit:]


def _extract_summary(dto: dict[str, Any]) -> dict[str, Any]:
    """从 production node DTO 提取展示 summary（只提取，不重算筹码）。

    - primaryConsensusPrice = profile_meta.poc_price
    - consensusLow/Mid/High = is_poc node 的 low/mid/high（VAH/VAL 仅 value area，
      禁止冒充“主要成交密集区上下沿”）
    - 多空量 = is_poc node 的 bullish/bearish/total_volume
    """
    meta = dto.get("profile_meta") or {}
    poc_price = _clean(meta.get("poc_price"))
    poc_node: dict[str, Any] | None = None
    for region in dto.get("node_regions") or []:
        if region.get("is_poc"):
            poc_node = region
            break
    return {
        "primaryConsensusPrice": poc_price,
        "consensusLow": _clean(poc_node.get("low")) if poc_node else None,
        "consensusMid": _clean(poc_node.get("mid")) if poc_node else None,
        "consensusHigh": _clean(poc_node.get("high")) if poc_node else None,
        "bullishVolume": _clean(poc_node.get("bullish_volume")) if poc_node else None,
        "bearishVolume": _clean(poc_node.get("bearish_volume")) if poc_node else None,
        "totalVolume": _clean(poc_node.get("total_volume")) if poc_node else None,
    }


async def export() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        inst = await _resolve_instrument(session, SYMBOL, MARKET)
        visible_df = await _load_visible_bars(session, inst.id, VISIBLE_BARS)

        ends = frame_ends(START_VISIBLE_BARS, VISIBLE_BARS, CANONICAL_FRAME_COUNT)
        if not (FRAME_COUNT_MIN <= len(ends) <= FRAME_COUNT_MAX):
            raise SystemExit(
                f"帧数应在 {FRAME_COUNT_MIN}~{FRAME_COUNT_MAX}，实际 {len(ends)}"
            )

        frames: list[dict[str, Any]] = []
        poc_trajectory: list[float] = []
        for end_index in ends:
            frame_date = visible_df.index[end_index - 1].date()
            frame_as_of = frame_date.isoformat()

            # 生产唯一输入 Provider（point-in-time：end_date + adjustment_as_of）
            node_input = await NodeClusterInputProvider.get_inputs(
                session,
                inst.id,
                adjustment_as_of=frame_date,
                end_date=frame_date,
            )

            # 硬门 1：availability 必须 available（禁止 degraded/unavailable 混入）
            if node_input.availability != "available":
                raise SystemExit(
                    f"帧 {end_index} ({frame_as_of}) availability="
                    f"{node_input.availability} degraded_reason="
                    f"{node_input.degraded_reason} —— 要求全帧 available，FAIL CLOSED"
                )

            # 硬门 2：无未来函数——算法输入最新 bar 不得晚于 frame 日期
            daily_last = node_input.daily_bars.index[-1].date()
            m15_last = node_input.bars_15m.index[-1].date()
            if daily_last > frame_date or m15_last > frame_date:
                raise SystemExit(
                    f"帧 {end_index} ({frame_as_of}) 输入含未来 bar："
                    f"daily_last={daily_last} m15_last={m15_last}，FAIL CLOSED"
                )
            if node_input.daily_count < 250 or node_input.m15_count < 4000:
                raise SystemExit(
                    f"帧 {end_index} ({frame_as_of}) 输入数量异常："
                    f"daily={node_input.daily_count} m15={node_input.m15_count}，"
                    f"要求 daily>=250 m15>=4000，FAIL CLOSED"
                )

            # 生产 serialization helper（详情链同款）：内部走
            # CanonicalComputationService.compute(algorithm_id='node_cluster')
            dto = await _compute_independent_node_cluster(
                node_input,
                symbol=inst.symbol,
                instrument_id=inst.id,
            )
            if dto.get("availability") != "available":
                raise SystemExit(
                    f"帧 {end_index} ({frame_as_of}) node DTO availability="
                    f"{dto.get('availability')} degraded_reason="
                    f"{dto.get('degraded_reason')}，FAIL CLOSED"
                )
            if not dto.get("profile_rows") or not dto.get("node_regions"):
                raise SystemExit(
                    f"帧 {end_index} ({frame_as_of}) node DTO 为空"
                    f"（profile_rows/node_regions 缺失），FAIL CLOSED"
                )

            summary = _extract_summary(dto)
            if summary["primaryConsensusPrice"] is None:
                raise SystemExit(
                    f"帧 {end_index} ({frame_as_of}) primaryConsensusPrice 为空，FAIL CLOSED"
                )
            poc_trajectory.append(float(summary["primaryConsensusPrice"]))

            frames.append(
                {
                    "endIndex": int(end_index),
                    "endTime": visible_df.index[end_index - 1].isoformat(),
                    "node": _sanitize(dto),
                    "summary": summary,
                }
            )

        bars = [_bar_to_json(idx, row) for idx, row in visible_df.iterrows()]

        # 输出级 POC 迁移 sanity：真实数据必须存在显著迁移（35→45→42.7 量级）。
        poc_min = min(poc_trajectory)
        poc_max = max(poc_trajectory)
        if poc_max - poc_min < 2.0:
            raise SystemExit(
                f"POC 迁移过弱（min={poc_min:.2f} max={poc_max:.2f}），"
                f"不满足“存在可观察迁移”的窗口要求，FAIL CLOSED"
            )

        return {
            "schemaVersion": SCHEMA_VERSION,
            "instrument": {
                "symbol": inst.symbol,
                "name": inst.name,
                "exchange": inst.market,
            },
            "timeframe": TIME_FRAME,
            "adj": ADJ,
            "firstVisibleDate": bars[0]["time"],
            "lastVisibleDate": bars[-1]["time"],
            "generatedAt": datetime.now(UTC).isoformat(),
            "provenance": {
                "algorithmId": "node_cluster",
                "source": "CanonicalComputationService",
                "availability": "available",
                "gitSha": (os.environ.get("TARGET_SHA", "") or "dev-local"),
            },
            "bars": bars,
            "nodeLayer": NODE_LAYER,
            "frames": frames,
        }


# =============================================================================
# fail-closed 校验
# =============================================================================


def _validate(payload: dict[str, Any]) -> None:
    inst = payload["instrument"]
    if inst["name"] != "近岸蛋白":
        raise ValueError(f"instrument.name 应为 近岸蛋白，实际 {inst['name']}")
    if inst["symbol"] != "688137":
        raise ValueError(f"instrument.symbol 应为 688137，实际 {inst['symbol']}")
    if inst["exchange"] != "SH":
        raise ValueError(f"instrument.exchange 应为 SH，实际 {inst['exchange']}")

    n = len(payload["bars"])
    if n != VISIBLE_BARS:
        raise ValueError(f"bars 应为 {VISIBLE_BARS}，实际 {n}")
    times = [b["time"] for b in payload["bars"]]
    if times != sorted(times):
        raise ValueError("bars time 必须单调递增")

    prov = payload["provenance"]
    if prov["algorithmId"] != "node_cluster":
        raise ValueError(f"algorithmId 应为 node_cluster，实际 {prov['algorithmId']}")
    if "CanonicalComputationService" not in prov["source"]:
        raise ValueError(f"source 必须包含 CanonicalComputationService，实际 {prov['source']}")
    if prov["availability"] != "available":
        raise ValueError(f"provenance.availability 应为 available，实际 {prov['availability']}")

    frames = payload["frames"]
    if not (FRAME_COUNT_MIN <= len(frames) <= FRAME_COUNT_MAX):
        raise ValueError(f"帧数应在 {FRAME_COUNT_MIN}~{FRAME_COUNT_MAX}，实际 {len(frames)}")

    prev = 0
    for f in frames:
        e = int(f["endIndex"])
        if e <= prev or e > n:
            raise ValueError(f"帧 endIndex 非法: {e}")
        if f["endTime"] != times[e - 1]:
            raise ValueError(f"帧 {e} endTime 与 bars 不对齐")
        node = f["node"]
        if node.get("availability") != "available":
            raise ValueError(f"帧 {e} node availability 必须 available")
        if not node.get("profile_rows"):
            raise ValueError(f"帧 {e} node.profile_rows 必须非空")
        if not node.get("node_regions"):
            raise ValueError(f"帧 {e} node.node_regions 必须非空")
        meta = node.get("profile_meta") or {}
        for key in ("poc_price", "profile_hash", "node_regions_hash",
                    "daily_bars_count", "bars_15m_count"):
            if key not in meta:
                raise ValueError(f"帧 {e} node.profile_meta 缺 {key}")
        summary = f["summary"]
        if "primaryConsensusPrice" not in summary:
            raise ValueError(f"帧 {e} summary 缺 primaryConsensusPrice")
        for key in ("consensusLow", "consensusMid", "consensusHigh",
                    "bullishVolume", "bearishVolume", "totalVolume"):
            if key not in summary:
                raise ValueError(f"帧 {e} summary 缺 {key}")
        prev = e
    if frames[0]["endIndex"] < 25:
        raise ValueError("第一帧 endIndex 应 >= 25")
    if frames[-1]["endIndex"] != n:
        raise ValueError("最后一帧 endIndex 必须等于 bars 长度")


# =============================================================================
# 入口
# =============================================================================


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="导出近岸蛋白真实筹码共识回放 JSON")
    parser.add_argument("--out", default=str(
        Path(__file__)
        .resolve()
        .parents[2]
        / "frontend/public/marketing-data/nearshore-protein-688137-chip-consensus-1d-250d.json"
    ))
    args = parser.parse_args()

    payload = await export()
    _validate(payload)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tmp.write_text(raw, "utf-8")
    tmp.replace(out)

    poc_values = [f["summary"]["primaryConsensusPrice"] for f in payload["frames"]]
    print(
        f"导出完成: {out}\n"
        f"  instrument={payload['instrument']['symbol']}.{payload['instrument']['exchange']}"
        f" {payload['instrument']['name']}\n"
        f"  window={payload['firstVisibleDate'][:10]} -> {payload['lastVisibleDate'][:10]}\n"
        f"  bars={len(payload['bars'])} frames={len(payload['frames'])}\n"
        f"  availability={payload['provenance']['availability']}\n"
        f"  POC min={min(poc_values):.2f} max={max(poc_values):.2f}\n"
        f"  raw={len(raw.encode('utf-8')) / 1e6:.2f} MB "
        f"gzip={len(gzip.compress(raw.encode('utf-8'))) / 1e6:.2f} MB"
    )


if __name__ == "__main__":
    asyncio.run(main_async())
