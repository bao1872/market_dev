"""监控快照统一服务 - 提供 MonitorSnapshot 唯一真源。

所有需要监控快照的场景（盘中监控/个股详情/首页/自选/消息中心/手动飞书/截图）
MUST 调用本服务，禁止各自解析 compute_all_indicators() 内部结构。

数据来源：compute_all_indicators() 返回 data["watchlist_monitor"]（BB+VN 合并字段）
缓存键：instrument_id + timeframe + algorithm_version + last_bar_time
    - algorithm_version: watchlist_monitor 策略最新 released StrategyVersion.version
    - last_bar_time: bars_daily 最新 trade_date（DB MAX 查询，cheap）
    - 兜底：任一查询失败时用 as_of 分钟级时间戳，保证缓存键仍可生成

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

from app.constants.indicator_contract import DAILY_HISTORY_BARS
from app.core.time import now_shanghai
from app.models.instrument import Instrument
from app.services.indicator_service import compute_all_indicators

logger = logging.getLogger("services.monitor_snapshot_service")

# [MonitorSnapshot] - watchlist_monitor 策略 ID（StrategyLoader._registry 中的 key）
_WATCHLIST_MONITOR_KEY = "watchlist_monitor"

# [MonitorSnapshot] - 内存缓存 TTL（秒）：作为 last_bar_time 兜底失效的安全网
# 缓存键已含 algorithm_version + last_bar_time，理论上版本/数据不变即命中；
# TTL 60 秒保证兜底场景（last_bar_time 取不到时用 as_of 分钟级）下新数据可自然失效
_CACHE_TTL_SECONDS = 60

# [MonitorSnapshot] - 默认复权方式（A 股标准前复权）
_DEFAULT_ADJ = "qfq"

# [MonitorSnapshot] - 描述: 默认返回 bar 数，引用 indicator_contract.DAILY_HISTORY_BARS 唯一真源
_DEFAULT_BARS = DAILY_HISTORY_BARS


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

    [自选股涨跌幅] - 描述: advice.md 第三节新增字段（前复权）
    - previous_close → previous_close  (上一交易日收盘价，前复权)
    - change_pct     → change_pct      (当日涨跌幅 %)

    注意：compute_indicators 返回的是 bar 对齐时间序列（list），
    快照取最新一根 bar 的值（[-1]）。
    previous_close/change_pct 由 _fetch_previous_close 单独从 BarDaily 查询并计算。
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
    previous_close: float | None = None
    change_pct: float | None = None


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

    内存缓存：键=instrument_id:timeframe:algorithm_version:last_bar_time，TTL 60 秒。
    - algorithm_version: watchlist_monitor 最新 released StrategyVersion.version
    - last_bar_time: bars_daily 最新 trade_date
    - TTL 作为兜底安全网（advice.md 第十一节遗留清理）
    """

    def __init__(self) -> None:
        # [MonitorSnapshot] - 内存缓存: 键=instrument_id:timeframe:algo_ver:last_bar_time, 值=(snapshot, 创建时间戳)
        self._cache: dict[str, tuple[MonitorSnapshot, float]] = {}

    async def _resolve_cache_key_components(
        self,
        db: AsyncSession,
        inst_uuid: uuid.UUID,
    ) -> tuple[str, str]:
        """解析缓存键的 algorithm_version 与 last_bar_time 分量。

        [MonitorSnapshot] - 描述: 缓存键扩展分量查询（advice.md 第十一节遗留清理）

        - algorithm_version: 查 watchlist_monitor 策略最新 released StrategyVersion.version
        - last_bar_time: 查 bars_daily 该标的最新 trade_date（ISO 字符串）
        - 任一查询失败时用 as_of 分钟级时间戳兜底，保证缓存键仍可生成

        Args:
            db: 异步 DB 会话
            inst_uuid: 标的 UUID

        Returns:
            (algorithm_version, last_bar_time) 元组
        """
        from app.models.bar import BarDaily
        from app.models.strategy import StrategyDefinition, StrategyVersion

        # algorithm_version: 查 watchlist_monitor 最新 released 版本号
        algo_version = "unknown"
        try:
            ver_stmt = (
                select(StrategyVersion.version)
                .join(
                    StrategyDefinition,
                    StrategyDefinition.id == StrategyVersion.strategy_definition_id,
                )
                .where(
                    StrategyDefinition.strategy_key == _WATCHLIST_MONITOR_KEY,
                    StrategyVersion.status == "released",
                )
                .order_by(StrategyVersion.released_at.desc())
                .limit(1)
            )
            ver_result = await db.execute(ver_stmt)
            ver_row = ver_result.first()
            if ver_row is not None:
                algo_version = str(ver_row[0])
        except Exception as exc:
            # 不吞异常：记录上下文，用兜底值保证缓存键可生成
            logger.warning(
                "查询 algorithm_version 失败，用 as_of 兜底 instrument_id=%s: %s",
                inst_uuid, exc,
            )
            algo_version = now_shanghai().strftime("%Y%m%d%H%M")

        # last_bar_time: 查 bars_daily 最新 trade_date
        last_bar_time = now_shanghai().strftime("%Y-%m-%dT%H:%M")
        try:
            bar_stmt = (
                select(BarDaily.trade_date)
                .where(BarDaily.instrument_id == inst_uuid)
                .order_by(BarDaily.trade_date.desc())
                .limit(1)
            )
            bar_result = await db.execute(bar_stmt)
            bar_row = bar_result.first()
            if bar_row is not None:
                last_bar_time = str(bar_row[0])
        except Exception as exc:
            # 不吞异常：记录上下文，用 as_of 分钟级兜底
            logger.warning(
                "查询 last_bar_time 失败，用 as_of 分钟级兜底 instrument_id=%s: %s",
                inst_uuid, exc,
            )
            last_bar_time = now_shanghai().strftime("%Y-%m-%dT%H:%M")

        return algo_version, last_bar_time

    async def _fetch_previous_close(
        self,
        db: AsyncSession,
        inst_uuid: uuid.UUID,
    ) -> float | None:
        """查询 inst_uuid 上一交易日（不含今日）的 BarDaily.close（前复权）。

        [自选股涨跌幅] - 描述: advice.md 第三节
            - 严格 < today（SHANGHAI 时区），排除今日未完成 Bar
            - 取最近一根 historical Bar 的 close 字段
            - 数据缺失返回 None（不抛异常）

        Args:
            db: 异步 DB 会话
            inst_uuid: 标的 UUID

        Returns:
            前一交易日 close（float），或 None
        """
        from app.models.bar import BarDaily

        today = now_shanghai().date()
        try:
            stmt = (
                select(BarDaily.close)
                .where(
                    BarDaily.instrument_id == inst_uuid,
                    BarDaily.trade_date < today,
                )
                .order_by(BarDaily.trade_date.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            row = result.first()
            if row is None:
                return None
            close_raw = row[0]
            if close_raw is None:
                return None
            return round(float(close_raw), 4)
        except Exception as exc:
            logger.warning(
                "查询 previous_close 失败 instrument_id=%s: %s",
                inst_uuid, exc,
            )
            return None

    @staticmethod
    def _compute_change_pct(
        current_price: float | None,
        previous_close: float | None,
    ) -> float | None:
        """计算涨跌幅（%）：(current - previous) / previous * 100。

        Args:
            current_price: 当前价
            previous_close: 前一交易日收盘价

        Returns:
            涨跌幅（%），保留 4 位小数；输入任一为 None 或 previous=0 时返回 None
        """
        if current_price is None or previous_close is None:
            return None
        if previous_close == 0:
            return None
        return round((float(current_price) - float(previous_close)) / float(previous_close) * 100, 4)

    def _build_cache_key(
        self,
        instrument_id: str,
        timeframe: str,
        algorithm_version: str,
        last_bar_time: str,
    ) -> str:
        """构建缓存键。

        [MonitorSnapshot] - 描述: 缓存键=instrument_id:timeframe:algorithm_version:last_bar_time

        Args:
            instrument_id: 标的 ID 字符串
            timeframe: 周期
            algorithm_version: 策略版本号
            last_bar_time: 最新 bar 时间字符串

        Returns:
            缓存键字符串
        """
        return f"{instrument_id}:{timeframe}:{algorithm_version}:{last_bar_time}"

    async def get_snapshot(
        self,
        db: AsyncSession,
        instrument_id: str,
        timeframe: str = "1d",
    ) -> MonitorSnapshot:
        """获取监控快照。

        流程：
        1. 查 instruments 表获取 symbol/name
        2. 解析缓存键分量（algorithm_version + last_bar_time）
        3. 缓存命中且未过期则直接返回（跳过 compute_all_indicators）
        4. 调用 compute_all_indicators(symbol, timeframe)
        5. 从 result["data"]["watchlist_monitor"] 读取字段
        6. 映射字段（bb_upper→range_upper 等）
        7. as_of = now_shanghai()，写入缓存

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
        total_start = time.time()

        # 1. 查 instruments 表获取 symbol/name
        db_start = time.time()
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
        db_ms = (time.time() - db_start) * 1000

        # 2. 解析缓存键分量（algorithm_version + last_bar_time）
        key_start = time.time()
        algorithm_version, last_bar_time = await self._resolve_cache_key_components(
            db, inst_uuid
        )
        cache_key = self._build_cache_key(
            instrument_id, timeframe, algorithm_version, last_bar_time
        )
        key_ms = (time.time() - key_start) * 1000

        # 3. 缓存检查
        cached = self._cache.get(cache_key)
        if cached is not None:
            snapshot, ts = cached
            if time.time() - ts < _CACHE_TTL_SECONDS:
                total_ms = (time.time() - total_start) * 1000
                logger.info(
                    "[MonitorSnapshot] 缓存命中 instrument_id=%s symbol=%s "
                    "timeframe=%s algo_ver=%s last_bar=%s "
                    "db_ms=%.1f key_ms=%.1f indicator_ms=0.0 total_ms=%.1f cache_hit=true",
                    instrument_id, symbol, timeframe,
                    algorithm_version, last_bar_time,
                    db_ms, key_ms, total_ms,
                )
                return snapshot

        # 4. 调用 compute_all_indicators（复用 SSOT，不重新实现指标计算）
        indicator_start = time.time()
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
        indicator_ms = (time.time() - indicator_start) * 1000

        # 5. 从 data["watchlist_monitor"] 读取字段（key 不存在抛 KeyError，不吞异常）
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

        # 6. 映射字段（list[-1] → float）
        current_price = _last_float(wm.get("current_price"))
        # [自选股涨跌幅] - 描述: 查询 previous_close 并计算 change_pct（advice.md 第三节）
        previous_close = await self._fetch_previous_close(db, inst_uuid)
        change_pct = self._compute_change_pct(current_price, previous_close)
        snapshot = MonitorSnapshot(
            instrument_id=instrument_id,
            symbol=symbol,
            name=name,
            as_of=now_shanghai(),
            current_price=current_price,
            range_upper=_last_float(wm.get("bb_upper")),
            range_center=_last_float(wm.get("bb_mid")),
            range_lower=_last_float(wm.get("bb_lower")),
            upper_volume_zone=_last_float(wm.get("upper_node")),
            lower_volume_zone=_last_float(wm.get("lower_node")),
            most_traded_price=_last_float(wm.get("poc_price")),
            range_position=_last_float(wm.get("position_0_1")),
            previous_close=previous_close,
            change_pct=change_pct,
        )

        # 7. 写入缓存
        self._cache[cache_key] = (snapshot, time.time())

        total_ms = (time.time() - total_start) * 1000
        logger.info(
            "[MonitorSnapshot] 快照生成 instrument_id=%s symbol=%s "
            "timeframe=%s algo_ver=%s last_bar=%s current_price=%s "
            "db_ms=%.1f key_ms=%.1f indicator_ms=%.1f total_ms=%.1f cache_hit=false",
            instrument_id, symbol, timeframe,
            algorithm_version, last_bar_time, snapshot.current_price,
            db_ms, key_ms, indicator_ms, total_ms,
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
    assert hasattr(svc, "_build_cache_key"), "应有 _build_cache_key 方法"
    assert hasattr(svc, "_resolve_cache_key_components"), "应有 _resolve_cache_key_components 方法"
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

    # 9. 验证缓存键构建（advice.md 第十一节遗留清理：键含 algo_ver + last_bar_time）
    key1 = svc._build_cache_key("inst-1", "1d", "1.2.0", "2026-06-26")
    assert key1 == "inst-1:1d:1.2.0:2026-06-26", f"缓存键格式不匹配: {key1}"
    # 不同 algo_version 应产生不同键（策略升级后缓存自动失效）
    key2 = svc._build_cache_key("inst-1", "1d", "1.3.0", "2026-06-26")
    assert key1 != key2, "不同 algorithm_version 应产生不同缓存键"
    # 不同 last_bar_time 应产生不同键（新 bar 到达后缓存自动失效）
    key3 = svc._build_cache_key("inst-1", "1d", "1.2.0", "2026-06-27")
    assert key1 != key3, "不同 last_bar_time 应产生不同缓存键"
    # 不同 instrument_id 应产生不同键
    key4 = svc._build_cache_key("inst-2", "1d", "1.2.0", "2026-06-26")
    assert key1 != key4, "不同 instrument_id 应产生不同缓存键"
    print(f"缓存键构建 key1={key1} ✓")

    print("OK")
