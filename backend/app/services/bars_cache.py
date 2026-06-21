"""行情数据 Redis 查询缓存。

提供：
- get_bars_cache: 从 Redis 获取缓存的行情查询结果
- set_bars_cache: 将行情查询结果写入 Redis
- invalidate_bars_cache: 写入时失效该 instrument 的所有缓存

设计要点：
- 缓存 BarListResponse 的 JSON 序列化结果
- TTL 60 秒（可配置 via settings.bars_redis_cache_ttl_seconds）
- Redis 不可用时降级为直查 DB（捕获 RedisError，记录 warning）
- 写入失效使用 SCAN 删除该 instrument 的所有缓存 key

用法：
    from app.services.bars_cache import get_bars_cache, set_bars_cache, invalidate_bars_cache

    # 查询前先查缓存
    cached = await get_bars_cache(instrument_id, timeframe, adj, start, end, page, page_size)
    if cached is not None:
        return cached

    # 缓存 miss 时查 DB 并回填
    response = await _query_bars(...)
    await set_bars_cache(instrument_id, timeframe, adj, start, end, page, page_size, response)

    # 写入后失效缓存
    await invalidate_bars_cache(instrument_id)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.core.redis_client import get_redis

logger = logging.getLogger("bars_cache")

# 缓存 key 前缀
_CACHE_PREFIX = "bars"


def _build_cache_key(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    start: date | datetime,
    end: date | datetime,
    page: int,
    page_size: int,
) -> str:
    """构造缓存 key。

    格式：bars:{instrument_id}:{timeframe}:{adj}:{start}:{end}:{page}:{page_size}
    """
    return (
        f"{_CACHE_PREFIX}:{instrument_id}:{timeframe}:{adj}:{start}:{end}:{page}:{page_size}"
    )


def _serialize_response(response: Any) -> str:
    """序列化 BarListResponse 为 JSON 字符串。

    Args:
        response: BarListResponse 对象（含 Pydantic model）

    Returns:
        JSON 字符串
    """
    # Pydantic v2 model_dump
    if hasattr(response, "model_dump"):
        data = response.model_dump(mode="json")
    elif hasattr(response, "dict"):
        data = response.dict()
    else:
        data = response
    return json.dumps(data, ensure_ascii=False, default=str)


def _deserialize_response(json_str: str) -> dict[str, Any]:
    """反序列化 JSON 字符串为字典。"""
    return json.loads(json_str)


async def get_bars_cache(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    start: date | datetime,
    end: date | datetime,
    page: int,
    page_size: int,
) -> dict[str, Any] | None:
    """从 Redis 获取缓存的行情查询结果。

    Args:
        instrument_id: 标的 UUID
        timeframe: 周期（1d/15m/1h/1w/1mo）
        adj: 复权方式（qfq/none）
        start: 起始日期/时间
        end: 结束日期/时间
        page: 页码
        page_size: 每页大小

    Returns:
        缓存的字典数据，缓存 miss 或 Redis 不可用时返回 None
    """
    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return None

    key = _build_cache_key(instrument_id, timeframe, adj, start, end, page, page_size)
    try:
        client = get_redis()
        cached = await client.get(key)
        if cached is not None:
            logger.debug("缓存命中 key=%s", key)
            return _deserialize_response(cached)
        logger.debug("缓存未命中 key=%s", key)
    except aioredis.RedisError as exc:
        logger.warning("Redis 缓存读取失败，降级为直查 DB: %s", exc)
    except Exception as exc:
        logger.warning("Redis 缓存读取异常（非 RedisError），降级为直查 DB: %s", exc)
    return None


async def set_bars_cache(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    start: date | datetime,
    end: date | datetime,
    page: int,
    page_size: int,
    response: Any,
) -> None:
    """将行情查询结果写入 Redis 缓存。

    Args:
        instrument_id: 标的 UUID
        timeframe: 周期
        adj: 复权方式
        start: 起始日期/时间
        end: 结束日期/时间
        page: 页码
        page_size: 每页大小
        response: BarListResponse 对象
    """
    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return

    key = _build_cache_key(instrument_id, timeframe, adj, start, end, page, page_size)
    try:
        client = get_redis()
        serialized = _serialize_response(response)
        await client.set(key, serialized, ex=settings.bars_redis_cache_ttl_seconds)
        logger.debug("缓存写入 key=%s ttl=%ds", key, settings.bars_redis_cache_ttl_seconds)
    except aioredis.RedisError as exc:
        logger.warning("Redis 缓存写入失败（不影响查询结果）: %s", exc)
    except Exception as exc:
        logger.warning("Redis 缓存写入异常（非 RedisError）: %s", exc)


async def invalidate_bars_cache(instrument_id: uuid.UUID) -> int:
    """失效指定 instrument 的所有行情缓存。

    在 _upsert_*_bars 写入后调用，确保查询结果一致。
    使用 SCAN 避免阻塞 Redis（不用 KEYS）。

    Args:
        instrument_id: 标的 UUID

    Returns:
        删除的缓存数量
    """
    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return 0

    pattern = f"{_CACHE_PREFIX}:{instrument_id}:*"
    deleted = 0
    try:
        client = get_redis()
        # SCAN 遍历匹配的 key，批量删除
        async for key in client.scan_iter(match=pattern, count=100):
            await client.delete(key)
            deleted += 1
        if deleted > 0:
            logger.info("缓存失效 instrument_id=%s deleted=%d", instrument_id, deleted)
    except aioredis.RedisError as exc:
        logger.warning("Redis 缓存失效失败（不影响写入）: %s", exc)
    except Exception as exc:
        logger.warning("Redis 缓存失效异常（非 RedisError）: %s", exc)
    return deleted


if __name__ == "__main__":
    # 自测入口：验证缓存 key 构造和序列化（不连 Redis，无副作用）
    import inspect

    # 1. 验证函数签名
    sig = inspect.signature(get_bars_cache)
    params = list(sig.parameters.keys())
    assert params == ["instrument_id", "timeframe", "adj", "start", "end", "page", "page_size"], \
        f"get_bars_cache 参数不匹配: {params}"
    print(f"get_bars_cache params={params} ✓")

    sig = inspect.signature(set_bars_cache)
    params = list(sig.parameters.keys())
    assert params == ["instrument_id", "timeframe", "adj", "start", "end", "page", "page_size", "response"], \
        f"set_bars_cache 参数不匹配: {params}"
    print(f"set_bars_cache params={params} ✓")

    sig = inspect.signature(invalidate_bars_cache)
    params = list(sig.parameters.keys())
    assert params == ["instrument_id"], f"invalidate_bars_cache 参数不匹配: {params}"
    print(f"invalidate_bars_cache params={params} ✓")

    # 2. 验证缓存 key 构造
    test_id = uuid.UUID("12345678-1234-1234-1234-123456789012")
    key = _build_cache_key(test_id, "1d", "qfq", date(2026, 1, 1), date(2026, 6, 1), 1, 100)
    expected = "bars:12345678-1234-1234-1234-123456789012:1d:qfq:2026-01-01:2026-06-01:1:100"
    assert key == expected, f"缓存 key 不匹配: {key} != {expected}"
    print(f"缓存 key 构造 ✓: {key}")

    # 3. 验证序列化/反序列化
    mock_response = {
        "items": [{"close": 10.5, "open": 10.0}],
        "total": 1,
        "page": 1,
        "page_size": 100,
        "timeframe": "1d",
        "adj": "qfq",
    }
    serialized = _serialize_response(mock_response)
    deserialized = _deserialize_response(serialized)
    assert deserialized == mock_response, "序列化/反序列化不一致"
    print("序列化/反序列化 ✓")

    # 4. 验证缓存未启用时返回 None
    settings = get_settings()
    original = settings.bars_redis_cache_enabled
    object.__setattr__(settings, "bars_redis_cache_enabled", False)

    import asyncio

    result = asyncio.run(get_bars_cache(test_id, "1d", "qfq", date(2026, 1, 1), date(2026, 6, 1), 1, 100))
    assert result is None, "缓存未启用时应返回 None"
    print("缓存未启用时返回 None ✓")

    deleted = asyncio.run(invalidate_bars_cache(test_id))
    assert deleted == 0, "缓存未启用时应返回 0"
    print("缓存未启用时 invalidate 返回 0 ✓")

    object.__setattr__(settings, "bars_redis_cache_enabled", original)

    print("\n所有自测通过 ✓（未进行 Redis 连接测试）")
