"""Redis 客户端 - 异步与同步连接池。

提供：
- get_redis(): 获取异步 Redis 客户端单例（redis.asyncio.Redis）
- get_sync_redis(): 获取同步 Redis 客户端单例（redis.Redis），供同步代码使用
- close_redis(): 关闭异步连接池（应用退出时调用）
- close_sync_redis(): 关闭同步连接池（应用退出时调用）

用途：
- Job 队列（job_queue.py）
- 分布式锁（distributed_lock.py）
- 幂等键（idempotency.py）
- Outbox relay 投递目标
- 行情 xdxr 缓存（pytdx_adapter.py，同步客户端）

连接配置：从 Settings.redis_url 读取（redis://localhost:6379/0）
"""

from __future__ import annotations

import redis
import redis.asyncio as aioredis

from app.config import get_settings

# 异步客户端单例（供 async 代码使用）
_redis_client: aioredis.Redis | None = None

# 同步客户端单例（供同步代码使用，如 PytdxAdapter）
_sync_redis_client: redis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """获取异步 Redis 客户端单例。

    首次调用时创建连接池，后续复用。
    """
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=50,
        )
    return _redis_client


def get_sync_redis() -> redis.Redis:
    """获取同步 Redis 客户端单例（供同步代码使用，如 PytdxAdapter）。

    首次调用时创建连接池，后续复用。
    用于避免在同步类中桥接异步 Redis 的复杂性。
    """
    global _sync_redis_client
    if _sync_redis_client is None:
        settings = get_settings()
        _sync_redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
    return _sync_redis_client


async def close_redis() -> None:
    """关闭异步 Redis 连接池（应用退出时调用）。"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def close_sync_redis() -> None:
    """关闭同步 Redis 连接池（应用退出时调用）。"""
    global _sync_redis_client
    if _sync_redis_client is not None:
        _sync_redis_client.close()
        _sync_redis_client = None


if __name__ == "__main__":
    # 自测入口：验证客户端创建（不连接 Redis）
    async_client = get_redis()
    sync_client = get_sync_redis()
    print(f"async_redis_client={async_client}")
    print(f"sync_redis_client={sync_client}")
    print(f"redis_url={get_settings().redis_url}")

    # 验证单例
    assert get_redis() is async_client, "异步客户端应为单例"
    assert get_sync_redis() is sync_client, "同步客户端应为单例"
    print("单例验证 ✓")

    # 验证类型
    assert isinstance(sync_client, redis.Redis), "同步客户端应为 redis.Redis"
    print("类型验证 ✓")

    print("OK")
