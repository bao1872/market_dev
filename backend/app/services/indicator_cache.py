"""指标结果缓存服务 - 基于 Redis。

缓存键格式：indicator:{instrument_id}:{timeframe}:{adj}:{last_bar_time}:{algorithm_version}[:smc]
TTL：300 秒（5 分钟）

核心函数：
- get(...) -> dict | None
- set(...)
- invalidate(...)

设计要点：
- last_bar_time 作为缓存键组成部分：新 bar 到达时键自动变化，旧缓存自然失效
- algorithm_version 用于指标算法变更时强制失效（递增版本号）
- include_smc 作为缓存键后缀：SMC 与非 SMC 结果独立缓存，互不污染
- Redis 不可用时降级返回 None（不阻塞主流程，但记录 warning）
- 使用 redis.asyncio（复用 app.core.redis_client 单例）

用法：
    from app.services.indicator_cache import get as cache_get, set as cache_set

    cached = await cache_get(instrument_id, "1d", "qfq", "2026-06-18")
    if cached is not None:
        return cached  # 缓存命中
    # 缓存未命中：计算后写入
    result = await compute_all_indicators(...)
    await cache_set(instrument_id, "1d", "qfq", "2026-06-18", result)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.core.redis_client import get_redis

logger = logging.getLogger("services.indicator_cache")

# [指标缓存] - TTL: 300 秒（5 分钟），覆盖一个交易时段内的多次刷新
CACHE_TTL_SECONDS = 300

# [指标缓存] - 算法版本：指标计算逻辑变更时递增，使旧缓存自动失效
# v10: CHANGE-20260717-001 - SMC Pine parity 最终收口（warmup/历史分离 15m 5000 计算/4000 展示、
#      1mo ≥200；execution gate 严格复刻 Pine L784/L787；trailing NaN 严格复刻 math.max(high, na)=na；
#      OB 顺序 newest-first 复刻 array.unshift）；旧 v9 SMC 输出（事件数量/位置/OB 顺序/窗口左缘）
#      与新逻辑不一致，必须强制失效
# v9: CHANGE-20260716-001 - SMC crossover/crossunder 修正（pivot level 上一 bar 快照，
#     不再将 current_level 同时作为 [0] 和 [1]）；旧 v8 事件数量/位置可能不同，必须强制失效
# v8: CHANGE-20260715-007 - SMC DTO 重构（EQH/EQL second_pivot/confirmed 重命名、
#     swing_bias 字段、view adapter 裁窗口）；旧 v7 缓存 EQH/EQL 字段名不兼容，必须强制失效
# v7: CHANGE-20260715-002 - SMC Pine parity（SMA→RMA、CMR 除数修正、1d warmup≥500、
#     不截断 SMC 输出）；旧 v6 SMC 缓存基于 ref/smc.py SMA 实现，必须强制失效
# v6: CHANGE-011 - 新增 SMC 按需计算图层（include_smc 参数 + 缓存键后缀隔离）
# v5: PR #32 - DSA 全周期支持（bars_daily=macd_bars）+ 1w/1mo BB 用 compute_bollinger 计算
#     v4 旧缓存返回 1d-only DSA + 1w/1mo 无 BB，必须强制失效
# v11: CHANGE-20260718-004 - Node Cluster engine 统一三链（详情/监控/盘后全部经
#      node_cluster_engine.compute_node_cluster_profile 入口），输出新增
#      algorithm_version/contract_fingerprint/profile_hash/daily_source_hash/
#      bars_15m_source_hash/adj_factor_hash/adjustment_as_of 等诊断字段；
#      monitor_batch_service adj 由 none→qfq（三链口径对齐），并加实例级 Profile 缓存。
#      旧 v10 缓存的 node_cluster meta 字段缺失且 adj 口径不一致，必须强制失效。
# v12: CHANGE-20260721-002 - Display Frame Contract V2（PROMPT.md §二）。
#      删除 _display_window=100 硬编码，改用请求 bars 参数；display_frame 新增
#      requested_count/actual_count/first_time/last_time/include_realtime/is_partial/
#      adjustment_as_of 字段。indicators API 新增 include_realtime/completed_only/
#      adjustment_as_of 参数，与 bars API 同款，缓存键追加 spec 后缀。
#      旧 v11 缓存的 display_frame 缺失 V2 字段且展示窗口固定 100 根，必须强制失效。
ALGORITHM_VERSION = "v12"

# 缓存键前缀
_CACHE_PREFIX = "indicator"


def build_cache_key(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    last_bar_time: str | None,
    algorithm_version: str = ALGORITHM_VERSION,
    include_smc: bool = False,
    spec_suffix: str | None = None,
) -> str:
    """[指标缓存] - 构造缓存键。

    格式：
        include_smc=False: indicator:{instrument_id}:{timeframe}:{adj}:{last_bar_time}:{algorithm_version}[:{spec_suffix}]
        include_smc=True:  indicator:{instrument_id}:{timeframe}:{adj}:{last_bar_time}:{algorithm_version}:smc[:{spec_suffix}]

    Args:
        instrument_id: 标的 UUID
        timeframe: K 线周期（1d | 15m | 1h | 1w | 1mo）
        adj: 复权方式（qfq | none）
        last_bar_time: 最新 bar 时间戳（ISO 字符串）；None 时用 "unknown"
        algorithm_version: 算法版本号
        include_smc: 是否包含 SMC 图层（CHANGE-011）；True 时缓存键追加 :smc 后缀，
            使 SMC 与非 SMC 结果独立缓存，互不污染。
        spec_suffix: DisplayWindowSpec 后缀（V2，CHANGE-20260721-002）。
            来自 DisplayWindowSpec.to_cache_suffix()，编码 include_realtime/
            completed_only/adjustment_as_of 三参数。None 时省略（向后兼容）。

    Returns:
        Redis 缓存键字符串
    """
    # [指标缓存] - last_bar_time 为 None 时使用 "unknown"（首次查询无数据时）
    safe_last_bar = last_bar_time or "unknown"
    base = f"{_CACHE_PREFIX}:{instrument_id}:{timeframe}:{adj}:{safe_last_bar}:{algorithm_version}"
    # [CHANGE-011 SMC] - include_smc=True 时追加 :smc 后缀，SMC 与非 SMC 独立缓存
    smc_part = ":smc" if include_smc else ""
    # [CHANGE-20260721-002 V2] - 追加 spec 后缀，不同 DisplayWindowSpec 独立缓存
    spec_part = f":{spec_suffix}" if spec_suffix else ""
    return f"{base}{smc_part}{spec_part}"


async def get(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    last_bar_time: str | None,
    include_smc: bool = False,
    spec_suffix: str | None = None,
) -> dict[str, Any] | None:
    """[指标缓存] - 从 Redis 读取缓存的指标结果。

    Redis 不可用或键不存在时返回 None（不抛异常，由调用方降级计算）。

    Args:
        instrument_id: 标的 UUID
        timeframe: K 线周期
        adj: 复权方式
        last_bar_time: 最新 bar 时间戳（ISO 字符串）
        include_smc: 是否读取 SMC 版本缓存（CHANGE-011）
        spec_suffix: DisplayWindowSpec 后缀（V2，CHANGE-20260721-002）

    Returns:
        dict: 缓存的指标结果；None 表示未命中或读取失败
    """
    key = build_cache_key(
        instrument_id, timeframe, adj, last_bar_time,
        include_smc=include_smc, spec_suffix=spec_suffix,
    )
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        # [指标缓存] - Redis 异常不阻塞主流程，降级为缓存未命中
        logger.warning("指标缓存读取失败 key=%s: %s", key, exc)
        return None


async def set(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    last_bar_time: str | None,
    value: dict[str, Any],
    include_smc: bool = False,
    spec_suffix: str | None = None,
) -> None:
    """[指标缓存] - 将指标结果写入 Redis 缓存。

    Args:
        instrument_id: 标的 UUID
        timeframe: K 线周期
        adj: 复权方式
        last_bar_time: 最新 bar 时间戳（ISO 字符串）
        value: 指标结果字典
        include_smc: 是否写入 SMC 版本缓存（CHANGE-011）
        spec_suffix: DisplayWindowSpec 后缀（V2，CHANGE-20260721-002）
    """
    key = build_cache_key(
        instrument_id, timeframe, adj, last_bar_time,
        include_smc=include_smc, spec_suffix=spec_suffix,
    )
    try:
        redis = get_redis()
        # [指标缓存] - JSON 序列化，default=str 处理不可序列化类型（如 UUID/datetime）
        payload = json.dumps(value, ensure_ascii=False, default=str)
        await redis.set(key, payload, ex=CACHE_TTL_SECONDS)
    except Exception as exc:
        # [指标缓存] - 写入失败不阻塞主流程（下次查询会重新计算）
        logger.warning("指标缓存写入失败 key=%s: %s", key, exc)


async def invalidate(
    instrument_id: uuid.UUID,
    timeframe: str | None = None,
    adj: str | None = None,
) -> int:
    """[指标缓存] - 使缓存失效（按 instrument_id 模糊删除，或精确删除）。

    使用 SCAN 而非 KEYS 避免阻塞 Redis（生产环境安全）。

    Args:
        instrument_id: 标的 UUID
        timeframe: 可选，指定周期则仅删除该周期缓存
        adj: 可选，指定复权方式则仅删除该复权缓存

    Returns:
        删除的键数量
    """
    # [指标缓存] - 构造匹配 pattern：从粗到细
    if timeframe is not None and adj is not None:
        pattern = f"{_CACHE_PREFIX}:{instrument_id}:{timeframe}:{adj}:*"
    elif timeframe is not None:
        pattern = f"{_CACHE_PREFIX}:{instrument_id}:{timeframe}:*"
    else:
        pattern = f"{_CACHE_PREFIX}:{instrument_id}:*"

    deleted = 0
    try:
        redis = get_redis()
        # [指标缓存] - SCAN 迭代删除，避免 KEYS 阻塞（count=100 平衡性能与延迟）
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                deleted += await redis.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:
        logger.warning(
            "指标缓存失效失败 instrument_id=%s pattern=%s: %s",
            instrument_id, pattern, exc,
        )
    return deleted


if __name__ == "__main__":
    # 自测入口：验证缓存键构造与模块加载（不连 Redis，无副作用）
    print("=== indicator_cache self-test ===")

    test_instrument_id = uuid.UUID("12345678-1234-1234-1234-123456789012")

    # 1. 验证缓存键格式
    key1 = build_cache_key(test_instrument_id, "1d", "qfq", "2026-06-18")
    expected1 = f"indicator:{test_instrument_id}:1d:qfq:2026-06-18:{ALGORITHM_VERSION}"
    assert key1 == expected1, f"缓存键不匹配: {key1} != {expected1}"
    print(f"缓存键格式 OK: {key1}")

    # 2. 验证 last_bar_time=None 时的回退
    key2 = build_cache_key(test_instrument_id, "1d", "qfq", None)
    assert "unknown" in key2, f"last_bar_time=None 应含 unknown: {key2}"
    print(f"last_bar_time=None 回退 OK: {key2}")

    # 3. 验证不同 timeframe 生成不同键
    key3 = build_cache_key(test_instrument_id, "15m", "qfq", "2026-06-18")
    assert key3 != key1, "不同 timeframe 应生成不同键"
    print(f"timeframe 区分 OK: {key3}")

    # 4. 验证不同 adj 生成不同键
    key4 = build_cache_key(test_instrument_id, "1d", "none", "2026-06-18")
    assert key4 != key1, "不同 adj 应生成不同键"
    print(f"adj 区分 OK: {key4}")

    # 5. 验证不同 last_bar_time 生成不同键（新 bar 到达时自动失效）
    key5 = build_cache_key(test_instrument_id, "1d", "qfq", "2026-06-19")
    assert key5 != key1, "不同 last_bar_time 应生成不同键"
    print(f"last_bar_time 区分 OK: {key5}")

    # 6. 验证 TTL 常量
    assert CACHE_TTL_SECONDS == 300, f"TTL 应为 300，实得 {CACHE_TTL_SECONDS}"
    print(f"CACHE_TTL_SECONDS={CACHE_TTL_SECONDS} OK")

    # 7. [CHANGE-011 SMC] 验证 include_smc=True 追加 :smc 后缀
    key_smc = build_cache_key(test_instrument_id, "1d", "qfq", "2026-06-18", include_smc=True)
    assert key_smc.endswith(":smc"), f"include_smc=True 应追加 :smc 后缀: {key_smc}"
    assert key_smc != key1, "include_smc=True 与 False 应生成不同键"
    print(f"include_smc 后缀区分 OK: {key_smc}")

    print("OK")
