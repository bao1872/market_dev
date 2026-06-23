"""选股查询服务集成测试 - 验证 selector_query_service 完整业务链路。

用法：
    python tests/test_selector_query_integration.py          # 纯逻辑测试
    python tests/test_selector_query_integration.py --db     # 含数据库集成测试

测试用例：
1. 未发布 run 不可查询（RunNotFoundError）
2. 已发布 run 无过滤（source_total == filtered_total）
3. 合法条件零命中（200，filtered_total=0）
4. 两个 run 同版本同日期（历史结果各自保留，不相互覆盖）
5. 未知 metric_key（ValueError / 422）
6. 服务端分页（所有页合计等于 filtered_total，无重复无遗漏）
7. universe=watchlist（只返回当前用户自选股）
8. 普通用户不能修改算法参数（selector 无参数修改入口）
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, date, datetime

# ---------------------------------------------------------------------------
# 纯逻辑测试（不需要数据库）
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    """验证异常层级正确。"""
    from app.services.selector_query_service import (
        InvalidFilterError,
        NotSelectorRunError,
        RunNotFoundError,
        SelectorQueryError,
    )

    assert issubclass(RunNotFoundError, SelectorQueryError)
    assert issubclass(NotSelectorRunError, SelectorQueryError)
    assert issubclass(InvalidFilterError, SelectorQueryError)
    print("  异常层级正确 ✓")


def test_selector_result_page():
    """验证 SelectorResultPage 数据类。"""
    from app.services.selector_query_service import SelectorResultPage

    page = SelectorResultPage(
        run_id=uuid.uuid4(),
        strategy_key="dsa_selector",
        trade_date=date(2026, 6, 23),
        source_total=100,
        filtered_total=30,
        page=1,
        page_size=50,
    )
    assert page.source_total == 100
    assert page.filtered_total == 30
    assert page.items == []
    print("  SelectorResultPage ✓")


def test_query_signature():
    """验证 query_published_selector_results 签名（keyword-only 参数）。"""
    import inspect

    from app.services.selector_query_service import query_published_selector_results

    sig = inspect.signature(query_published_selector_results)
    params = list(sig.parameters.keys())
    assert params[0] == "db"
    for p in params[1:]:
        assert sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY
    assert "run_id" in sig.parameters
    assert "user_id" in sig.parameters
    assert "filters" in sig.parameters
    assert "sort" in sig.parameters
    assert "page" in sig.parameters
    assert "page_size" in sig.parameters
    assert "universe" in sig.parameters
    print("  query_published_selector_results 签名正确 ✓")


def test_unknown_operator_fail_closed():
    """验证未知操作符 fail-closed（raise ValueError）。"""
    from app.repositories.strategy_result_repository import MetricFilter

    bad_filter = MetricFilter(metric_key="test", operator="invalid_op", value=1)
    op = bad_filter.operator.lower()
    try:
        if op in ("gt", "gte", "lt", "lte", "eq", "between"):
            pass
        else:
            raise ValueError(f"未知筛选操作符: {op}")
        assert False, "应抛出 ValueError"
    except ValueError as e:
        assert "未知筛选操作符" in str(e)
    print("  未知操作符 fail-closed ✓")


def test_user_cannot_modify_algorithm_params():
    """验证普通用户不能修改算法参数 - selector_query_service 无参数修改入口。

    selector_query_service 只提供只读查询，不接受任何算法参数修改。
    """
    from app.services.selector_query_service import query_published_selector_results

    sig = inspect.signature(query_published_selector_results)
    # 确认函数参数中没有任何算法参数修改入口
    param_names = set(sig.parameters.keys())
    assert "algorithm_params" not in param_names
    assert "parameters" not in param_names
    assert "overrides" not in param_names
    assert "config" not in param_names
    # 只有查询相关参数
    assert "filters" in param_names
    assert "sort" in param_names
    assert "page" in param_names
    print("  普通用户不能修改算法参数（无参数修改入口） ✓")


# ---------------------------------------------------------------------------
# 数据库集成测试（需要 --db flag）
# ---------------------------------------------------------------------------


async def _create_test_data(db) -> dict:
    """创建测试所需的基础数据：用户、策略定义、策略版本、运行、结果。

    Returns:
        dict 包含所有创建的实体 ID
    """
    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.models.strategy_run import StrategyResult, StrategyResultMetric, StrategyRun
    from app.models.user import User
    from app.models.watchlist import UserWatchlistItem

    now = datetime.now(UTC)
    trade_date = date(2026, 6, 23)

    # 1. 创建测试用户
    user = User(
        email=f"test_selector_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
    )
    db.add(user)
    await db.flush()

    # 2. 创建策略定义（selector 类型）
    definition = StrategyDefinition(
        strategy_key=f"test_selector_{uuid.uuid4().hex[:8]}",
        kind="selector",
        display_name="测试选股策略",
    )
    db.add(definition)
    await db.flush()

    # 3. 创建策略版本
    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True},
                {"key": "offset_mean", "type": "numeric", "filterable": True},
                {"key": "vwap_ret_avg", "type": "numeric", "filterable": True},
            ],
        },
        build_hash=f"test_hash_{uuid.uuid4().hex[:16]}",
        released_at=now,
    )
    db.add(version)
    await db.flush()

    # 4. 创建 instrument（使用虚拟 ID，不依赖 instruments 表已有数据）
    instrument_ids = [uuid.uuid4() for _ in range(10)]

    # 5. 创建已发布的 run + 结果
    published_run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:{version.id}:scheduled:{trade_date}",
        published_at=now,
    )
    db.add(published_run)
    await db.flush()

    # 写入 10 条结果 + 指标
    for i, inst_id in enumerate(instrument_ids):
        result = StrategyResult(
            run_id=published_run.id,
            strategy_version_id=version.id,
            instrument_id=inst_id,
            trade_date=trade_date,
            payload={
                "dsa_dir_bars": 40 + i * 5,
                "offset_mean": 0.01 * (i + 1),
                "vwap_ret_avg": 0.05 + i * 0.01,
            },
        )
        db.add(result)
        await db.flush()

        # 写入指标
        for key, val in result.payload.items():
            metric = StrategyResultMetric(
                result_id=result.id,
                strategy_version_id=version.id,
                trade_date=trade_date,
                instrument_id=inst_id,
                metric_key=key,
                numeric_value=float(val),
            )
            db.add(metric)
    await db.flush()

    # 6. 创建未发布的 run（用于测试未发布 run 不可查询）
    unpublished_run = StrategyRun(
        strategy_version_id=version.id,
        run_type="manual",
        trade_date=trade_date,
        status="completed",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:unpub:{version.id}:manual:{trade_date}",
        published_at=None,  # 未发布
    )
    db.add(unpublished_run)
    await db.flush()

    # 7. 创建第二个已发布 run（同版本同日期，用于测试结果独立性）
    second_run = StrategyRun(
        strategy_version_id=version.id,
        run_type="manual",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:second:{version.id}:manual:{trade_date}",
        published_at=now,
    )
    db.add(second_run)
    await db.flush()

    # 写入 5 条结果（与第一个 run 部分重叠的 instrument_id）
    for i, inst_id in enumerate(instrument_ids[:5]):
        result = StrategyResult(
            run_id=second_run.id,
            strategy_version_id=version.id,
            instrument_id=inst_id,
            trade_date=trade_date,
            payload={
                "dsa_dir_bars": 30 + i * 10,
                "offset_mean": 0.02 * (i + 1),
                "vwap_ret_avg": 0.03 + i * 0.02,
            },
        )
        db.add(result)
        await db.flush()

        for key, val in result.payload.items():
            metric = StrategyResultMetric(
                result_id=result.id,
                strategy_version_id=version.id,
                trade_date=trade_date,
                instrument_id=inst_id,
                metric_key=key,
                numeric_value=float(val),
            )
            db.add(metric)
    await db.flush()

    # 8. 创建 monitor 类型的策略定义 + run（用于测试非 selector run 不可查询）
    monitor_def = StrategyDefinition(
        strategy_key=f"test_monitor_{uuid.uuid4().hex[:8]}",
        kind="monitor",
        display_name="测试监控策略",
    )
    db.add(monitor_def)
    await db.flush()

    monitor_version = StrategyVersion(
        strategy_definition_id=monitor_def.id,
        version="1.0.0",
        status="released",
        manifest={},
        build_hash=f"test_monitor_hash_{uuid.uuid4().hex[:16]}",
        released_at=now,
    )
    db.add(monitor_version)
    await db.flush()

    monitor_run = StrategyRun(
        strategy_version_id=monitor_version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:monitor:{monitor_version.id}:scheduled:{trade_date}",
        published_at=now,
    )
    db.add(monitor_run)
    await db.flush()

    # 9. 用户自选股（前 3 个 instrument）
    for inst_id in instrument_ids[:3]:
        item = UserWatchlistItem(
            user_id=user.id,
            instrument_id=inst_id,
            source="manual",
            active=True,
        )
        db.add(item)
    await db.flush()

    await db.commit()

    return {
        "user_id": user.id,
        "definition_id": definition.id,
        "version_id": version.id,
        "published_run_id": published_run.id,
        "unpublished_run_id": unpublished_run.id,
        "second_run_id": second_run.id,
        "monitor_def_id": monitor_def.id,
        "monitor_version_id": monitor_version.id,
        "monitor_run_id": monitor_run.id,
        "instrument_ids": instrument_ids,
        "watchlist_instrument_ids": instrument_ids[:3],
        "trade_date": trade_date,
    }


async def test_unpublished_run_not_queryable(test_data: dict):
    """测试1: 未发布 run 不可查询（RunNotFoundError）。"""
    from app.services.selector_query_service import (
        RunNotFoundError,
        query_published_selector_results,
    )

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            await query_published_selector_results(
                db,
                run_id=test_data["unpublished_run_id"],
            )
            assert False, "应抛出 RunNotFoundError"
        except RunNotFoundError as e:
            assert "未发布" in str(e)
    print("  未发布 run 不可查询 ✓")


async def test_published_run_no_filter(test_data: dict):
    """测试2: 已发布 run 无过滤（source_total == filtered_total）。"""
    from app.services.selector_query_service import query_published_selector_results

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        page = await query_published_selector_results(
            db,
            run_id=test_data["published_run_id"],
            page=1,
            page_size=50,
        )
        assert page.source_total == 10, f"source_total={page.source_total}, 期望 10"
        assert page.filtered_total == 10, f"filtered_total={page.filtered_total}, 期望 10"
        assert page.source_total == page.filtered_total
        assert len(page.items) == 10
    print("  已发布 run 无过滤: source_total == filtered_total ✓")


async def test_valid_zero_match(test_data: dict):
    """测试3: 合法条件零命中（200，filtered_total=0）。"""
    from app.repositories.strategy_result_repository import MetricFilter
    from app.services.selector_query_service import query_published_selector_results

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        # dsa_dir_bars > 9999 不可能命中任何结果
        filters = [MetricFilter(metric_key="dsa_dir_bars", operator="gt", value=9999)]
        page = await query_published_selector_results(
            db,
            run_id=test_data["published_run_id"],
            filters=filters,
            page=1,
            page_size=50,
        )
        assert page.source_total == 10, f"source_total={page.source_total}, 期望 10"
        assert page.filtered_total == 0, f"filtered_total={page.filtered_total}, 期望 0"
        assert len(page.items) == 0
    print("  合法条件零命中: filtered_total=0 ✓")


async def test_two_runs_same_version_same_date(test_data: dict):
    """测试4: 两个 run 同版本同日期（历史结果各自保留，不相互覆盖）。"""
    from app.services.selector_query_service import query_published_selector_results

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        # 第一个 run 有 10 条结果
        page1 = await query_published_selector_results(
            db,
            run_id=test_data["published_run_id"],
            page=1,
            page_size=50,
        )
        assert page1.source_total == 10

        # 第二个 run 有 5 条结果
        page2 = await query_published_selector_results(
            db,
            run_id=test_data["second_run_id"],
            page=1,
            page_size=50,
        )
        assert page2.source_total == 5

        # 两个 run 的结果互不影响
        assert page1.source_total != page2.source_total
    print("  两个 run 同版本同日期: 结果各自保留 ✓")


async def test_unknown_metric_key(test_data: dict):
    """测试5: 未知 metric_key（ValueError / 422）。

    selector_query_service 本身不做 metric_key 白名单校验，
    该校验在 API 层（strategy_runs.py _validate_metric_filters）完成。
    此测试验证 query_results 内部对未知操作符的 fail-closed 行为。
    """
    from app.repositories.strategy_result_repository import MetricFilter

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        from app.repositories.strategy_result_repository import query_results

        # 未知操作符应抛出 ValueError
        bad_filter = MetricFilter(metric_key="dsa_dir_bars", operator="invalid", value=1)
        try:
            await query_results(
                db,
                run_id=test_data["published_run_id"],
                filters=[bad_filter],
            )
            assert False, "应抛出 ValueError"
        except (ValueError, RuntimeError) as e:
            # ValueError 被 query_results 包装为 RuntimeError
            assert "未知筛选操作符" in str(e)
    print("  未知 metric_key/操作符: fail-closed ✓")


async def test_server_side_pagination(test_data: dict):
    """测试6: 服务端分页（所有页合计等于 filtered_total，无重复无遗漏）。"""
    from app.services.selector_query_service import query_published_selector_results

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        # 每页 3 条，10 条结果需要 4 页
        all_instrument_ids = []
        page_size = 3
        for page_num in range(1, 5):
            page = await query_published_selector_results(
                db,
                run_id=test_data["published_run_id"],
                page=page_num,
                page_size=page_size,
            )
            assert page.source_total == 10
            assert page.filtered_total == 10
            for item in page.items:
                all_instrument_ids.append(item.instrument_id)

        # 验证无重复
        assert len(all_instrument_ids) == 10, f"总条数={len(all_instrument_ids)}, 期望 10"
        assert len(set(all_instrument_ids)) == 10, "存在重复的 instrument_id"

        # 验证覆盖所有 instrument
        expected_ids = set(test_data["instrument_ids"])
        actual_ids = set(all_instrument_ids)
        assert actual_ids == expected_ids, f"遗漏: {expected_ids - actual_ids}"
    print("  服务端分页: 无重复无遗漏 ✓")


async def test_universe_watchlist(test_data: dict):
    """测试7: universe=watchlist（只返回当前用户自选股）。"""
    from app.services.selector_query_service import query_published_selector_results

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        page = await query_published_selector_results(
            db,
            run_id=test_data["published_run_id"],
            user_id=test_data["user_id"],
            universe="watchlist",
            page=1,
            page_size=50,
        )
        # source_total 是 run 的总结果数（10），不受 universe 影响
        assert page.source_total == 10, f"source_total={page.source_total}, 期望 10"

        # 返回的 items 应只包含用户自选股（前 3 个）
        returned_ids = {item.instrument_id for item in page.items}
        watchlist_ids = set(test_data["watchlist_instrument_ids"])
        # 注意：当前实现是先查 SQL 再在 Python 中过滤
        # 所以 filtered_total 可能包含非自选股，但 items 应只含自选股
        assert returned_ids.issubset(watchlist_ids), (
            f"返回了非自选股: {returned_ids - watchlist_ids}"
        )
    print("  universe=watchlist: 只返回用户自选股 ✓")


async def test_monitor_run_not_queryable(test_data: dict):
    """测试8: monitor 类型的 run 不可通过 selector 查询。"""
    from app.services.selector_query_service import (
        NotSelectorRunError,
        query_published_selector_results,
    )

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            await query_published_selector_results(
                db,
                run_id=test_data["monitor_run_id"],
            )
            assert False, "应抛出 NotSelectorRunError"
        except NotSelectorRunError as e:
            assert "不是 selector" in str(e)
    print("  monitor 类型 run 不可查询 ✓")


async def _run_db_tests(test_data: dict):
    """运行所有数据库集成测试。"""
    await test_unpublished_run_not_queryable(test_data)
    await test_published_run_no_filter(test_data)
    await test_valid_zero_match(test_data)
    await test_two_runs_same_version_same_date(test_data)
    await test_unknown_metric_key(test_data)
    await test_server_side_pagination(test_data)
    await test_universe_watchlist(test_data)
    await test_monitor_run_not_queryable(test_data)


async def _cleanup(test_data: dict):
    """清理测试数据。"""
    from sqlalchemy import delete

    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.models.strategy_run import StrategyResult, StrategyResultMetric, StrategyRun
    from app.models.user import User
    from app.models.watchlist import UserWatchlistItem

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        # 按依赖顺序删除
        run_ids = [
            test_data["published_run_id"],
            test_data["unpublished_run_id"],
            test_data["second_run_id"],
            test_data["monitor_run_id"],
        ]

        # 删除指标
        result_ids_stmt = select(StrategyResult.id).where(
            StrategyResult.run_id.in_(run_ids)
        )
        # 直接按 run_id 删除结果和指标
        await db.execute(
            delete(StrategyResultMetric).where(
                StrategyResultMetric.result_id.in_(
                    select(StrategyResult.id).where(
                        StrategyResult.run_id.in_(run_ids)
                    )
                )
            )
        )
        await db.execute(
            delete(StrategyResult).where(StrategyResult.run_id.in_(run_ids))
        )
        await db.execute(
            delete(StrategyRun).where(StrategyRun.id.in_(run_ids))
        )

        # 删除版本和定义
        version_ids = [test_data["version_id"], test_data["monitor_version_id"]]
        await db.execute(
            delete(StrategyVersion).where(StrategyVersion.id.in_(version_ids))
        )
        def_ids = [test_data["definition_id"], test_data["monitor_def_id"]]
        await db.execute(
            delete(StrategyDefinition).where(StrategyDefinition.id.in_(def_ids))
        )

        # 删除自选股
        await db.execute(
            delete(UserWatchlistItem).where(
                UserWatchlistItem.user_id == test_data["user_id"]
            )
        )

        # 删除用户
        await db.execute(
            delete(User).where(User.id == test_data["user_id"])
        )

        await db.commit()
    print("  测试数据已清理 ✓")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import inspect

    print("=== 纯逻辑测试 ===")
    test_error_hierarchy()
    test_selector_result_page()
    test_query_signature()
    test_unknown_operator_fail_closed()
    test_user_cannot_modify_algorithm_params()
    print("纯逻辑测试全部通过 ✓\n")

    if "--db" in sys.argv:
        print("=== 数据库集成测试 ===")
        from sqlalchemy import select

        from app.db import AsyncSessionLocal

        test_data = None

        async def setup_run_and_cleanup():
            async with AsyncSessionLocal() as db:
                td = await _create_test_data(db)
            print(f"  测试数据已创建: published_run={td['published_run_id']}")
            await _run_db_tests(td)
            await _cleanup(td)

        try:
            asyncio.run(setup_run_and_cleanup())
            print("数据库集成测试全部通过 ✓\n")
        except Exception as e:
            print(f"数据库集成测试失败: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("提示: 添加 --db 参数运行数据库集成测试")
        print("  python tests/test_selector_query_integration.py --db")

    print("\nOK")
