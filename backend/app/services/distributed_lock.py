"""分布式锁 - 基于 Redis SET NX EX + Lua 脚本释放。

设计：
- acquire_lock(key, ttl, holder): SET key holder NX EX ttl，原子获取
- release_lock(key, holder): Lua 脚本校验 holder 后 DEL，原子释放
- renew_lock(key, ttl, holder): Lua 脚本校验 holder 后 EXPIRE，原子续期

holder 标识锁的持有者（如 worker_id），防止误释放他人持有的锁。
Lua 脚本保证"检查 + 操作"的原子性，避免竞态条件。
"""

from __future__ import annotations

import uuid

from app.core.redis_client import get_redis

# Redis 锁键前缀
_LOCK_PREFIX = "lock:"

# 释放锁的 Lua 脚本：仅当 holder 匹配时才 DEL
# KEYS[1] = lock key, ARGV[1] = holder
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

# 续期锁的 Lua 脚本：仅当 holder 匹配时才 EXPIRE
# KEYS[1] = lock key, ARGV[1] = holder, ARGV[2] = ttl
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""


def generate_holder() -> str:
    """生成唯一的锁持有者标识。"""
    return str(uuid.uuid4())


async def acquire_lock(
    key: str,
    ttl: int,
    holder: str | None = None,
) -> str | None:
    """获取分布式锁（Redis SET NX EX）。

    Args:
        key: 锁键（不含前缀）
        ttl: 锁 TTL（秒），超时自动释放
        holder: 持有者标识，未提供则自动生成

    Returns:
        holder 字符串（获取成功），None（获取失败，锁已被持有）

    说明：
        - 使用 SET key holder NX EX ttl 原子操作
        - 获取失败返回 None，调用方可重试或放弃
    """
    if ttl <= 0:
        raise ValueError("ttl 必须大于 0")
    if holder is None:
        holder = generate_holder()

    redis = get_redis()
    lock_key = f"{_LOCK_PREFIX}{key}"
    # SET NX EX：仅当 key 不存在时设置，并带 TTL
    acquired = await redis.set(lock_key, holder, nx=True, ex=ttl)
    if acquired:
        return holder
    return None


async def release_lock(key: str, holder: str) -> bool:
    """释放分布式锁（Lua 脚本保证原子性）。

    Args:
        key: 锁键（不含前缀）
        holder: 持有者标识（必须与获取时一致）

    Returns:
        True（释放成功），False（holder 不匹配或锁已过期）
    """
    redis = get_redis()
    lock_key = f"{_LOCK_PREFIX}{key}"
    result = await redis.eval(_RELEASE_SCRIPT, 1, lock_key, holder)
    return bool(result)


async def renew_lock(key: str, ttl: int, holder: str) -> bool:
    """续期分布式锁（Lua 脚本保证原子性）。

    Args:
        key: 锁键（不含前缀）
        ttl: 新的 TTL（秒）
        holder: 持有者标识（必须与获取时一致）

    Returns:
        True（续期成功），False（holder 不匹配或锁已过期）
    """
    if ttl <= 0:
        raise ValueError("ttl 必须大于 0")
    redis = get_redis()
    lock_key = f"{_LOCK_PREFIX}{key}"
    result = await redis.eval(_RENEW_SCRIPT, 1, lock_key, holder, ttl)
    return bool(result)


if __name__ == "__main__":
    # 自测入口：验证函数可导入与 Lua 脚本定义（不连接 Redis）
    print(f"acquire_lock={acquire_lock}")
    print(f"release_lock={release_lock}")
    print(f"renew_lock={renew_lock}")
    print(f"generate_holder={generate_holder()}")
    print(f"release script length={len(_RELEASE_SCRIPT)}")
    print(f"renew script length={len(_RENEW_SCRIPT)}")
    assert "GET" in _RELEASE_SCRIPT and "DEL" in _RELEASE_SCRIPT
    assert "GET" in _RENEW_SCRIPT and "EXPIRE" in _RENEW_SCRIPT
    print("OK")
