"""幂等键管理 - 基于 Redis SET NX EX 的幂等性保证。

用途：
- Job 入队幂等（job_queue.py 已内置）
- Outbox 投递幂等
- 通知投递幂等

设计：
- check_and_record(key, ttl): 检查幂等键是否已存在，不存在则记录
  使用 SET NX EX 原子操作，返回 True 表示首次（可执行），False 表示重复（跳过）
"""

from __future__ import annotations

from app.core.redis_client import get_redis

# Redis 幂等键前缀
_IDEM_PREFIX = "idem:"

# 默认 TTL（秒）：24 小时
DEFAULT_TTL = 24 * 3600


async def check_and_record(
    key: str,
    ttl: int = DEFAULT_TTL,
) -> bool:
    """检查并记录幂等键。

    原子操作：SET key 1 NX EX ttl
    - 首次调用（key 不存在）：设置成功，返回 True（可执行业务）
    - 重复调用（key 已存在）：设置失败，返回 False（跳过业务）

    Args:
        key: 幂等键（不含前缀）
        ttl: 幂等键 TTL（秒），超时后可再次执行

    Returns:
        True（首次，可执行），False（重复，跳过）

    Raises:
        ValueError: 参数非法
    """
    if not key:
        raise ValueError("key 不能为空")
    if ttl <= 0:
        raise ValueError("ttl 必须大于 0")

    redis = get_redis()
    idem_key = f"{_IDEM_PREFIX}{key}"
    # SET NX EX：仅当 key 不存在时设置
    result = await redis.set(idem_key, "1", nx=True, ex=ttl)
    return bool(result)


async def check(key: str) -> bool:
    """检查幂等键是否已存在（不记录）。

    Args:
        key: 幂等键（不含前缀）

    Returns:
        True（已存在，已执行过），False（不存在，未执行）
    """
    if not key:
        raise ValueError("key 不能为空")
    redis = get_redis()
    idem_key = f"{_IDEM_PREFIX}{key}"
    result = await redis.get(idem_key)
    return result is not None


async def clear(key: str) -> bool:
    """清除幂等键（用于测试或回滚场景）。

    Args:
        key: 幂等键（不含前缀）

    Returns:
        True（清除成功），False（key 不存在）
    """
    if not key:
        raise ValueError("key 不能为空")
    redis = get_redis()
    idem_key = f"{_IDEM_PREFIX}{key}"
    result = await redis.delete(idem_key)
    return bool(result)


if __name__ == "__main__":
    # 自测入口：验证函数可导入（不连接 Redis）
    print(f"check_and_record={check_and_record}")
    print(f"check={check}")
    print(f"clear={clear}")
    print(f"prefix={_IDEM_PREFIX}")
    print(f"default ttl={DEFAULT_TTL}")
    print("OK")
