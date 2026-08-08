"""PG 100 股票真实 scheduled core 计算调用计数测试（PANJI_REMOTE_VERIFY_DB_TEST=1）。

[CHANGE-20260806-CP4A-Amendment] 正式化 CP4A 诊断阶段 Step 5 验证为受版本控制的测试文件。
只在远程验证库（bz_stock_verify_<sha>）运行。

[R1.4 repair] 本测试改为 **SELF-CONTAINED**：不再依赖 synthetic seed 预置 instruments。
测试自己在 db_session 的外层事务/savepoint 内建立 100 只合法 A-share instruments +
当前真实 core 所需的最小 daily history（≥60 根日线，来源 feature_snapshot_service
的 `len(df_1d) < 60` insufficient 判定）+ 必要 released dsa_selector StrategyVersion
（core_run_context.SqlAlchemyReleasedConfigResolver fail-closed 要求），然后真实调用：

    compute_review_core_with_run_items（经 _make_savepoint_session_factory 绑定同一
    _db_connection 外层事务，使服务 savepoint 内读写、fixture 退出时整体 rollback）

并断言：
    - 五类 kernel（dsa_bundle / smc_pine / bollinger / sqzmom / volume_context）各 100 次
      （compute-once：不重复计算）；
    - daily-core 15m reads = 0（review core 只允许日线，不读 15m）；
    - StrategyRuntime.execute = 0（DSA 只做投影，不重新执行策略）；
    - coverage 正确（snapshot_count == 100）。

**不再因 instruments 不足而 SKIP**。savepoint 退出自动回滚，不污染验证库。

**注意**：本测试是 PG 测试，PURE_UNIT_TEST=1 时 skip；必须在远程验证库执行。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

# [CHANGE-20260806-005 / Phase 5] 显式声明 postgres marker（不得只靠 conftest 扫描推断）。
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        _PURE_UNIT_TEST,
        reason="PG 100 股票真实计算测试需远程验证库（PANJI_REMOTE_VERIFY_DB_TEST=1）",
    ),
]

# 测试 trade_date：daily bars 须覆盖到它，且每股 ≥60 根（feature_snapshot_service
# `len(df_1d) < 60` insufficient 判定，见 _compute_review_core_for_trade_date）。
_SELF_TEST_DAILY_BARS = 65
_TRADE_DATE = date(2026, 8, 4)


async def _ensure_strategy_version(db) -> None:
    """建 dsa_selector 的 released StrategyVersion（带 parameters），消除对 seed 预置的依赖。

    [R1.4] core_run_context.SqlAlchemyReleasedConfigResolver 在 scheduled 模式 fail-closed：
    无 released dsa_selector StrategyVersion 即抛 ReleasedConfigError（禁止回退代码常量）。
    manifest 的 parameters 用与 seed `_gen_synthetic_released_dsa_config` 一致的结构
    （参数 spec 数组，每项 {key, type, default}），使 resolver 能映射出 effective config。
    """
    await db.execute(
        text(
            "INSERT INTO strategy_definitions "
            "(id, strategy_key, kind, display_name, environment) "
            "VALUES (gen_random_uuid(), 'dsa_selector', 'selector', 'DSA Selector', 'production') "
            "ON CONFLICT (strategy_key) DO NOTHING"
        )
    )
    await db.flush()
    def_id = (
        await db.execute(
            text("SELECT id FROM strategy_definitions WHERE strategy_key='dsa_selector'")
        )
    ).scalar_one()
    manifest = {
        "selector": "dsa_selector",
        "parameters": [
            {"key": "min_score", "default": 0.6, "type": "float"},
            {"key": "top_n", "default": 20, "type": "int"},
        ],
    }
    await db.execute(
        text(
            "INSERT INTO strategy_versions "
            "(id, strategy_definition_id, version, status, manifest, build_hash, released_at) "
            "VALUES (:id, :did, :ver, 'released', CAST(:manifest AS jsonb), "
            ":build_hash, now()) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "did": str(def_id),
            "ver": f"verify-{uuid.uuid4().hex[:8]}",
            # [R1.5a] asyncpg 无法直接把 dict 绑定给 CAST(:manifest AS jsonb)；
            # 与项目惯例（seed _gen_synthetic_released_dsa_config）一致传 JSON 字符串。
            "manifest": json.dumps(manifest),
            "build_hash": "self-contained-build-1",
        },
    )
    await db.flush()


async def _seed_self_contained_universe(db, n: int = 100) -> list[uuid.UUID]:
    """[R1.4] 在 db_session 外层事务内自建 n 只 A-share instruments + ≥60 根 daily bars。

    - instruments：symbol 为 6 位数字（A-share 规范化代码），status=active，market=SH。
    - daily bars：每股 `_SELF_TEST_DAILY_BARS`(65) 根确定性 OHLC，覆盖到 _TRADE_DATE 当日
      （>=60 根，满足 feature_snapshot_service 的 insufficient(<60) 判定，使 kernel 执行）。
    - 隔离：savepoint 内写入，fixture 退出随外层事务 rollback。
    """
    inst_ids: list[uuid.UUID] = []
    for i in range(n):
        inst_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"self-100-{i}")
        inst_ids.append(inst_id)
        await db.execute(
            text(
                "INSERT INTO instruments "
                "(id, symbol, name, market, status, listing_date) "
                "VALUES (:id, :symbol, :name, 'SH', 'active', '2010-01-04') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": str(inst_id),
                "symbol": f"{600000 + i:06d}",
                "name": f"自包含{i:04d}",
            },
        )
    # daily bars：从 trade_date 倒推 _SELF_TEST_DAILY_BARS 个交易日（weekday 近似），
    # 全部 <= _TRADE_DATE，保证 point-in-time 与 insufficient 判定一致。
    days: list[date] = []
    d = _TRADE_DATE
    while len(days) < _SELF_TEST_DAILY_BARS:
        if d.weekday() < 5:
            days.append(d)
        d = d - timedelta(days=1)
    days.reverse()
    for i, inst_id in enumerate(inst_ids):
        base = 10.0 + (i % 50)
        for idx, day in enumerate(days):
            close = base + (idx % 90) / 90.0
            volume = 10000 + idx
            await db.execute(
                text(
                    "INSERT INTO bars_daily "
                    "(instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor) "
                    "VALUES (:iid, :td, :o, :h, :l, :c, :v, :a, :af) "
                    "ON CONFLICT (instrument_id, trade_date) DO NOTHING"
                ),
                {
                    "iid": str(inst_id),
                    "td": day,
                    "o": close - 0.1,
                    "h": close + 0.2,
                    "l": close - 0.2,
                    "c": close,
                    "v": volume,
                    "a": volume * close,
                    "af": 1.0,
                },
            )
    await db.flush()
    return inst_ids


def _make_savepoint_session_factory(_db_connection):
    """返回绑定 _db_connection 外层事务的 session factory（与 db_session 共享事务）。

    [R1.4] compute_review_core_with_run_items 内部用 session_factory 自建 session 读写。
    若用 test_async_engine（不同连接）将看不到 db_session savepoint 内未提交的
    instruments / bars。绑定同一 _db_connection + create_savepoint 使服务在 savepoint 内
    读写、fixture 退出时随外层事务整体 rollback（不污染验证库）。
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    def _f():
        return AsyncSession(
            _db_connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )

    return _f


async def _ensure_snapshot_run(db, snapshot_run_id: uuid.UUID, trade_date: date,
                               n: int) -> None:
    """确保 snapshot run 存在（running，不伪造终态）。"""
    await db.execute(
        text(
            "INSERT INTO stock_feature_snapshot_runs "
            "(id, trade_date, run_type, status, expected_count, snapshot_count, "
            "failed_count, skipped_count, failure_rate, started_at) "
            "VALUES (:id, :td, 'after_close', 'running', :n, 0, 0, 0, 0.0, now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(snapshot_run_id), "td": trade_date, "n": n},
    )


@pytest.mark.asyncio
async def test_pg_100_stock_real_compute_call_counts(db_session, _db_connection) -> None:
    """100 股真实 compute：kernel 调用计数、15m=0、StrategyRuntime=0、coverage。

    [R1.4 repair] SELF-CONTAINED：自建 100 instruments + 65 根日线 + released dsa_selector，
    不再依赖 seed；savepoint 内真实 core，测试结束随外层事务 rollback。
    """
    # 自建 released dsa_selector + 100 instruments + daily bars（savepoint 内）
    await _ensure_strategy_version(db_session)
    instrument_ids = await _seed_self_contained_universe(db_session, n=100)
    await db_session.commit()  # 释放到外层事务，使 savepoint 嵌套可见
    n = len(instrument_ids)

    sf = _make_savepoint_session_factory(_db_connection)
    snapshot_run_id = uuid.uuid4()
    await _ensure_snapshot_run(db_session, snapshot_run_id, _TRADE_DATE, n)
    await db_session.commit()

    from app.services.feature_snapshot_service import compute_review_core_with_run_items

    # spy 五类 kernel：统计各被调用次数
    kernel_calls = {"dsa": 0, "smc": 0, "bollinger": 0, "sqzmom": 0, "volume": 0}

    # [CHANGE-20260806-005 / Phase 5] 修正 spy 目标：canonical 主链经
    # `compute_core_kernel_bundle` → `_compute_first_pyramid_raw_results` 调用
    # first_pyramid_service 的五个 kernel 函数（compute_dsa_bundle / compute_smc_pine /
    # compute_bollinger_features / compute_sqzmom_lb / compute_volume_context_series）。
    from app.services import first_pyramid_service as fps

    def _wrap(attr, key):
        orig = getattr(fps, attr)

        def _patched(*a, **k):
            kernel_calls[key] += 1
            return orig(*a, **k)

        setattr(fps, attr, _patched)
        return _patched

    # 记录原始引用，测试后恢复
    _wrapped = []
    for attr, key in [
        ("compute_dsa_bundle", "dsa"),
        ("compute_smc_pine", "smc"),
        ("compute_bollinger_features", "bollinger"),
        ("compute_sqzmom_lb", "sqzmom"),
        ("compute_volume_context_series", "volume"),
    ]:
        _wrapped.append((fps, attr, getattr(fps, attr)))
        _wrap(attr, key)

    # 15m read 守卫：spy MDAS.get_bars 拦截 15m 请求
    from app.services.market_data_aggregation_service import MarketDataAggregationService
    orig_get_bars = MarketDataAggregationService.get_bars
    calls_15m = {"count": 0}

    async def _patched_get_bars(self, db, instrument_id, timeframe="1d", **kw):
        if timeframe == "15m":
            calls_15m["count"] += 1
            raise AssertionError(f"daily-core 不应读 15m: {instrument_id}")
        return await orig_get_bars(self, db, instrument_id, timeframe=timeframe, **kw)

    MarketDataAggregationService.get_bars = _patched_get_bars

    # StrategyRuntime.execute 守卫
    import app.services.strategy_batch_service
    runtime_execute_calls = {"count": 0}
    orig_execute = app.services.strategy_batch_service.StrategyRuntime.execute

    async def _patched_execute(self, *a, **k):
        runtime_execute_calls["count"] += 1
        return await orig_execute(self, *a, **k)

    app.services.strategy_batch_service.StrategyRuntime.execute = _patched_execute

    try:
        result = await compute_review_core_with_run_items(
            trade_date=_TRADE_DATE,
            instrument_ids=instrument_ids,
            snapshot_run_id=snapshot_run_id,
            worker_id="pg-test",
            algorithm_version="dsa-v1",
            input_hash="pg-test-100",
            session_factory=sf,
        )
    finally:
        # 恢复全部 spy
        for svc_obj, attr, orig in _wrapped:
            setattr(svc_obj, attr, orig)
        MarketDataAggregationService.get_bars = orig_get_bars
        app.services.strategy_batch_service.StrategyRuntime.execute = orig_execute

    # 硬断言
    snapshot_count = int(result.get("snapshot_count") or 0)
    assert snapshot_count == n, f"应生成 {n} 只快照，实际={snapshot_count}"
    assert calls_15m["count"] == 0, f"daily-core 15m reads 应为 0，实际={calls_15m['count']}"
    assert runtime_execute_calls["count"] == 0, (
        f"StrategyRuntime.execute 应为 0，实际={runtime_execute_calls['count']}"
    )
    # kernel 调用计数（compute-once：五类 kernel 每股各算一次）
    # [CHANGE-20260806-005 / Phase 5] 补齐五类 kernel 断言（dsa/smc/bollinger/sqzmom/volume）。
    for key in ("dsa", "smc", "bollinger", "sqzmom", "volume"):
        assert kernel_calls[key] == n, (
            f"{key} kernel 应为 {n}，实际={kernel_calls[key]}"
        )


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        print("此测试需远程验证库 + db_session，不能直接运行。")

    asyncio.run(_main())
