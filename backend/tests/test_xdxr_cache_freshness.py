"""xdxr Redis 缓存 freshness 测试（纯单元）。

验证 ``PytdxAdapter.get_xdxr_info`` 的 ``force_refresh`` 语义：

- 旧缓存存在时 ``force_refresh=True`` 必须绕过缓存、拉取远端并覆盖缓存；
- 普通调用复用本轮写入的缓存（不二次远端拉取）；
- 空 XDXR 也 negative-cache（不二次拉取）。

不连接真实 Redis / pytdx：``get_settings`` / ``get_sync_redis`` / 实例级
``_fetch_xdxr_from_pytdx`` 全部被 monkeypatch。
用法：PURE_UNIT_TEST=1 pytest tests/test_xdxr_cache_freshness.py -q
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Callable

import pandas as pd
import pytest

from app.core import pytdx_adapter as module

pytestmark = pytest.mark.pure_unit  # 纯单元：不连 Redis / pytdx / DB


class _FakeRedis:
    """内存版 Redis（仅实现 get/set，满足 get_xdxr_info 的缓存读写签名）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


class _Settings:
    bars_redis_cache_enabled = True


def _xdxr_df(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "category": [1] * len(dates),
            "name": ["x"] * len(dates),
            "fenhong": [0] * len(dates),
            "peigujia": [0] * len(dates),
            "songzhuangu": [0] * len(dates),
            "peigu": [0] * len(dates),
        }
    )


def _empty_xdxr_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "category",
            "name",
            "fenhong",
            "peigujia",
            "songzhuangu",
            "peigu",
        ]
    )


def _make_adapter(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    fetch_fn: Callable[[], pd.DataFrame],
) -> tuple[Any, dict[str, int]]:
    """patch settings/redis/fetch，返回 (adapter, 远端调用计数器)。"""
    monkeypatch.setattr(module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(module, "get_sync_redis", lambda: fake_redis)

    adapter = module.PytdxAdapter(max_retries=1, retry_delay=0)
    calls = {"n": 0}

    def fake_fetch(symbol: str) -> pd.DataFrame:
        calls["n"] += 1
        return fetch_fn()

    monkeypatch.setattr(adapter, "_fetch_xdxr_from_pytdx", fake_fetch)
    return adapter, calls


# =============================================================================
# 1. stale 24h 缓存被 force_refresh 忽略
# =============================================================================


def test_force_refresh_ignores_stale_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    # 预置旧缓存（2020 事件，无今日事件）
    stale = _xdxr_df(["2020-01-01"])
    fake_redis.store["xdxr:600519"] = stale.to_json(orient="split")

    def remote() -> pd.DataFrame:
        return _xdxr_df(["2026-09-11"])

    adapter, calls = _make_adapter(monkeypatch, fake_redis, remote)

    df = adapter.get_xdxr_info("600519", force_refresh=True)

    # 远端确实被调用，且返回的是今日数据（不是旧缓存的 2020）
    assert calls["n"] == 1
    assert df["date"].iloc[0] == pd.Timestamp("2026-09-11")
    # 缓存被覆盖为今日数据
    cached = pd.read_json(
        io.StringIO(fake_redis.store["xdxr:600519"]), orient="split"
    )
    assert "2026-09-11" in str(cached["date"].tolist())


# =============================================================================
# 2. 普通调用复用本轮写入的缓存
# =============================================================================


def test_plain_call_reuses_fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()

    def remote() -> pd.DataFrame:
        return _xdxr_df(["2026-09-11"])

    adapter, calls = _make_adapter(monkeypatch, fake_redis, remote)

    adapter.get_xdxr_info("600519", force_refresh=True)  # 首遍：写缓存
    adapter.get_xdxr_info("600519")  # 普通：应命中缓存

    # 第二次不得再次访问远端
    assert calls["n"] == 1
    # 缓存确实已写入
    assert "xdxr:600519" in fake_redis.store


# =============================================================================
# 3. 空 XDXR 也 negative-cache
# =============================================================================


def test_empty_xdxr_negative_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()

    def remote() -> pd.DataFrame:
        return _empty_xdxr_df()

    adapter, calls = _make_adapter(monkeypatch, fake_redis, remote)

    df1 = adapter.get_xdxr_info("600519", force_refresh=True)
    assert df1.empty
    assert calls["n"] == 1

    df2 = adapter.get_xdxr_info("600519")  # 普通：应命中空缓存
    assert df2.empty
    # 不二次远端拉取
    assert calls["n"] == 1
    # 空缓存确实写入（避免审计又重新远端）
    assert "xdxr:600519" in fake_redis.store
