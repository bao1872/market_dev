"""Task 4.3 测试 - 指标结果缓存（Redis）验证。

测试 5 个场景：
1. 缓存键构造（build_cache_key 格式与区分度）
2. 缓存命中（get 返回缓存值）
3. 缓存未命中 + 写入（get 返回 None，set 写入后 get 返回值）
4. last_bar_time 变化触发重算（不同 key）
5. MonitorEvaluation.metrics 复用（API 层集成）

测试策略：
- mock get_redis() 返回 AsyncMock，避免连接真实 Redis
- 缓存键测试为纯函数测试，无需 mock
- MonitorEvaluation 复用测试通过 TestClient + dependency override

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_indicator_cache.py -q
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import indicator_cache

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


# ============================================================
# 测试 1: 缓存键构造
# ============================================================


def test_cache_key_construction() -> None:
    """测试 1: 缓存键格式与区分度。

    格式：indicator:{instrument_id}:{timeframe}:{adj}:{last_bar_time}:{algorithm_version}
    """
    # 基本格式
    key = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )
    expected = f"indicator:{TEST_INSTRUMENT_ID}:1d:qfq:2026-06-18:{indicator_cache.ALGORITHM_VERSION}"
    assert key == expected, f"缓存键格式错误: {key}"

    # last_bar_time=None 回退到 "unknown"
    key_none = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "1d", "qfq", None,
    )
    assert "unknown" in key_none, f"None 应回退到 unknown: {key_none}"

    # 不同 timeframe 生成不同键
    key_15m = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "15m", "qfq", "2026-06-18",
    )
    assert key_15m != key, "不同 timeframe 应生成不同键"

    # 不同 adj 生成不同键
    key_none_adj = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "1d", "none", "2026-06-18",
    )
    assert key_none_adj != key, "不同 adj 应生成不同键"


# ============================================================
# 测试 2: 缓存命中
# ============================================================


@pytest.mark.asyncio
async def test_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 2: 缓存命中，get 返回缓存值。"""
    cached_data = {"layers": [], "data": {"strategy1": {"close": [10.0]}}, "errors": {}}

    mock_redis = AsyncMock()
    mock_redis.get.return_value = json.dumps(cached_data, default=str)
    monkeypatch.setattr(indicator_cache, "get_redis", lambda: mock_redis)

    result = await indicator_cache.get(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )

    assert result is not None
    assert result == cached_data
    # 验证 Redis GET 被调用
    mock_redis.get.assert_called_once()


# ============================================================
# 测试 3: 缓存未命中 + 写入
# ============================================================


@pytest.mark.asyncio
async def test_cache_miss_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 3: 缓存未命中（get 返回 None），set 写入后可读取。

    模拟完整流程：get → None → set → get → 返回值。
    """
    # 第一次 get：返回 None（未命中）
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    monkeypatch.setattr(indicator_cache, "get_redis", lambda: mock_redis)

    result_miss = await indicator_cache.get(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )
    assert result_miss is None, "缓存未命中应返回 None"

    # set 写入
    new_data = {"layers": [{"layer_id": "test"}], "data": {}, "errors": {}}
    await indicator_cache.set(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18", new_data,
    )
    # 验证 Redis SET 被调用，含 TTL
    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    assert call_args.kwargs.get("ex") == indicator_cache.CACHE_TTL_SECONDS, (
        f"SET 应含 TTL={indicator_cache.CACHE_TTL_SECONDS}"
    )

    # 模拟第二次 get：返回写入的值
    mock_redis.get.return_value = json.dumps(new_data, default=str)
    result_hit = await indicator_cache.get(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )
    assert result_hit == new_data, "写入后应能读取"


# ============================================================
# 测试 4: last_bar_time 变化触发重算
# ============================================================


@pytest.mark.asyncio
async def test_last_bar_time_change_triggers_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 4: last_bar_time 变化时缓存键不同，触发重算。

    场景：新 bar 到达后 last_bar_time 从 06-18 变为 06-19，
    旧缓存键 miss，需重新计算。
    """
    # 旧 bar 时间的缓存有数据
    old_data = {"data": {"close": [10.0]}}
    new_data = {"data": {"close": [11.0]}}

    # 模拟 Redis：仅旧 key 有值
    old_key = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )
    new_key = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-19",
    )
    assert old_key != new_key, "不同 last_bar_time 应生成不同键"

    mock_redis = AsyncMock()

    async def mock_get(key):
        if key == old_key:
            return json.dumps(old_data, default=str)
        return None  # 新 key 未命中

    mock_redis.get = mock_get
    monkeypatch.setattr(indicator_cache, "get_redis", lambda: mock_redis)

    # 查询旧 bar 时间：命中
    result_old = await indicator_cache.get(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )
    assert result_old == old_data, "旧 last_bar_time 应命中"

    # 查询新 bar 时间：未命中
    result_new = await indicator_cache.get(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-19",
    )
    assert result_new is None, "新 last_bar_time 应未命中（触发重算）"

    # 写入新数据
    await indicator_cache.set(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-19", new_data,
    )
    mock_redis.set.assert_called_once()


# ============================================================
# 测试 5: MonitorEvaluation.metrics 复用
# ============================================================


@pytest.mark.asyncio
async def test_monitor_evaluation_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 5: 缓存未命中时复用 MonitorEvaluation.metrics。

    场景：Redis 缓存 miss → 查询 MonitorEvaluation → 返回 metrics。
    验证 X-Data-Source: monitor_evaluation 响应头。
    """
    from fastapi.testclient import TestClient

    from app.api import indicators as indicators_api
    from app.main import app

    eval_metrics = {"state": "normal", "events_detected": 0}

    # mock _get_last_bar_time 返回固定值
    async def mock_get_last_bar_time(db, instrument_id):
        return "2026-06-18T15:00:00+08:00"

    # mock indicator_cache.get 返回 None（缓存未命中）
    async def mock_cache_get(*args, **kwargs):
        return None

    # mock indicator_cache.set（记录调用）
    set_called = {"called": False}

    async def mock_cache_set(*args, **kwargs):
        set_called["called"] = True

    # mock _try_monitor_evaluation 返回 metrics
    async def mock_try_eval(db, instrument_id):
        return eval_metrics

    monkeypatch.setattr(indicators_api, "_get_last_bar_time", mock_get_last_bar_time)
    monkeypatch.setattr(indicators_api.indicator_cache, "get", mock_cache_get)
    monkeypatch.setattr(indicators_api.indicator_cache, "set", mock_cache_set)
    monkeypatch.setattr(indicators_api, "_try_monitor_evaluation", mock_try_eval)

    # 覆盖 get_db 依赖（避免真实 DB 查询）
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute.return_value = mock_result

    async def mock_get_db():
        yield mock_session

    app.dependency_overrides[indicators_api.get_db] = mock_get_db
    try:
        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/indicators",
            params={"timeframe": "1d", "adj": "qfq"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "monitor_evaluation"
        assert response.headers["X-Cache-Hit"] == "false"
        assert int(response.headers["X-Total-Ms"]) >= 0
        assert response.json() == eval_metrics
        assert set_called["called"] is True, "应写入缓存"
    finally:
        app.dependency_overrides.pop(indicators_api.get_db, None)


if __name__ == "__main__":
    print("=== test_indicator_cache self-test ===")

    # 验证缓存键构造
    key = indicator_cache.build_cache_key(
        TEST_INSTRUMENT_ID, "1d", "qfq", "2026-06-18",
    )
    assert "indicator:" in key
    assert "1d" in key
    assert "qfq" in key
    print(f"缓存键构造 OK: {key}")

    # 验证 TTL
    assert indicator_cache.CACHE_TTL_SECONDS == 300
    print(f"CACHE_TTL_SECONDS={indicator_cache.CACHE_TTL_SECONDS} OK")

    # 验证算法版本
    assert indicator_cache.ALGORITHM_VERSION == "v1"
    print(f"ALGORITHM_VERSION={indicator_cache.ALGORITHM_VERSION} OK")

    print("OK")
