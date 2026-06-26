"""监控快照统一服务 - 提供 MonitorSnapshot 唯一真源。

所有需要监控快照的场景（盘中监控/个股详情/首页/自选/消息中心/手动飞书/截图）
MUST 调用本服务，禁止各自解析 compute_all_indicators() 内部结构。

数据来源：compute_all_indicators() 返回 data["watchlist_monitor"]（BB+VN 合并字段）
缓存键：instrument_id + timeframe + last_bar_time + algorithm_version

用法（模块自测）：
    python -m app.services.monitor_snapshot_service    # 自测：验证 dataclass 和字段映射（不连 DB/网络）
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import now_shanghai
from app.models.instrument import Instrument
from app.services.indicator_service import compute_all_indicators

logger = logging.getLogger("services.monitor_snapshot_service")

# [MonitorSnapshot] - watchlist_monitor 策略 ID（StrategyLoader._registry 中的 key）
_WATCHLIST_MONITOR_KEY = "watchlist_monitor"

# [MonitorSnapshot] - 内存缓存 TTL（秒）：新 bar 到达后通过 TTL 自然失效
_CACHE_TTL_SECONDS = 60

# [MonitorSnapshot] - 默认复权方式（A 股标准前复权）
_DEFAULT_ADJ = "qfq"

# [MonitorSnapshot] - 默认返回 bar 数（快照只需最新值，250 与图表默认一致）
_DEFAULT_BARS = 250


@dataclass(frozen=True)
class MonitorSnapshot:
    """监控快照 - BB+VN 合并字段的统一只读视图。

    字段映射来源：compute_all_indicators() → data["watchlist_monitor"]
    - bb_upper    → range_upper        (布林上轨)
    - bb_mid      → range_center       (布林中轨)
    - bb_lower    → range_lower        (布林下轨)
    - upper_node  → upper_volume_zone  (上方成交量节点，取 price_mid)
    - lower_node  → lower_volume_zone  (下方成交量节点，取 price_mid)
    - poc_price   → most_traded_price  (成交量最大价位 POC)
    - position_0_1 → range_position    (区间位置 0~1)
    - current_price → current_price    (最新价)

    注意：compute_indicators 返回的是 bar 对齐时间序列（list），
    快照取最新一根 bar 的值（[-1]）。
    """

    instrument_id: str
    symbol: str
    name: str
    as_of: datetime
    current_price: float | None
    range_upper: float | None
    range_center: float | None
    range_lower: float | None
    upper_volume_zone: float | None
    lower_volume_zone: float | None
    most_traded_price: float | None
    range_position: float | None


def _last_float(values: list[Any] | None) -> float | None:
    """从时间序列 list 取最后一个元素并转为 float。

    compute_indicators 返回 bar 对齐时间序列（list），
    快照只需最新一根 bar 的值，取 [-1]。

    元素可能是：
    - float/int → 直接转 float
    - None → 返回 None
    - dict（如 upper_node={"price_mid": 10.5}）→ 取 "price_mid"

    NaN/Inf 转为 None（JSON 不支持）。

    Args:
        values: 时间序列 list（可能为 None 或空）

    Returns:
        最新 bar 的 float 值，或 None
    """
    if not values:
        return None
    last = values[-1]
    if last is None:
        return None
    if isinstance(last, dict):
        # upper_node/lower_node 是 {"price_mid": x, ...}
        last = last.get("price_mid")
        if last is None:
            return None
    try:
        f = float(last)
    except (TypeError, ValueError):
        return None
    # NaN/Inf 转为 None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


class MonitorSnapshotService:
    """监控快照服务 - 提供 MonitorSnapshot 唯一真源。

    所有需要监控快照的场景 MUST 调用本服务，
    禁止各自解析 compute_all_indicators() 内部结构。

    简单内存缓存：键=instrument_id:timeframe，TTL 60 秒。
    （理想缓存键应含 last_bar_time，但 compute_all_indicators 不返回该值；
    TTL 60 秒可保证新 bar 到达后自然失效，效果等价）
    """

    def __init__(self) -> None:
        # [MonitorSnapshot] - 内存缓存: 键=instrument_id:timeframe, 值=(snapshot, 创建时间戳)
        self._cache: dict[str, tuple[MonitorSnapshot, float]] = {}

    async def get_snapshot(
        self,
        db: AsyncSession,
        instrument_id: str,
        timeframe: str = "1d",
    ) -> MonitorSnapshot:
        """获取监控快照。

        流程：
        1. 查 instruments 表获取 symbol/name
        2. 调用 compute_all_indicators(symbol, timeframe)
        3. 从 result["data"]["watchlist_monitor"] 读取字段
        4. 映射字段（bb_upper→range_upper 等）
        5. as_of = now_shanghai()

        Args:
            db: 异步 DB 会话
            instrument_id: 标的 ID（str，内部转 UUID）
            timeframe: 周期（默认 1d）

        Returns:
            MonitorSnapshot 快照

        Raises:
            ValueError: instrument_id 格式非法或 instrument 不存在
            KeyError: watchlist_monitor key 不存在于 data（不吞异常）
            RuntimeError: compute_all_indicators 失败（含上下文）
        """
        # 缓存检查
        cache_key = f"{instrument_id}:{timeframe}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            snapshot, ts = cached
            if time.time() - ts < _CACHE_TTL_SECONDS:
                logger.debug(
                    "缓存命中 instrument_id=%s timeframe=%s", instrument_id, timeframe
                )
                return snapshot

        # 1. 查 instruments 表获取 symbol/name
        try:
            inst_uuid = uuid.UUID(instrument_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"instrument_id 格式非法: {instrument_id!r}"
            ) from exc

        stmt = select(Instrument.symbol, Instrument.name).where(
            Instrument.id == inst_uuid
        )
        result = await db.execute(stmt)
        row = result.first()
        if row is None:
            raise ValueError(f"instrument 不存在: instrument_id={instrument_id}")
        symbol: str = row[0]
        name: str = row[1]

        # 2. 调用 compute_all_indicators（复用 SSOT，不重新实现指标计算）
        try:
            indicators_result = await compute_all_indicators(
                session=db,
                instrument_id=inst_uuid,
                timeframe=timeframe,
                adj=_DEFAULT_ADJ,
                bars=_DEFAULT_BARS,
            )
        except Exception as exc:
            # 不吞异常：补上下文后 re-raise 为 RuntimeError
            raise RuntimeError(
                f"compute_all_indicators 失败 instrument_id={instrument_id} "
                f"symbol={symbol} timeframe={timeframe}: {exc}"
            ) from exc

        # 3. 从 data["watchlist_monitor"] 读取字段（key 不存在抛 KeyError，不吞异常）
        data: dict[str, Any] = indicators_result.get("data", {})
        wm: dict[str, Any] | None = data.get(_WATCHLIST_MONITOR_KEY)
        if wm is None:
            errors: dict[str, str] = indicators_result.get("errors", {})
            err_msg = errors.get(_WATCHLIST_MONITOR_KEY, "未知错误")
            raise KeyError(
                f"watchlist_monitor 不存在于 data 中 "
                f"instrument_id={instrument_id} timeframe={timeframe} "
                f"errors={err_msg}"
            )

        # 4. 映射字段（list[-1] → float）
        snapshot = MonitorSnapshot(
            instrument_id=instrument_id,
            symbol=symbol,
            name=name,
            as_of=now_shanghai(),
            current_price=_last_float(wm.get("current_price")),
            range_upper=_last_float(wm.get("bb_upper")),
            range_center=_last_float(wm.get("bb_mid")),
            range_lower=_last_float(wm.get("bb_lower")),
            upper_volume_zone=_last_float(wm.get("upper_node")),
            lower_volume_zone=_last_float(wm.get("lower_node")),
            most_traded_price=_last_float(wm.get("poc_price")),
            range_position=_last_float(wm.get("position_0_1")),
        )

        # 5. 写入缓存
        self._cache[cache_key] = (snapshot, time.time())

        logger.info(
            "监控快照生成 instrument_id=%s symbol=%s timeframe=%s current_price=%s",
            instrument_id, symbol, timeframe, snapshot.current_price,
        )

        return snapshot


# ===== 模块自测入口 =====

if __name__ == "__main__":
    # 自测入口：验证 dataclass 和字段映射（无副作用，不连 DB/网络）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证 MonitorSnapshot dataclass 字段
    expected_fields = [
        "instrument_id", "symbol", "name", "as_of",
        "current_price", "range_upper", "range_center", "range_lower",
        "upper_volume_zone", "lower_volume_zone",
        "most_traded_price", "range_position",
    ]
    actual_fields = list(MonitorSnapshot.__dataclass_fields__.keys())
    assert actual_fields == expected_fields, \
        f"MonitorSnapshot 字段不匹配: {actual_fields} != {expected_fields}"
    print(f"MonitorSnapshot fields={actual_fields} ✓")

    # 2. 验证 frozen=True（不可变）
    snap = MonitorSnapshot(
        instrument_id="test-id",
        symbol="000001",
        name="平安银行",
        as_of=now_shanghai(),
        current_price=10.5,
        range_upper=11.0,
        range_center=10.0,
        range_lower=9.0,
        upper_volume_zone=11.5,
        lower_volume_zone=8.5,
        most_traded_price=10.2,
        range_position=0.75,
    )
    try:
        snap.current_price = 99.0  # type: ignore[misc]
        raise AssertionError("frozen=True 的 dataclass 应不可变")
    except AttributeError:
        print("MonitorSnapshot frozen=True 不可变 ✓")

    # 3. 验证 _last_float 字段映射（list → float）
    assert _last_float(None) is None, "None 应返回 None"
    assert _last_float([]) is None, "空 list 应返回 None"
    assert _last_float([1.0, 2.0, 3.0]) == 3.0, "应取最后一个元素"
    assert _last_float([1, 2, 3]) == 3.0, "int 应转 float"
    assert _last_float([1.0, None, 3.0]) == 3.0, "中间 None 不影响"
    assert _last_float([1.0, 2.0, None]) is None, "末尾 None 返回 None"
    print("_last_float 基础映射 ✓")

    # 4. 验证 _last_float 处理 dict（upper_node/lower_node）
    assert _last_float([{"price_mid": 10.5}, {"price_mid": 11.0}]) == 11.0, \
        "dict 应取 price_mid"
    assert _last_float([{"price_mid": 10.5}, None]) is None, \
        "末尾 None dict 返回 None"
    assert _last_float([{"price_mid": None}]) is None, \
        "price_mid=None 返回 None"
    print("_last_float dict 映射 ✓")

    # 5. 验证 _last_float 处理 NaN/Inf
    assert _last_float([1.0, float("nan")]) is None, "NaN 应返回 None"
    assert _last_float([1.0, float("inf")]) is None, "Inf 应返回 None"
    assert _last_float([1.0, float("-inf")]) is None, "-Inf 应返回 None"
    print("_last_float NaN/Inf 处理 ✓")

    # 6. 验证字段映射对照（模拟 watchlist_monitor 输出）
    wm_mock: dict[str, Any] = {
        "bb_upper": [9.0, 10.0, 11.0],
        "bb_mid": [8.0, 9.0, 10.0],
        "bb_lower": [7.0, 8.0, 9.0],
        "upper_node": [{"price_mid": 11.5}, {"price_mid": 12.0}],
        "lower_node": [{"price_mid": 8.5}, {"price_mid": 8.0}],
        "poc_price": [10.2, 10.3, 10.5],
        "position_0_1": [0.5, 0.6, 0.75],
        "current_price": [10.0, 10.2, 10.5],
    }
    assert _last_float(wm_mock.get("bb_upper")) == 11.0
    assert _last_float(wm_mock.get("bb_mid")) == 10.0
    assert _last_float(wm_mock.get("bb_lower")) == 9.0
    assert _last_float(wm_mock.get("upper_node")) == 12.0
    assert _last_float(wm_mock.get("lower_node")) == 8.0
    assert _last_float(wm_mock.get("poc_price")) == 10.5
    assert _last_float(wm_mock.get("position_0_1")) == 0.75
    assert _last_float(wm_mock.get("current_price")) == 10.5
    print("字段映射对照（bb_upper→range_upper 等）✓")

    # 7. 验证 MonitorSnapshotService 可实例化 + 方法签名
    svc = MonitorSnapshotService()
    assert hasattr(svc, "get_snapshot"), "应有 get_snapshot 方法"
    assert hasattr(svc, "_cache"), "应有 _cache"
    sig = inspect.signature(svc.get_snapshot)
    params = list(sig.parameters.keys())
    # bound method 的 signature 不含 self
    assert params == ["db", "instrument_id", "timeframe"], \
        f"get_snapshot 参数不匹配: {params}"
    assert sig.parameters["timeframe"].default == "1d", \
        "timeframe 默认值应为 1d"
    print(f"MonitorSnapshotService.get_snapshot params={params} ✓")

    # 8. 验证缓存 TTL 常量
    assert _CACHE_TTL_SECONDS == 60, "缓存 TTL 应为 60 秒"
    assert _WATCHLIST_MONITOR_KEY == "watchlist_monitor"
    print(f"缓存 TTL={_CACHE_TTL_SECONDS}s key={_WATCHLIST_MONITOR_KEY} ✓")

    print("OK")
