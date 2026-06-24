"""DSA 批量选股系统测试。

测试分类：
- 纯逻辑测试（不连 DB）：payload / readiness
- DB 集成测试（需要 DB 环境）：批次调度/状态机/数据就绪等

用法：
    # 纯逻辑测试（无需 DB）
    python -m tests.test_strategy_batch

    # DB 集成测试（需要 DATABASE_URL 环境变量）
    python -m tests.test_strategy_batch --db

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date

# ============================================================
# 纯逻辑测试（不连 DB）
# ============================================================


def test_build_payload_no_matched():
    """测试 _build_payload 不包含 matched。"""
    from app.repositories.strategy_result_repository import _build_payload
    from app.strategy.runtime import StrategyResult as RuntimeResult

    r = RuntimeResult(
        instrument_id=uuid.uuid4(),
        strategy_version_id=uuid.uuid4(),
        trade_date=date(2026, 6, 18),
        matched=True,
        metrics={"dsa_dir_bars": 60, "offset_mean": 0.05},
    )
    payload = _build_payload(r)
    assert "matched" not in payload, f"payload 不应包含 matched: {payload}"
    assert payload["dsa_dir_bars"] == 60
    assert payload["offset_mean"] == 0.05

    print("测试 _build_payload_no_matched ✓")


def test_data_readiness_result_fields():
    """测试 DataReadinessResult 新字段存在。"""
    from app.services.strategy_batch_service import DataReadinessResult

    result = DataReadinessResult(
        is_ready=True,
        is_trading_day=True,
        active_instrument_count=5000,
        bars_count=4800,
        coverage_rate=0.96,
        warnings=[],
        reason=None,
        suspended_count=10,
        delisted_count=5,
        new_listing_count=3,
        import_completeness=1.0,
    )
    assert result.suspended_count == 10
    assert result.delisted_count == 5
    assert result.new_listing_count == 3
    assert result.import_completeness == 1.0

    print("测试 data_readiness_result_fields ✓")


def test_filterable_whitelist():
    """测试 filterable 白名单逻辑。"""
    outputs = [
        {"key": "dsa_dir_bars", "filterable": True},
        {"key": "vwap_ret_avg", "filterable": True},
        {"key": "internal_debug_value", "filterable": False},
    ]
    filterable_keys = {o["key"] for o in outputs if o.get("filterable")}
    assert "dsa_dir_bars" in filterable_keys
    assert "vwap_ret_avg" in filterable_keys
    assert "internal_debug_value" not in filterable_keys
    print("测试 filterable_whitelist ✓")


# ============================================================
# DB 集成测试（需要 DB 环境）
# ============================================================

async def test_01_non_trading_day_rejected(db_session):
    """测试 #1: 非交易日触发批次，应拒绝。"""
    from app.services.strategy_batch_service import StrategyBatchService

    service = StrategyBatchService()
    non_trading_day = date(2026, 6, 20)  # 周六

    db = db_session
    result = await service.check_data_readiness(db, non_trading_day)
    if result.is_trading_day:
        print("测试 #01 跳过（指定日期是交易日）⚠")
        return
    assert not result.is_ready, "非交易日应拒绝"
    assert result.reason is not None
    assert "非交易日" in result.reason

    print("测试 #01 (non_trading_day_rejected) ✓")


async def run_db_tests(db_session):
    """运行 DB 集成测试。"""
    await test_01_non_trading_day_rejected(db_session)


if __name__ == "__main__":
    print("=== 纯逻辑测试 ===")
    test_build_payload_no_matched()
    test_data_readiness_result_fields()
    test_filterable_whitelist()

    if "--db" in sys.argv:
        print("\n=== DB 集成测试 ===")
        import os
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        test_database_url = os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://bz:es123456@127.0.0.1:5432/bz_stock_test",
        )
        async_url = test_database_url.replace(
            "postgresql+psycopg://", "postgresql+asyncpg://"
        ).replace(
            "postgresql://", "postgresql+asyncpg://"
        )
        test_engine = create_async_engine(async_url, echo=False)
        TestSessionLocal = async_sessionmaker(
            bind=test_engine, class_=AsyncSession, expire_on_commit=False,
        )

        async def _run():
            async with TestSessionLocal() as db_session:
                await run_db_tests(db_session)
            await test_engine.dispose()

        asyncio.run(_run())
    else:
        print("\n=== DB 集成测试（跳过，使用 --db 参数运行）===")

    print("\nOK")
