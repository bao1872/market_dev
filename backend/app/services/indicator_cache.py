"""指标结果缓存服务 - 基于 Redis。

缓存键格式：indicator:{instrument_id}:{timeframe}:{adj}:{last_bar_time}:{algorithm_version}
TTL：300 秒（5 分钟）

核心函数：
- get(...) -> dict | None
- set(...)
- invalidate(...)

设计要点：
- last_bar_time 作为缓存键组成部分：新 bar 到达时键自动变化，旧缓存自然失效
- algorithm_version 用于指标算法变更时强制失效（递增版本号）
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
ALGORITHM_VERSION = "v1"

# 缓存键前缀
_CACHE_PREFIX = "indicator"


def build_cache_key(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    last_bar_time: str | None,
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    """[指标缓存] - 构造缓存键。

    格式：indicator:{instrument_id}:{timeframe}:{adj}:{last_bar_time}:{algorithm_version}

    Args:
        instrument_id: 标的 UUID
        timeframe: K 线周期（1d | 15m | 1h | 1w | 1mo）
        adj: 复权方式（qfq | none）
        last_bar_time: 最新 bar 时间戳（ISO 字符串）；None 时用 "unknown"
        algorithm_version: 算法版本号

    Returns:
        Redis 缓存键字符串
    """
    # [指标缓存] - last_bar_time 为 None 时使用 "unknown"（首次查询无数据时）
    safe_last_bar = last_bar_time or "unknown"
    return f"{_CACHE_PREFIX}:{instrument_id}:{timeframe}:{adj}:{safe_last_bar}:{algorithm_version}"


async def get(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    last_bar_time: str | None,
) -> dict[str, Any] | None:
    """[指标缓存] - 从 Redis 读取缓存的指标结果。

    Redis 不可用或键不存在时返回 None（不抛异常，由调用方降级计算）。

    Args:
        instrument_id: 标的 UUID
        timeframe: K 线周期
        adj: 复权方式
        last_bar_time: 最新 bar 时间戳（ISO 字符串）

    Returns:
        dict: 缓存的指标结果；None 表示未命中或读取失败
    """
    key = build_cache_key(instrument_id, timeframe, adj, last_bar_time)
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
) -> None:
    """[指标缓存] - 将指标结果写入 Redis 缓存。

    Args:
        instrument_id: 标的 UUID
        timeframe: K 线周期
        adj: 复权方式
        last_bar_time: 最新 bar 时间戳（ISO 字符串）
        value: 指标结果字典
    """
    key = build_cache_key(instrument_id, timeframe, adj, last_bar_time)
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

    print("OK")
